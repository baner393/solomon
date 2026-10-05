/**
 * DeepSeek 多 Key 轮换代理 - 极简版
 *
 * ⚠️ 修改前请阅读：C:\Users\ban\.zcode\v2\PROXY-GUIDE.md
 *
 * 纯轮换转发，无队列、无重试、无多余监听器
 * 一个 Key 限流 → 换下一个
 * 流式/非流式直通
 */
const http = require('http');
const https = require('https');
const PORT = 3457;
const SENSENOVA_HOST = 'token.sensenova.cn';
const API_KEYS = [
  'SK_YOUR_KEY_1',
  'SK_YOUR_KEY_1',
  'SK_YOUR_KEY_1',
  'SK_YOUR_KEY_1',
  'SK_YOUR_KEY_1',
];

let keyCooldowns = API_KEYS.map(() => 0);
let keyIndex = 0;

function pickKey() {
  const now = Date.now();
  for (let i = 0; i < API_KEYS.length; i++) {
    const idx = (keyIndex + i) % API_KEYS.length;
    if (now >= keyCooldowns[idx]) {
      keyIndex = (idx + 1) % API_KEYS.length;
      return idx;
    }
  }
  return -1;
}

const server = http.createServer((req, res) => {
  if (req.url === '/health') {
    const now = Date.now();
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      status: 'ok', uptime: Math.round(process.uptime()),
      keys: API_KEYS.map((_, i) => ({
        key: i + 1,
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
    if (!modelName || !modelName.toLowerCase().includes('deepseek')) {
      res.writeHead(400); res.end('{"error":"not a deepseek model"}');
      return;
    }

    const isStream = parsed && parsed.stream === true;
    const bodyStr = body;
    let retries = 0;
    const maxRetries = API_KEYS.length;

    tryRequest();

    function tryRequest() {
      const ki = pickKey();
      if (ki === -1) {
        const now = Date.now();
        const waits = keyCooldowns.map(t => Math.max(0, t - now));
        const minWait = Math.min(...waits);
        res.writeHead(503, { 'Retry-After': String(Math.ceil(minWait / 1000)) });
        res.end(JSON.stringify({ error: '所有 Key 冷却中' }));
        return;
      }

      const key = API_KEYS[ki];
      let done = false;

      const proxyReq = https.request({
        hostname: SENSENOVA_HOST,
        path: '/v1/chat/completions',
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${key}`,
          'Content-Length': Buffer.byteLength(bodyStr),
        },
      }, (proxyRes) => {
        if (done) { proxyRes.resume(); return; }

        if (proxyRes.statusCode === 429) {
          proxyRes.resume();
          keyCooldowns[ki] = Date.now() + 60000;
          retries++;
          done = true;
          if (retries < maxRetries) { tryRequest(); }
          else { res.writeHead(429); res.end(JSON.stringify({ error: '所有 Key 限流' })); }
          return;
        }

        if (proxyRes.statusCode === 400) {
          let buf = '';
          proxyRes.on('data', c => buf += c);
          proxyRes.on('end', () => {
            if (buf.includes('insufficient_quota') || buf.includes('quota exceeded')) {
              keyCooldowns[ki] = Date.now() + 1800000;
              retries++;
              done = true;
              if (retries < maxRetries) { tryRequest(); }
              else { res.writeHead(502); res.end(JSON.stringify({ error: '所有 Key 额度用完' })); }
              return;
            }
            done = true;
            res.writeHead(400, { 'Content-Type': 'application/json' });
            res.end(buf);
          });
          return;
        }

        done = true;
        if (isStream && proxyRes.statusCode === 200) {
          res.writeHead(200, {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
          });
          proxyRes.pipe(res);
        } else {
          res.writeHead(proxyRes.statusCode, proxyRes.headers);
          proxyRes.pipe(res);
        }
      });

      proxyReq.on('error', (err) => {
        if (done) return;
        done = true;
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
  console.error('[DeepSeek] 异常:', err.message);
});

server.on('error', (err) => {
  if (err.code === 'EADDRINUSE') { process.exit(0); }
  console.error('[DeepSeek] 启动失败:', err.message);
  process.exit(1);
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`DeepSeek 轮换代理已启动 (端口 ${PORT}, ${API_KEYS.length} Key)`);
});