# 部署与配置

## 环境要求

- Linux（WSL2 已验证）或 macOS
- Python ≥ 3.10
- 视频入库需：`yt-dlp` + `ffmpeg`（+ `ffprobe`）
- 可选加速/能力：`sherpa-onnx`（转写）、`opencv`（关键帧）

## 安装

```bash
# 方式 A：推荐，uv
uv sync
uv run solomon doctor

# 方式 B：pip
pip install -e .
solomon doctor

# 视频工具链
pip install yt-dlp
apt install ffmpeg          # 或 brew install ffmpeg
```

## 配置（`.env`）

复制模板后按需修改：

```bash
cp .env.example .env
```

| 变量 | 默认 | 说明 |
|---|---|---|
| `SOLOMON_VAULT` | `~/.solomon/vault` | 知识库根目录（可指 Obsidian 库） |
| `WORK_ROOT` | `~/.solomon/work` | 视频中间产物目录 |
| `SOLOMON_FTS_DB` | `(vault)/.kb` | FTS5 索引/查询缓存目录 |
| `SENSENOVA_BASE_URL` | `http://127.0.0.1:3458/v1` | LLM/视觉 OpenAI 兼容端点 |
| `SENSENOVA_API_KEY` | `proxy` | LLM API key |
| `HTTP_PROXY` | 空 | 出网代理（yt-dlp 下载用） |
| `PYTHON_BIN` | 当前解释器 | 子进程 python |
| `SOLOMON_PYTHONPATH` | 空 | 子进程 PYTHONPATH（依赖在系统 dist-packages 时需要） |
| `WHISPER_MODEL` | `medium` | 字幕降级转写模型 |
| `SOLOMON_MAX_PARTS` | `5` | 合集入库上限 |

> 兼容旧环境：历史版本把 FTS 索引放在固定目录，迁移时设
> `SOLOMON_FTS_DB=/旧目录` 即可继续用旧索引；不设则每 vault 独立新索引。

## 初始化与自检

```bash
solomon init       # 建 vault 目录结构（幂等）
solomon doctor     # 全环境自检：依赖 / LLM 端点 / vault 可写
```

`doctor` 是新人上手第一道闸：全绿再继续。

## 日常使用

```bash
# 视频入库（默认断点续跑；--force 全量重跑；--images 配图版）
solomon ingest "https://www.bilibili.com/video/BVxxx"
solomon ingest "https://www.bilibili.com/video/BVxxx" --images
solomon ingest --name "视频标题搜索" --max-parts 1

# 文档/文本入库
solomon ingest path/to/doc.md --category concept
solomon ingest --text "直接粘贴的内容" --title "标题"

# 问答
solomon ask "RAG 是什么"
solomon ask "什么是向量数据库" --raw    # 只看检索命中

# 维护
solomon index update      # 异常时重建 FTS5 索引
solomon verify            # 校验知识库链接/断链
```

## 迁移自旧版本

1. 把旧 vault 路径配到 `SOLOMON_VAULT`（vault 结构不变，直接复用）
2. FTS 索引二选一：
   - 设 `SOLOMON_FTS_DB` 指向旧索引目录（最快，立即可用）
   - 或删除旧索引引用，跑 `solomon index build` 重建（推荐，索引随 vault 走）
3. `solomon doctor` 确认全绿
4. 首次 `solomon ask` 验证检索命中老知识

## 常见部署形态

- **纯本地**：LLM 端点指本地代理（SenseNova/任意 OpenAI 兼容），隐私数据不出本机
- **远程 LLM**：`SENSENOVA_BASE_URL` 指云上端点，`.env` 里只配 key
- **渠道化**（QQ/飞书机器人）：接入 Hermes，见 `profiles/` + `patches/`