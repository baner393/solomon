/**
 * SenseNova 6.8 Flash Lite 多 Key 轮换代理 - 极简版
 *
 * 仿照 sensenova-deepseek-proxy.js 的轮换机制（纯轮换、无队列、流式直通）
 * 一个 Key 限流 / 额度耗尽 -> 换下一个 Key
 *
 * 与 deepseek 版的三点差异：
 * 1. 只保留对 sensenova-6.8-flash-lite 有权限的 4 个 Key
 *    （SK_YOUR_KEY_1... / SK_YOUR_KEY_1... 对该模型返回 Forbidden code=16，已排除）
 * 2. 429 先读 body 再决定冷却时长——上游"额度耗尽"返回 429 + quota_exceeded_error，
 *    若按 60s 短冷却处理会在所有 Key 上空转轮满后误报"所有 Key 限流"
 * 3. 401/403 视为无权限，长冷却避免反复消耗配额
 *
 * ⚠️ 修改前请先备份原文件；密钥占位符 SK_YOUR_KEY_* 替换为真实 key
 */
const http = require('http');
const https = require('https');

const PORT = 3458;
const UPSTREAM_HOST = 'token.sensenova.cn';
const TARGET_MODEL = 'sensenova-6.8-flash-lite';

const API_KEYS = [
  'SK_YOUR_KEY_1',
  'SK_YOUR_KEY_1',
  'SK_YOUR_KEY_1',
  'SK_YOUR_KEY_1',
  'SK_YOUR_KEY_1',
];

const RATE_LIMIT_COOLDOWN_MS = 60000;            // 普通限流：60 秒
const QUOTA_EXHAUSTED_COOLDOWN_MS = 30 * 60000;  // 额度耗尽：30 分钟
const FORBIDDEN_COOLDOWN_MS = 60 * 60000;        // 无权限：1 小时
const TIMEOUT_COOLDOWN_MS = 60000;               // 上游超时 / 错误：60 秒（避开刚好抽风的 key）
const REQUEST_TIMEOUT_MS = 60000;                // 上游请求超时上限，防止一个 key 卡死代理
// 2026-09-26 事故修复：上游 200 响应头到达后 done=true，REQUEST_TIMEOUT_MS 不再保护；
// SSE 流建立后挂死 + 客户端 TCP 半开（hermes stale-kill 后 RST 丢失）→ proxyRes 永不
// end/error → keyBusy 永久 true，5 个 key 全部泄漏，整池报"所有 Key 冷却中"。
const STREAM_IDLE_TIMEOUT_MS = 60000;            // 上游流 60s 无数据视为挂死，主动断链放 key
const MAX_BUSY_MS = 10 * 60000;                  // 单 key 连续占用上限，兜底防未知泄漏路径

let keyCooldowns = API_KEYS.map(() => 0);
let keyBusySince = API_KEYS.map(() => 0);
let keyStatus = API_KEYS.map(() => 'idle');
// 在途标记：流式响应可能持续几十秒到几分钟，期间这个 key 必须被占住。
// 没有它，多个对话并发时同一个 key 会被同时发给 2-3 个请求 → 撞 TPM 429
// → 该 key 锁 60 秒 → 重试又撞上别的忙碌 key → 全池在几秒内连锁锁死，
// 表现为"多对话只有一个能用"。3456 里对应的是 keyStates[].busy。
let keyBusy = API_KEYS.map(() => false);
let keyIndex = 0;
let totalRequests = 0;
let totalSuccess = 0;

function releaseKey(ki, status) {
  keyBusy[ki] = false;
  keyBusySince[ki] = 0;
  if (status) keyStatus[ki] = status;
}

// 兜底巡检：busy 超过 MAX_BUSY_MS 强制释放（正常流几秒到几分钟，10 分钟必是泄漏）
setInterval(() => {
  const now = Date.now();
  keyBusySince.forEach((since, i) => {
    if (keyBusy[i] && since && now - since > MAX_BUSY_MS) {
      console.error(`[FlashLite] key#${i + 1} 连续占用超过 ${MAX_BUSY_MS / 60000}min，强制释放（防泄漏）`);
      releaseKey(i, 'busy_timeout_released');
    }
  });
}, 60000).unref();

function pickKey() {
  const now = Date.now();
  for (let i = 0; i < API_KEYS.length; i++) {
    const idx = (keyIndex + i) % API_KEYS.length;
    if (!keyBusy[idx] && now >= keyCooldowns[idx]) {
      keyIndex = (idx + 1) % API_KEYS.length;
      return idx;
    }
  }
  return -1;
}

