# 故障排查

## 环境类

### `solomon doctor` 报依赖缺失
```bash
pip install yt-dlp jieba sherpa-onnx opencv-python-headless
# ffmpeg/ffprobe: apt install ffmpeg 或 brew install ffmpeg
```

### LLM 端点连通 ✗
见 [LLM 端点配置](llm-endpoint.md#代理故障排查)。先手测：
```bash
curl -X POST http://127.0.0.1:3458/v1/chat/completions \
  -H "Authorization: Bearer proxy" -H "Content-Type: application/json" \
  -d '{"model":"sensenova-6.8-flash-lite","messages":[{"role":"user","content":"hi"}],"max_tokens":5}'
```

### vault 不存在或不可写
```
solomon init        # 建结构
chmod -R u+w ~/.solomon   # 权限
```

## 入库类

### 视频下载失败
- B站：yt-dlp 兜底走 B站 API（自动）。`HTTP_PROXY` 需指向可访问代理。
- 需要登录态字幕：设 `BILI_COOKIE`（完整 Cookie 串，可选）。
- YouTube：已内置 mweb 客户端重试与 format 18 兜底；网络问题先查代理。

### 字幕获取为 0 / 转写很慢
- B站 AI 字幕并非全量覆盖（无 CC 且未 ASR 的视频没有），会落到 sherpa-onnx 转写。
- 转写只走 sherpa-onnx（无 torch，CPU 高效）。>25min 视频自动分段。
- ⚠️ 不要在该环境装/跑 funasr/torch 转写（WSL 低内存下 OOM）。

### 入库后查询 0 命中
```bash
solomon index build     # 重建 FTS5 索引（旧手工流程末跑索引的常见症状）
solomon ask "关键词" --raw   # 看检索命中再调 LLM
```

### 图片没入库 / 断链
- `--images` 才跑配图版；纯文字版默认跳过关键帧/识图。
- 入库后 `solomon verify` 查断链；五层页图片引用按真实文件名校验，幻觉文件名自动丢弃。

### 长视频五层提炼 JSON 截断
已内置：max_tokens 8192 + prompt 限制篇幅。仍截断则重跑该视频（断点续跑只重生成内容层）。

## 问答类

### `ask` 返回空 / 只回检索结果
- `--raw` 是刻意行为（只看检索）。
- 完整回答需 LLM 可达；看 `doctor` 的 LLM 连通项。
- 语义缓存：KB 更新后指纹变自动失效；同问 24h 内秒回属正常。

### 回答质量不好
- top-N 默认 5 页 ×3000 字，可用 `--top` 调；检索漏词看 `--raw` 的实际命中。
- 中文问句拆词：jieba 过滤停用词取 ≥3 字词，太口语的问句会拆不出词（回落整串）。

## 运维类

### 断点续跑 vs --force
- 默认：产物存在则跳过（下载/字幕/关键帧/识图/笔记/raw/五层）。
- `--force`：全量重跑。正常不要加。
- 只改提示词想重生内容层：删对应产物文件再跑（或 `--force` 全重）。

### verify 报「历史欠账」
`verify_solomon.py` 只把**新页面**问题计入失败，历史欠账单列不影响退出码。
清理历史欠账需要内容级决策（如旧截图改名后引用丢失），逐页处理。

### 进度看不到
- `--progress-file <path>` 每阶段写一行带时间戳，供脚本轮询推送。
- `solomon ingest --progress-file /tmp/x.txt` + `tail -f /tmp/x.txt`。

## 已知边界

- 文档入库 = 存 raw 原文 + 骨架页 + 索引（不做五层提炼；五层是视频管线）
- 合集默认全部集数，务必 `--max-parts N`
- 未识别视频类型回落默认模板（要新类型往 SKILL_TEMPLATES 加 .md）
- FTS 索引是 vault 独立文件（`vault/.kb/kb_fts.db`），多 vault 互不影响