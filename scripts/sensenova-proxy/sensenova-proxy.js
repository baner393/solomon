/**
 * SenseNova 智能限流代理 - 多 Key 轮换 + 多模型独立冷却 + 队列
 *
 * ⚠️ 修改前请阅读：C:\Users\ban\.zcode\v2\PROXY-GUIDE.md
 *
 * GLM-5.2 限额严重，需要排队和冷却管理，不能简单透传
 * 特性：
 * 1. 多 API Key 轮换，每个模型独立跟踪
 * 2. 请求排队 + 并发控制
 * 3. 429 直返客户端（不内部重试）——避免触发 workspace 级限流
 * 4. 各模型独立冷却：glm-5.2 限流不影响 sensenova-6.8-flash-lite
 * 5. 非代理模型直接透传
 * 6. 客户端断开自动释放锁
 * 7. 队列上限保护（50）
 * 8. 排队超时保护（30 秒）
 * 9. 心跳自检：事件循环卡死超 30 秒自动退出
 * 10. DNS 缓存防解析失败
 */
const http = require('http');
const https = require('https');
const dns = require('dns');
const fs = require('fs');

const PORT = 3456;
const SENSENOVA_HOST = 'token.sensenova.cn';
const API_KEYS = [
  'SK_YOUR_KEY_1',
  'SK_YOUR_KEY_1',
  'SK_YOUR_KEY_1',
  'SK_YOUR_KEY_1',
  'SK_YOUR_KEY_1',
];
const MAX_RETRIES = 5; // 429/配额耗尽换 key 轮换的最大尝试次数
const MAX_QUEUE_SIZE = 50;
const REQUEST_TIMEOUT_MS = 300000; // 5 分钟
const MAX_BODY_SIZE = 16 * 1024 * 1024; // 16MB，ZCode 请求含长 system prompt + 历史
const PROXIED_MODELS = ['glm-5.2', 'sensenova-6.8-flash-lite', 'kimi-k3', 'deepseek-flash'];
// 每模型最大并发。⚠️ 不要设成 API_KEYS.length：商汤是 workspace 级共享限额，
// 5 个 Key 并不等于 5 倍并发能力。实测并发 3 时 4 个不同 Key 在 392ms 内全部 429
// （progress-log 2026-09-16T10:52:09，glm-5.2），429 会被 ZCode 标成 retryable=false
// 直接杀死本轮对话——宁可让对话排队等几秒，也不要并发起跑去打爆共享额度。
const MAX_CONCURRENT_BY_MODEL = {
  'glm-5.2': 1,               // 限额最严，日志里 active:3 即 4 连 429
  'kimi-k3': 1,               // 3 并发图片请求 300ms 内全 429（坑 18）
  'sensenova-6.8-flash-lite': 2,  // 池子宽松，3 并发图片实测全 200
  // DeepSeek V4.1 Flash。⚠️ 上游官方 Model ID 是 deepseek-flash，不是 deepseek-v4.1-flash
  // （后者在 /v1/models 清单里有，但 5 个 key 全 403 code=7 permission_denied_error
  //  "model is not available in the current token plan"，属套餐资格，轮换无解）。
  // 并发给 1 而非 2：首次探测 Key1 就撞 EndpointTPMExceeded，TPM 预算比 glm-5.2 还紧；
  // 观察运行一段时间确认额度充裕后再放宽。
  'deepseek-flash': 1,
};
// 每模型最大重试次数（默认 MAX_RETRIES）。
// deepseek-flash 的 429 是 RateLimitExceeded.EndpointRPMExceeded / EndpointTPMExceeded，
// 属端点级共享限额，5 个 key 共用同一个令牌桶——轮换 key 拿不到新的额度。
// 实测一次客户端请求经默认 5 次重试在 0.72 秒内打出 5 次上游调用，全部 429，
// 等于把 60 秒的窗口瞬间烧空，还额外多冷 4 个 key，成功率 0%。
// 降到 1：最多 2 次上游调用，靠 60 秒冷却自然轮换 key 即可。
const MAX_RETRIES_BY_MODEL = {
  'deepseek-flash': 1,
};
const QUEUE_WAIT_TIMEOUT_MS = 240000; // 排队超时 4 分钟，让 ZCode 多等等
const HEARTBEAT_INTERVAL = 10000; // 自检间隔 10 秒

