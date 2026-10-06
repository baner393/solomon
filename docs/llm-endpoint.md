# LLM 端点配置

solomon 的 `llm_client` 走 **OpenAI 兼容**协议（`/chat/completions`），因此可接任何
OpenAI 兼容端点。视觉（识图）走同一端点的 `chat/completions`（图片 base64 内联）。

## 推荐配置（云端端点）

```bash
# .env
LLM_BASE_URL=https://token.sensenova.cn/v1     # 任意 OpenAI 兼容端点
LLM_API_KEY=你的密钥
SENSENOVA_MODEL=sensenova-6.8-flash-lite        # 可选，默认此模型；可换端点上任意可用模型
```

## 默认回落（不配 LLM_BASE_URL 时）

```
端点列表：http://127.0.0.1:3456/v1（主）→ http://127.0.0.1:3458/v1（备）
认证：Bearer proxy（固定占位）
```

这是「本地代理」形态：一个运行在你机器上的 OpenAI 兼容代理（如 SenseNova 4Key
轮换），把请求转发到云端并处理多 key 轮换/限流。**代码里零硬编码密钥**。

> 兼容别名：`SENSENOVA_BASE_URL` / `SENSENOVA_API_KEY` 与新名等价（旧环境无需改动）。

## 换成任意 OpenAI 兼容端点

```bash
# 例如 OpenAI
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-your-key

# 或任何本地/自托管端点（vLLM / Ollama / LM Studio / 各类网关）
LLM_BASE_URL=http://127.0.0.1:8000/v1
LLM_API_KEY=whatever
```

要求：
- `POST {base}/chat/completions` 可用（OpenAI 格式协议）
- 图片内容参数（识图需要）：支持 `image_url` 类型的 content part（base64 data URL 形态）
- 模型名：`SENSENOVA_MODEL` env 可配；不支持 `thinking` 参数的端点设 `LLM_OMIT_THINKING=1`
- **网络**：LLM 调用直连端点（`llm_client` 不读 `HTTP_PROXY` 等代理 env）——机器需可直连
  该端点；受限网络请用可直连的云端/国内端点，或自建本地代理形态（默认回落即此形态）

## 识图备选线路

端点不接受 base64 形态（只收公网 URL）时，切换图床上传线路：

```bash
# .env
FAST_VISION_JS=~/solomon/scripts/fast-vision.js   # node ≥18 + curl
```

详见 [DEPLOY.md §辅助方案](../DEPLOY.md#辅助方案llm-能对话但不能识图)。

## 健康检查

`solomon doctor` 会对 `{base}/chat/completions` 发一个 5-token ping 请求，
HTTP 200 即连通（注意不是 `/models`——部分代理不支持该端点）。
doctor 与实际调用读同一份配置，不会出现「doctor 全绿但调用打错端点」。

## 代理故障排查

| 症状 | 原因 | 处理 |
|---|---|---|
| `doctor` 显示 `LLM 端点连通 ✗` | 代理没起 / 端点错 / 网络 | 确认代理进程；`curl -X POST {base}/chat/completions` 手测 |
| 入库时报 `LLM 调用失败` | 密钥错 / 限流 / 超时 | 查代理日志；确认 key 有效；重试（有自动重试） |
| 长 JSON 输出 `content` 为空 | 模型 thinking 空间挤占 max_tokens | 已内置 `thinking: disabled`（云端非 SenseNova 端点设 `LLM_OMIT_THINKING=1`） |
| 识图全部 `❌ 识图失败`，文本正常 | 端点不支持 base64 image_url | 换视觉模型（方案 A）或设 `FAST_VISION_JS`（方案 B） |

## 提示

- 不要提交真实 key 到仓库。`.env` 已 gitignore，`.env.example` 只给占位符。
- 轻量端点（如 flash-lite）适合笔记/标题；重活（五层提炼长 JSON）建议
  同一端点的高能力模型，超时已按调用类型分别放宽（笔记/五层 360s）。
