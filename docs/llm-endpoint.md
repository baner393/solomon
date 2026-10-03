# LLM 端点配置

solomon 的 `llm_client` 走 **OpenAI 兼容**协议（`/chat/completions`），因此可接任何
OpenAI 兼容端点。视觉（识图）走同一端点的 `chat/completions`（图片 base64 内联）。

## 默认配置

```
SENSENOVA_BASE_URL=http://127.0.0.1:3458/v1
SENSENOVA_API_KEY=proxy
```

这是「本地代理」形态：一个运行在你机器上的 OpenAI 兼容代理（如 SenseNova 4Key
轮换），把请求转发到云端并处理多 key 轮换/限流。**好处：代码里零硬编码密钥**，
`.env` 只写 `proxy` 占位。

## 换成任意 OpenAI 兼容端点

```bash
# 例如 OpenAI
SENSENOVA_BASE_URL=https://api.openai.com/v1
SENSENOVA_API_KEY=sk-your-key

# 或任何本地/自托管端点（vLLM / Ollama / LM Studio / 各类网关）
SENSENOVA_BASE_URL=http://127.0.0.1:8000/v1
SENSENOVA_API_KEY=whatever
```

要求：
- `POST {base}/chat/completions` 可用（OpenAI 格式协议）
- 图片内容参数（识图需要）：支持 `image_url` 类型的 content part
- 模型名：代码默认 `sensenova-6.8-flash-lite`，可用 `.env` 指认？

> ⚠️ 模型名目前写在 `llm_client.py` 的默认值里（`sensenova-6.8-flash-lite`）。
> 若换端点且模型名不同，当前版本需在代码里改（见 `llm_client.py::_DEFAULT_MODEL` 附近）。
> 计划后续收口到配置项。

## 健康检查

`solomon doctor` 会对 `{base}/chat/completions` 发一个 5-token ping 请求，
HTTP 200 即连通（注意不是 `/models`——部分代理不支持该端点）。

## 代理故障排查

| 症状 | 原因 | 处理 |
|---|---|---|
| `doctor` 显示 `LLM 端点连通 ✗` | 代理没起 / 端点错 / 网络 | 确认代理进程；`curl -X POST {base}/chat/completions` 手测 |
| 入库时报 `LLM 调用失败` | 密钥错 / 限流 / 超时 | 查代理日志；确认 key 有效；重试（有自动重试） |
| 长 JSON 输出 `content` 为空 | 模型 thinking 空间挤占 max_tokens | 已内置 `thinking: disabled`；仍看到则提 issue |

## 提示

- 不要提交真实 key 到仓库。`.env` 已 gitignore，`.env.example` 只给占位符。
- 轻量端点（如 flash-lite）适合笔记/标题；重活（五层提炼长 JSON）建议
  同一端点的高能力模型，超时已按调用类型分别放宽（笔记/五层 360s）。