// 上游额度耗尽返回 429 + quota_exceeded_error；限流也是 429 但 body 不同
function isQuotaExhausted(buf) {
  return /quota_exceeded|entitlement exhausted|insufficient_quota/i.test(buf);
}

const server = http.createServer((req, res) => {
  if (req.url === '/health') {
    const now = Date.now();
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      status: 'ok',
      uptime: Math.round(process.uptime()),
      model: TARGET_MODEL,
      requests: totalRequests,
      success: totalSuccess,
      keys: API_KEYS.map((_, i) => ({
        key: i + 1,
        busy: keyBusy[i],
        busyFor: keyBusy[i] ? Math.round((now - keyBusySince[i]) / 1000) : 0,
        status: keyStatus[i],
        cooldownLeft: Math.max(0, Math.round((keyCooldowns[i] - now) / 1000)),
      })),
    }));
    return;
  }

  if (!req.url.includes('/v1/chat/completions')) {
    res.writeHead(404); res.end();
    return;
  }

  let body = '';
  req.on('data', (chunk) => { body += chunk; });
  req.on('end', () => {
    if (req.destroyed) return;

    let parsed;
    try { parsed = JSON.parse(body); } catch (e) { res.writeHead(400); res.end('{}'); return; }
    const modelName = parsed && parsed.model;
    if (!modelName || !/sensenova|6\.8/i.test(modelName)) {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: `该代理只服务 ${TARGET_MODEL}，收到模型: ${modelName}` }));
      return;
    }

    const isStream = parsed && parsed.stream === true;
    const bodyStr = body;
    let retries = 0;
    const maxRetries = API_KEYS.length;
    totalRequests++;

    tryRequest();

    function tryRequest() {
      const ki = pickKey();
      if (ki === -1) {
        const now = Date.now();
        const minWait = Math.min(...keyCooldowns.map(t => Math.max(0, t - now)));
        const allQuota = keyStatus.every(s => s === 'quota_exhausted');
        const code = allQuota ? 503 : 429;
        const msg = allQuota ? '所有 Key 额度耗尽' : '所有 Key 冷却中';
        res.writeHead(code, {
          'Content-Type': 'application/json',
          'Retry-After': String(Math.ceil(minWait / 1000)),
        });
        res.end(JSON.stringify({ error: msg }));
        return;
      }

      const key = API_KEYS[ki];
      keyBusy[ki] = true; // 占住 key，直到本请求真正结束（含重试前的清理）
      keyBusySince[ki] = Date.now();
      let done = false;

      const proxyReq = https.request({
        hostname: UPSTREAM_HOST,
        path: '/v1/chat/completions',
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${key}`,
          'Content-Length': Buffer.byteLength(bodyStr),
        },
      }, (proxyRes) => {
        if (done) { proxyRes.resume(); return; }

        // 429：必须读 body 区分"限流"与"额度耗尽"
        if (proxyRes.statusCode === 429) {
          let buf = '';
          proxyRes.on('data', (c) => { buf += c; });
          proxyRes.on('end', () => {
            done = true;
            keyBusy[ki] = false;
            const quota = isQuotaExhausted(buf);
            keyStatus[ki] = quota ? 'quota_exhausted' : 'rate_limited';
            keyCooldowns[ki] = Date.now() + (quota ? QUOTA_EXHAUSTED_COOLDOWN_MS : RATE_LIMIT_COOLDOWN_MS);
            retries++;
            if (retries < maxRetries) { tryRequest(); }
            else {
              const allQuota = keyStatus.every(s => s === 'quota_exhausted');
              res.writeHead(allQuota ? 503 : 429, { 'Content-Type': 'application/json' });
              res.end(JSON.stringify({
                error: allQuota ? '所有 Key 额度耗尽' : '所有 Key 限流',
                upstream: buf.slice(0, 200),
              }));
            }
          });
          return;
        }

        // 401/403：无权限，长冷却避免反复消耗
        if (proxyRes.statusCode === 401 || proxyRes.statusCode === 403) {
          let buf = '';
          proxyRes.on('data', (c) => { buf += c; });
          proxyRes.on('end', () => {
            done = true;
            keyBusy[ki] = false;
            keyStatus[ki] = 'forbidden';
            keyCooldowns[ki] = Date.now() + FORBIDDEN_COOLDOWN_MS;
            retries++;
            if (retries < maxRetries) { tryRequest(); }
            else {
              res.writeHead(403, { 'Content-Type': 'application/json' });
              res.end(JSON.stringify({ error: '所有 Key 无权限', upstream: buf.slice(0, 200) }));
            }
          });
          return;
        }

        // 400：也可能携带额度错误
        if (proxyRes.statusCode === 400) {
          let buf = '';
          proxyRes.on('data', (c) => { buf += c; });
          proxyRes.on('end', () => {
            keyBusy[ki] = false; // 两条分支（换 key 重试 / 直接透传）都走到这里，先放掉 key
            if (isQuotaExhausted(buf)) {
              done = true;
              keyStatus[ki] = 'quota_exhausted';
              keyCooldowns[ki] = Date.now() + QUOTA_EXHAUSTED_COOLDOWN_MS;
              retries++;
              if (retries < maxRetries) { tryRequest(); }
              else {
                res.writeHead(502, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ error: '所有 Key 额度用完', upstream: buf.slice(0, 200) }));
              }
              return;
            }
            done = true;
            res.writeHead(400, { 'Content-Type': 'application/json' });
            res.end(buf);
          });
          return;
        }

        done = true;
        // 响应还在流式传输中，key 必须一直占住到上游流彻底结束。
        // 上游 200 后流可能挂死（SSE 建立后一个 chunk 都不来）：此时 REQUEST_TIMEOUT_MS
        // 已不保护（done=true），必须用流级空闲超时主动断链放 key（2026-09-26 泄漏事故）。
        let idleTimer = null;
        const armIdle = () => {
          clearTimeout(idleTimer);
          idleTimer = setTimeout(() => {
            console.error(`[FlashLite] key#${ki + 1} 上游流 ${STREAM_IDLE_TIMEOUT_MS / 1000}s 无数据，判定挂死并断链`);
            keyStatus[ki] = 'stream_stalled';
            proxyReq.destroy(new Error('upstream stream stalled'));
            res.destroy();
          }, STREAM_IDLE_TIMEOUT_MS);
        };
        armIdle();
        proxyRes.on('data', () => armIdle()); // 有数据就续期
        proxyRes.on('end', () => { clearTimeout(idleTimer); releaseKey(ki); });
        proxyRes.on('error', () => { clearTimeout(idleTimer); releaseKey(ki, 'error'); });
        // 客户端断开（含 hermes stale-kill 后的半开 TCP）→ 立即断上游并放 key
        res.on('close', () => {
          clearTimeout(idleTimer);
          if (!proxyRes.readableEnded && !proxyRes.destroyed) {
            proxyReq.destroy();
          }
          releaseKey(ki);
        });
        if (proxyRes.statusCode === 200) {
          keyStatus[ki] = 'ok';
          totalSuccess++;
          if (isStream) {
            res.writeHead(200, {
              'Content-Type': 'text/event-stream',
              'Cache-Control': 'no-cache',
              'Connection': 'keep-alive',
              'X-Accel-Buffering': 'no',
            });
            proxyRes.pipe(res);
          } else {
            res.writeHead(200, proxyRes.headers);
            proxyRes.pipe(res);
          }
        } else {
          res.writeHead(proxyRes.statusCode, proxyRes.headers);
          proxyRes.pipe(res);
        }
      });

      proxyReq.setTimeout(REQUEST_TIMEOUT_MS, () => {
        if (!done) { done = true; keyBusy[ki] = false; proxyReq.destroy(new Error('ETIMEDOUT')); }
      });

      proxyReq.on('error', (err) => {
        if (done) return;
        done = true;
        keyBusy[ki] = false;
        keyStatus[ki] = 'error';
        keyCooldowns[ki] = Date.now() + TIMEOUT_COOLDOWN_MS; // 上游抽风，避开 60 秒
        retries++;
        if (retries < maxRetries) { tryRequest(); }
        else { try { res.writeHead(502); res.end(JSON.stringify({ error: err.message })); } catch (e) {} }
      });

      proxyReq.write(bodyStr);
      proxyReq.end();
    }
  });
});

process.on('uncaughtException', (err) => {
  console.error('[FlashLite] 异常:', err.message);
});

server.on('error', (err) => {
  if (err.code === 'EADDRINUSE') { process.exit(0); }
  console.error('[FlashLite] 启动失败:', err.message);
  process.exit(1);
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`SenseNova FlashLite 轮换代理已启动 (端口 ${PORT}, ${API_KEYS.length} Key)`);
});