// DNS 缓存
let cachedIps = [];
let dnsCacheTime = 0;
function resolveHost() {
  return new Promise((resolve) => {
    const now = Date.now();
    if (cachedIps.length > 0 && now - dnsCacheTime < 300000) { resolve(cachedIps); return; }
    dns.resolve4(SENSENOVA_HOST, (err, addresses) => {
      if (err) { if (cachedIps.length > 0) resolve(cachedIps); else resolve(['127.0.0.1']); return; }
      cachedIps = addresses;
      dnsCacheTime = now;
      resolve(addresses);
    });
  });
}
// 预热 DNS
resolveHost();

// 判断上游是否"额度耗尽"（区别于 TPM 速率限制）。
// 上游已知三种额度耗尽信号：insufficient_quota、token plan entitlement exhausted、quota exceeded
// TPM 速率限制（ModelAccountTpmRateLimitExceeded / inference tpm exhausted）不匹配，走短冷却分支
function isQuotaExhausted(buf) {
  return /insufficient_quota|entitlement exhausted|quota exceeded/i.test(buf || '');
}
// 判断上游是否"持续型限流"（区别于短窗口 TPM 速率限制）。
// RPM 请求数限流（ModelAccountRpmRateLimitExceeded / inference exceeds tpm/rpm limit）
// 实测等 65 秒仍未恢复；若走 60 秒短冷却，3456 会反复拿同一必 429 的请求逐个打空全部 key
function isPersistentRateLimit(buf) {
  return /RpmRateLimitExceeded|exceeds tpm\/rpm limit/i.test(buf || '');
}

// 每个模型的独立状态
function createModelState(modelName) {
  return {
    queue: [],
    activeCount: 0,
    maxConcurrent: MAX_CONCURRENT_BY_MODEL[modelName] || 2,
    allKeysCooldownUntil: 0,
    keyIndex: 0,
    lastQueueChange: Date.now(),
    keyStates: API_KEYS.map(() => ({ busy: false, lastRequestTime: 0, last429Time: 0, streak429: 0 })),
  };
}
const models = {};
for (const m of PROXIED_MODELS) models[m] = createModelState(m);

const CLOSE_HANDLER_SYM = Symbol('closeHandler');

// 处理进度日志（排查卡死）：记录请求在代理内部的流转
function progressLog(ms, modelName, keyIdx, stage, extra) {
  try {
    fs.appendFileSync('C:/Users/ban/.zcode/workspace/default/progress-log.txt',
      `[${new Date().toISOString()}] ${modelName} Key${keyIdx + 1} ${stage} ${extra || ''} (queue:${ms.queue.length} active:${ms.activeCount})\n`);
  } catch (e) {}
}

