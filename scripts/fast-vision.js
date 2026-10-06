/**
 * fast-vision.js — 识图辅助脚本（备选线路：图床上传 → 公网 URL → 视觉 API）
 *
 * 用途：当 LLM 端点不支持 base64 data URL 形态的 image_url（只接受公网图片 URL）、
 * 或需要绕开端点对 base64 大小的限制时，用本脚本完成识图。
 * 默认（solomon 内置）识图走端点原生 base64（见 llm_client.vision_analyze），
 * 仅在端点不兼容时才需要本脚本：
 *   .env 里设置 FAST_VISION_JS=<本脚本绝对路径> 后，solomon 识图自动切换到本脚本。
 * 也可独立使用：
 *   node fast-vision.js <图片路径>
 *   node fast-vision.js -p "图里有什么文字" <图片路径>
 *   node fast-vision.js -m kimi-k3 <图片路径>      # 指定模型（默认 SENSENOVA_MODEL）
 *   node fast-vision.js --direct <图片路径>        # 强制 base64 直传，跳过图床
 *
 * 端点配置（与 llm_client 同源）：
 *   LLM_BASE_URL（兼容 SENSENOVA_BASE_URL）  默认 http://127.0.0.1:3456/v1
 *   LLM_API_KEY（兼容 SENSENOVA_API_KEY）    默认 proxy
 *   SENSENOVA_MODEL                          默认 sensenova-6.8-flash-lite
 *
 * 图片两条上传路径，按模型自动选择：
 *   图床 URL   —— 默认路径（上传到公共图床换取公网 URL）
 *   base64     —— kimi 系模型强制走这里（官方文档明确只接受 base64，传 URL 会 400）；
 *                 其他模型加 --direct 强制走 base64
 * 支持格式: JPG / JPEG / PNG / WebP
 */
const fs = require('fs');
const http = require('http');
const https = require('https');
const path = require('path');
const { execSync } = require('child_process');

// 端点（与 llm_client 同源：env 优先，本地代理兜底）
const BASE_URL = (process.env.LLM_BASE_URL || process.env.SENSENOVA_BASE_URL || 'http://127.0.0.1:3456/v1').replace(/\/+$/, '');
const API_KEY = process.env.LLM_API_KEY || process.env.SENSENOVA_API_KEY || 'proxy';
const DEFAULT_MODEL = process.env.SENSENOVA_MODEL || 'sensenova-6.8-flash-lite';

// 扩展名 → MIME（只列商汤文档确认支持的四种）
const MIME_BY_EXT = {
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.png': 'image/png',
  '.webp': 'image/webp',
};

// 读本地图片转 base64 Data URL
function toDataUrl(filePath) {
  const absPath = path.resolve(filePath);
  const ext = path.extname(absPath).toLowerCase();
  const mime = MIME_BY_EXT[ext] || 'image/png';
  return `data:${mime};base64,` + fs.readFileSync(absPath).toString('base64');
}

// 上传图片到公共图床，返回公开 URL（尝试多个服务，哪个成功用哪个）
const UPLOAD_SERVICES = [
  // uguu.se: 直接返回原始文件，最稳
  { cmd: (f) => `curl -s --connect-timeout 10 -m 30 -F "files[]=@${f}" "https://uguu.se/upload"` },
  // temp.sh: 备选
  { cmd: (f) => `curl -s --connect-timeout 10 -m 30 -F "file=@${f}" "https://temp.sh/upload"` },
];

function uploadImage(filePath) {
  const absPath = path.resolve(filePath);
  let lastErr = null;
  for (const svc of UPLOAD_SERVICES) {
    try {
      const cmd = svc.cmd(absPath);
      const output = execSync(cmd, { timeout: 35000, encoding: 'utf8', maxBuffer: 1024 * 1024 }).trim();
      // 提取 URL（uguu 返回 JSON，temp.sh 返回纯文本）
      let url = '';
      try {
        const j = JSON.parse(output);
        if (j.success && j.files?.[0]?.url) url = j.files[0].url;
      } catch { url = output; }
      if (url && url.startsWith('http')) return url;
      lastErr = `无效响应: ${output.slice(0, 100)}`;
    } catch (e) { lastErr = e.message; }
  }
  throw new Error(`所有图床上传失败: ${lastErr}`);
}

// kimi 系列模型只接受 base64 图片（官方文档明确），其余模型默认走图床 URL
function usesBase64(model, forceDirect) {
  return forceDirect || /kimi/i.test(model || '');
}