function getKey(ms) {
	  const now = Date.now();
	  if (now < ms.allKeysCooldownUntil) return -1;
	  for (let i = 0; i < API_KEYS.length; i++) {
	    const idx = (ms.keyIndex + i) % API_KEYS.length;
	    const ks = ms.keyStates[idx];
	    if (!ks.busy && (now - ks.last429Time) > 60000) {
	      ms.keyIndex = (idx + 1) % API_KEYS.length;
	      return idx;
	    }
	  }
	  // 计算最短等待时间。处理配额耗尽未来时间戳（last429Time 在未来）：
	  // 对这些 key，等待时间 = last429Time - now（正数，未来还要等多久）
	  // 对正常冷却的 key，等待时间 = 60000 - (now - last429Time)
	  //
	  // ⚠️ busy 的 key 不参与计算。旧逻辑把 busy 当作"60 秒后可用"并写进
	  // allKeysCooldownUntil，于是"3 个 key 在忙 + 2 个在冷却"会被判定成
	  // 全模型锁 61 秒——而忙碌的 key 其实几秒后就释放了。多对话并发时
	  // 这个全局锁反复触发，结果是只有已经建立好流的对话能继续跑，
	  // 其余对话一律拿到"所有 Key 均被限流"。这就是多对话只有一个能用的直接原因。
	  // busy 的情况交给 processModelQueue 的 500ms 轮询自然等释放，不设全局锁。
	  const waits = API_KEYS.map((_, i) => {
	    const ks = ms.keyStates[i];
	    if (ks.busy) return null; // 在途请求：等它自然结束，不猜测时长
	    const diff = now - ks.last429Time;
	    if (diff < 0) return -diff; // 配额耗尽未来锁：返回剩余等待时间
	    return Math.max(60000 - diff, 0); // 正常冷却剩余
	  }).filter(w => w !== null);
	  if (waits.length > 0) {
	    const minWait = Math.min(...waits);
	    if (minWait > 0) {
	      // 限制最大冷却时间到 120 秒，防止配额耗尽未来时间戳导致全局锁死
	      const cappedWait = Math.min(Math.max(minWait, 1000), 120000);
	      ms.allKeysCooldownUntil = now + cappedWait + 1000;
	    }
	  }
	  return -1;
	}

function processModelQueue(ms) {
  while (ms.activeCount < ms.maxConcurrent && ms.queue.length > 0) {
    ms.lastQueueChange = Date.now();
    const ki = getKey(ms);
    if (ki === -1) { setTimeout(() => processModelQueue(ms), 500); return; }
    processRequest(ms, ki);
  }
}

function cleanupRetry(ms, ak, forceTimer, res) {
  if (forceTimer) clearTimeout(forceTimer);
  if (res[CLOSE_HANDLER_SYM]) {
    res.removeListener('close', res[CLOSE_HANDLER_SYM]);
    delete res[CLOSE_HANDLER_SYM];
  }
  ms.keyStates[ak].busy = false;
  ms.activeCount = Math.max(0, ms.activeCount - 1);
}

function processRequest(ms, ki) {
  const item = ms.queue.shift();
  if (!item) { ms.activeCount--; return; }
  if (item.queueTimer) clearTimeout(item.queueTimer);
  ms.activeCount++;
  const ak = item.keyIdx !== undefined ? item.keyIdx : ki;
  ms.keyStates[ak].busy = true;
  ms.keyStates[ak].lastRequestTime = Date.now();
  const { req, res, body, retryCount = 0, modelName } = item;
  const key = API_KEYS[ak];
  let cleanedUp = false, proxyReq = null, forceTimer = null;
  progressLog(ms, modelName, ak, 'start', `retry:${retryCount}`);

  function safeRelease() {
    if (cleanedUp) return;
    cleanedUp = true;
    if (forceTimer) clearTimeout(forceTimer);
    if (res[CLOSE_HANDLER_SYM]) {
      res.removeListener('close', res[CLOSE_HANDLER_SYM]);
      delete res[CLOSE_HANDLER_SYM];
    }
    ms.activeCount--;
    ms.keyStates[ak].busy = false;
    ms.activeCount = Math.max(0, ms.activeCount);
    processModelQueue(ms);
  }

  // 客户端断开 → 清理
  const closeHandler = () => {
    if (forceTimer) clearTimeout(forceTimer);
    if (!cleanedUp) {
      if (proxyReq) proxyReq.destroy();
      safeRelease();
    }
  };
  res[CLOSE_HANDLER_SYM] = closeHandler;
  res.once('close', closeHandler);

  forceTimer = setTimeout(() => {
    if (!cleanedUp) {
      console.log(`[TIMEOUT] [${modelName}] 请求超时，强制释放 Key${ak + 1}`);
      if (proxyReq) proxyReq.destroy();
      if (!res.headersSent) {
        try { res.writeHead(504, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ error: { message: '上游超时', type: 'proxy_timeout' } })); } catch (e) {}
      }
      safeRelease();
    }
  }, REQUEST_TIMEOUT_MS + 5000);

  proxyReq = https.request({
    hostname: SENSENOVA_HOST, path: '/v1/chat/completions', method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${key}`, 'Content-Length': Buffer.byteLength(body) },
    timeout: REQUEST_TIMEOUT_MS,
  }, (proxyRes) => {
    if (cleanedUp) { proxyRes.resume(); return; }
    progressLog(ms, modelName, ak, 'upstream-resp', proxyRes.statusCode);

    // 429：读取响应体判断配额/速率，标记 key 冷却，换下一个 key 轮换
    if (proxyRes.statusCode === 429) {
      console.log(`[429] [${modelName}] Key${ak + 1} 返回 429`);
      let bodyBuf = '';
      proxyRes.on('data', (chunk) => { bodyBuf += chunk; });
      proxyRes.on('end', () => {
        // 判断配额耗尽（长冷却）vs 持续型限流（中长冷却）vs TPM 短窗口（60 秒）
        const isQuota = isQuotaExhausted(bodyBuf);
        const isPersistent = !isQuota && isPersistentRateLimit(bodyBuf);
        // 连续 429 计数（成功一次即清零）。"exceeds tpm/rpm limit" 这个报文
        // 在两种情况下都会出现：多对话并发造成的瞬时超额（60 秒窗口内自愈），
        // 以及模型级额度耗尽（永久）。报文本身分不清，所以首次按 60 秒短窗口处理，
        // 同一个 key 连续 3 次仍 429 才升级为 5 分钟长冷却。
        // 旧逻辑首次命中就锁 5 分钟：并发突发时 4 个 key 400ms 内全 429，
        // 池子瞬间被锁掉 5 分钟，等于把突发误判成额度耗尽。
        ms.keyStates[ak].streak429++;
        const streak = ms.keyStates[ak].streak429;
        // 未来时间戳 = 实际冷却时间 - 60 秒（getKey 要求 last429Time 距今超 60 秒才放行）
        ms.keyStates[ak].last429Time = isQuota ? (Date.now() + 9 * 60 * 1000)
          : (isPersistent && streak >= 3) ? (Date.now() + 4 * 60 * 1000)
          : Date.now();
        console.log(`[429] [${modelName}] Key${ak + 1} ${isQuota ? '配额耗尽冷10分' : (isPersistent && streak >= 3) ? `持续限流冷5分(连续${streak}次)` : `限流冷60s(连续${streak}次)`}`);
        if (retryCount < (MAX_RETRIES_BY_MODEL[modelName] || MAX_RETRIES)) {
          // 换 key 前先检查是否还有可用 key：全部 key 都在冷却时不要 unshift 排队干等
          // （否则 getKey 永远返回 -1，请求挂到客户端超时，坑 3/坑 9 教训）
          if (getKey(ms) === -1) {
            cleanupRetry(ms, ak, forceTimer, res);
            cleanedUp = true;
            if (!res.headersSent) {
              try { res.writeHead(429, { 'Content-Type': 'application/json', 'Retry-After': '30' }); res.end(JSON.stringify({ error: { message: `${modelName} 所有 Key 均被限流，请稍后重试`, type: 'rate_limit', retryAfter: 30 } })); } catch (e) {}
            }
            console.log(`[429] [${modelName}] 所有 Key 冷却中，直接返回 429`);
            return;
          }
          cleanupRetry(ms, ak, forceTimer, res);
          cleanedUp = true;
          // 不指定 keyIdx → getKey 自动跳过冷却的 key，轮换到下一个可用 key
          ms.queue.unshift({ req, res, body, retryCount: retryCount + 1, modelName, queueTimer: null });
          processModelQueue(ms);
          return;
        }
        cleanupRetry(ms, ak, forceTimer, res);
        cleanedUp = true;
        if (!res.headersSent) {
          try { res.writeHead(429, { 'Content-Type': 'application/json', 'Retry-After': '30' }); res.end(JSON.stringify({ error: { message: `${modelName} 繁忙，请稍后重试`, type: 'rate_limit', retryAfter: 30 } })); } catch (e) {}
        }
        console.log(`[429] [${modelName}] 重试耗尽，返回 429`);
      });
      return;
    }

    // 400：读响应体判断是否配额用完
    if (proxyRes.statusCode === 400) {
      let bodyBuf = '';
      proxyRes.on('data', (chunk) => { bodyBuf += chunk; });
      proxyRes.on('end', () => {
        if (isQuotaExhausted(bodyBuf)) {
          console.log(`[QUOTA] [${modelName}] Key${ak + 1} 额度用完，冷却 30 分钟，换 key 轮换`);
          ms.keyStates[ak].last429Time = Date.now() + 29 * 60 * 1000;
          if (retryCount < MAX_RETRIES) {
            cleanupRetry(ms, ak, forceTimer, res);
            cleanedUp = true;
            // 不指定 keyIdx → getKey 自动跳过配额耗尽的 key
            ms.queue.unshift({ req, res, body: item.body, retryCount: retryCount + 1, modelName, queueTimer: null });
            processModelQueue(ms);
          } else {
            if (!res.headersSent) { try { res.writeHead(502, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ error: { message: `${modelName} 所有 Key 配额用完`, type: 'quota_exceeded' } })); } catch (e) {} }
            cleanupRetry(ms, ak, forceTimer, res);
            cleanedUp = true;
          }
          return;
        }
        if (!res.headersSent) {
          try {
            const logLine = `[400] ${new Date().toISOString()}\n请求体: ${(body||'').slice(0,500)}\n上游: ${bodyBuf.slice(0,500)}\n\n`;
            fs.appendFileSync('C:/Users/ban/.zcode/workspace/default/400-log.txt', logLine);
            res.writeHead(400, { 'Content-Type': 'application/json' });
            res.end(bodyBuf);
          } catch (e) {}
        }
        safeRelease();
      });
      return;
    }

    // 正常响应：管道转发。只要拿到了非 429 的上游响应，说明这个 key 没被限流，
    // 连续 429 计数清零（否则一次突发会永久把该 key 定成"持续限流"）。
    ms.keyStates[ak].streak429 = 0;
    if (!res.headersSent) { try { res.writeHead(proxyRes.statusCode, proxyRes.headers); } catch (e) { safeRelease(); return; } }
    proxyRes.pipe(res);
    proxyRes.on('end', safeRelease);
    proxyRes.on('error', () => safeRelease());
  });

  proxyReq.on('timeout', () => {
    proxyReq.destroy();
    if (!cleanedUp) {
      if (!res.headersSent) { try { res.writeHead(504, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ error: { message: '上游超时', type: 'proxy_timeout' } })); } catch (e) {} }
      safeRelease();
    }
  });

  proxyReq.on('error', (err) => {
    if (err.code === 'ECONNRESET' && cleanedUp) return;
    if (!cleanedUp) {
      if (!res.headersSent) { try { res.writeHead(502, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ error: { message: '代理错误', type: 'proxy_error' } })); } catch (e) {} }
      safeRelease();
    }
  });

  proxyReq.write(body);
  proxyReq.end();
}

// 非代理模型直接透传
function passthrough(req, res, body) {
  const options = {
    hostname: SENSENOVA_HOST, path: '/v1/chat/completions', method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': req.headers.authorization || '' },
    timeout: REQUEST_TIMEOUT_MS,
  };
  let cleanedUp = false, proxyReq = null;
  req.on('close', () => { cleanedUp = true; if (proxyReq) proxyReq.destroy(); if (!res.destroyed) res.destroy(); });
  proxyReq = https.request(options, (proxyRes) => {
    if (cleanedUp) { proxyRes.resume(); return; }
    try { res.writeHead(proxyRes.statusCode, proxyRes.headers); } catch (e) { return; }
    proxyRes.pipe(res);
    proxyRes.on('error', () => { if (!res.destroyed) res.destroy(); });
  });
  proxyReq.on('timeout', () => { proxyReq.destroy(); if (!cleanedUp && !res.headersSent) { try { res.writeHead(504, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ error: { message: '上游超时' } })); } catch (e) {} } });
  proxyReq.on('error', (err) => { if (!cleanedUp && !res.headersSent) { try { res.writeHead(502, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ error: err.message })); } catch (e) {} } });
  if (body) proxyReq.write(body);
  proxyReq.end();
}

// HTTP 服务
function handleRequest(req, res) {
  // 全局请求超时：30 秒内没响应就返回超时，防止卡死
  const globalTimer = setTimeout(() => {
    if (!res.headersSent) {
      try { 
        res.writeHead(504, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: { message: '请求超时', type: 'global_timeout' } }));
      } catch (e) {}
    }
    if (!req.destroyed) req.destroy();
  }, 240000); // 4 分钟全局超时（配合排队等待）
  // 拦截 writeHead/end，发送响应头时清除全局超时，并记录请求日志
  const origWriteHead = res.writeHead.bind(res);
  res.writeHead = function() {
    clearTimeout(globalTimer);
    if (req.url && req.url.includes('/v1/chat/completions')) {
      try {
        fs.appendFileSync('C:/Users/ban/.zcode/workspace/default/req-log.txt',
          `[${new Date().toISOString()}] ${req.method} ${req.url} -> ${arguments[0]}\n`);
      } catch (e) {}
    }
    return origWriteHead(...arguments);
  };

  if (req.url === '/health') {
    const info = {};
    for (const m of PROXIED_MODELS) {
      const ms = models[m];
      info[m] = {
        queueLength: ms.queue.length,
        processing: ms.activeCount,
        maxConcurrent: ms.maxConcurrent,
        cooldown: Math.max(0, Math.round((ms.allKeysCooldownUntil - Date.now()) / 1000)),
        keys: ms.keyStates.map((ks, i) => {
          const d = Date.now() - ks.last429Time;
          return {
            key: i + 1, busy: ks.busy, streak429: ks.streak429,
            // 未来时间戳（配额耗尽/长冷却）显示剩余秒数，避免负数误导
            last429: !ks.last429Time ? 'never' : (d < 0 ? `lock ${Math.round(-d / 1000)}s left` : `${Math.round(d / 1000)}s ago`),
          };
        }),
      };
    }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ status: 'ok', uptime: Math.round(process.uptime()), models: info }));
    return;
  }

  if (!req.url.includes('/v1/chat/completions')) { passthrough(req, res, ''); return; }

  let body = '', bodySize = 0, reqEnded = false, reqCleaned = false;

  function cleanupReq() {
    if (reqCleaned) return;
    reqCleaned = true;
    req.removeListener('data', onData);
    req.removeListener('end', onEnd);
    req.removeListener('close', onReqClose);
    req.removeListener('error', onReqError);
  }

  function onData(chunk) {
    bodySize += chunk.length;
    if (bodySize > MAX_BODY_SIZE) { console.log('[413] body 超限:', bodySize); req.destroy(); if (!res.headersSent) { try { res.writeHead(413); res.end('{}'); } catch (e) {} } return; }
    body += chunk;
  }

  function onEnd() {
    if (req.destroyed || reqCleaned) return;
    reqEnded = true;
    cleanupReq();
    let parsed = null;
    try { parsed = JSON.parse(body); } catch (e) {}
    // 记录请求摘要
    try {
      fs.appendFileSync('C:/Users/ban/.zcode/workspace/default/req-log.txt',
        `  body: ${JSON.stringify(parsed||{}).slice(0,200)}\n`);
    } catch (e) {}
    const modelName = parsed && parsed.model;
    const ms = modelName && models[modelName];
    if (!ms) { passthrough(req, res, body); return; }
    if (ms.queue.length >= MAX_QUEUE_SIZE) {
      try { res.writeHead(503, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ error: { message: '队列已满', type: 'queue_full' } })); } catch (e) {}
      return;
    }
    // 排队超时保护
    const queueTimer = setTimeout(() => {
      if (!res.headersSent) {
        try { res.writeHead(504, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ error: { message: '排队超时', type: 'queue_timeout' } })); } catch (e) {}
      }
    }, QUEUE_WAIT_TIMEOUT_MS);
    ms.queue.push({ req, res, body, retryCount: 0, modelName, queueTimer });
    processModelQueue(ms);
  }

  function onReqClose() {
    if (!reqEnded && !reqCleaned) {
      cleanupReq();
      try { fs.appendFileSync('C:/Users/ban/.zcode/workspace/default/req-log.txt', `  [close] 未完成请求，bodySize: ${bodySize}\n`); } catch (e) {}
      if (!res.headersSent) { try { res.destroy(); } catch (e) {} }
    }
  }

  function onReqError(err) {
    try { fs.appendFileSync('C:/Users/ban/.zcode/workspace/default/req-log.txt', `  [reqError] ${err && err.message} bodySize: ${bodySize}\n`); } catch (e) {}
    // 客户端中止请求：不返回 400（ZCode 会显示"Provider rejected"），直接安全关闭
    cleanupReq();
    if (!res.destroyed) { try { res.destroy(); } catch (e) {} }
  }

  req.on('end', onEnd);
  req.on('data', onData);
  req.on('close', onReqClose);
  req.on('error', onReqError);
}

const server = http.createServer((req, res) => handleRequest(req, res));
// 显式处理 Expect: 100-continue（ZCode 发送大请求体时使用，不处理会导致请求超时中止）
server.on('checkContinue', (req, res) => {
  try { fs.appendFileSync('C:/Users/ban/.zcode/workspace/default/req-log.txt', `[checkContinue] ${req.url} expect:${req.headers.expect}\n`); } catch (e) {}
  res.writeContinue();
  handleRequest(req, res);
});

// 全局异常保护
process.on('uncaughtException', (err) => {
  console.error('[GLM] 未捕获异常:', err.message);
});
process.on('unhandledRejection', (err) => {
  console.error('[GLM] 未处理 Promise 拒绝:', err?.message || err);
});

server.on('error', (err) => { if (err.code === 'EADDRINUSE') process.exit(0); console.error('代理启动失败:', err.message); process.exit(1); });
server.listen(PORT, '127.0.0.1', () => {
  console.log('========================================');
  console.log('  SenseNova 智能限流代理');
  console.log('========================================');
  console.log(`  端口: ${PORT}`);
  console.log(`  API Key: ${API_KEYS.length} 个`);
  console.log(`  代理模型: ${PROXIED_MODELS.join(', ')}`);
  console.log(`  冷却策略: 各模型独立冷却`);
  console.log(`  队列上限: ${MAX_QUEUE_SIZE}`);
  console.log('========================================');
});

// 心跳自检 + DNS 缓存刷新 + 队列积压检测
let lastHeartbeat = Date.now();
setInterval(() => {
  const now = Date.now();
  const elapsed = now - lastHeartbeat;
  lastHeartbeat = now;
  if (elapsed > 30000) {
    console.error(`[心跳] 事件循环卡死 ${Math.round(elapsed / 1000)} 秒，主动退出`);
    process.exit(1);
  }
  // 队列积压检测：队列 60 秒没清空说明请求处理卡住，自杀让看门狗重启
  for (const m of PROXIED_MODELS) {
    const ms = models[m];
    if (ms.queue.length > 0 && (now - ms.lastQueueChange) > 60000) {
      console.error(`[自检] ${m} 队列积压 ${ms.queue.length} 个超 60 秒无变化，主动退出`);
      process.exit(1);
    }
  }
  // 定期刷新 DNS 缓存
  if (now - dnsCacheTime > 300000) resolveHost();
}, HEARTBEAT_INTERVAL);