// 调用端点识别一张图片
function describeImage(filePath, prompt, model, forceDirect) {
  return new Promise((resolve) => {
    const absPath = path.resolve(filePath);
    if (!fs.existsSync(absPath)) {
      resolve(`❌ 文件不存在: ${absPath}`);
      return;
    }

    const useBase64 = usesBase64(model, forceDirect);
    let imageUrl;
    try {
      imageUrl = useBase64 ? toDataUrl(absPath) : uploadImage(absPath);
    } catch (e) {
      const hint = useBase64 ? '' : '（图床路径失败，可加 --direct 改走 base64 重试）';
      resolve(`❌ 图片准备失败 (${path.basename(absPath)}): ${e.message}${hint}`);
      return;
    }

    const userText = prompt || '请描述这张图片的内容';
    const body = JSON.stringify({
      model: model || DEFAULT_MODEL,
      messages: [
        {
          role: 'system',
          content: '你是一个视觉识别助手。请用中文简要描述图片中的内容，包括主体、颜色、风格、文字等。回答要简洁准确。'
        },
        {
          role: 'user',
          content: [
            { type: 'text', text: userText },
            { type: 'image_url', image_url: { url: imageUrl } }
          ]
        }
      ],
      stream: false,
      max_tokens: 2000
    });

    const ep = new URL(BASE_URL + '/chat/completions');
    const transport = ep.protocol === 'https:' ? https : http;
    const req = transport.request({
      hostname: ep.hostname,
      port: ep.port || (ep.protocol === 'https:' ? 443 : 80),
      path: ep.pathname,
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${API_KEY}`,
        'Content-Length': Buffer.byteLength(body)
      }
    }, (res) => {
      let data = '';
      res.on('data', c => data += c);
      res.on('end', () => {
        try {
          const j = JSON.parse(data);
          if (j.choices?.[0]?.message?.content) {
            resolve(j.choices[0].message.content);
          } else {
            resolve(`❌ API 错误: ${j.error?.message || JSON.stringify(j).slice(0, 300)}`);
          }
        } catch (e) {
          resolve(`❌ 解析响应失败: ${data.slice(0, 300)}`);
        }
      });
    });
    req.on('error', (e) => resolve(`❌ 网络错误: ${e.message}`));
    req.write(body);
    req.end();
  });
}

async function main() {
  const args = process.argv.slice(2);

  // 解析 --prompt / -p / --model / -m / --direct
  let prompt = null, model = DEFAULT_MODEL, forceDirect = false;
  const filtered = [];
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--prompt' || args[i] === '-p') {
      prompt = args[++i] || '';
    } else if (args[i] === '--model' || args[i] === '-m') {
      model = args[++i] || DEFAULT_MODEL;
    } else if (args[i] === '--direct') {
      forceDirect = true;
    } else {
      filtered.push(args[i]);
    }
  }

  const files = filtered.filter(f => f && !f.startsWith('-'));
  if (files.length === 0) {
    console.log('用法:');
    console.log('  node fast-vision.js <图片路径1> [图片路径2 ...]');
    console.log('  node fast-vision.js --prompt "图里有什么文字" <图片路径>');
    console.log('  node fast-vision.js -p "这个角色是谁" <图1> <图2>');
    console.log('  node fast-vision.js -m kimi-k3 <图片路径>      # 指定模型');
    console.log('  node fast-vision.js --direct <图片路径>          # 强制 base64 直传，跳过图床');
    console.log('');
    console.log('上传路径: 默认走图床 URL；kimi 模型自动改走 base64（只接受 base64，传 URL 会 400）');
    console.log(`端点: ${BASE_URL}（LLM_BASE_URL 可改）`);
    process.exit(1);
  }

  console.error(`[fast-vision] ${files.length} 张图 / ${model} / ${usesBase64(model, forceDirect) ? 'base64' : '图床URL'}...`);

  if (files.length === 1) {
    // 单图: 直接输出
    const result = await describeImage(files[0], prompt, model, forceDirect);
    console.log(result);
  } else {
    // 多图: 并行处理
    const promises = files.map((f, i) =>
      describeImage(f, prompt, model, forceDirect).then(text => `=== 图片${i + 1}: ${path.basename(f)} ===\n${text}`)
    );
    const results = await Promise.all(promises);
    console.log(results.join('\n\n'));
  }
}

main().catch(e => console.error('❌ 错误:', e.message));
