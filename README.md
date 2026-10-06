# solomon

视频/文档 → 知识库 → 语义问答的**确定性管线**：一条命令完成视频下载、转写、关键帧、识图、笔记、五层提炼、入库，以及基于 FTS5 + LLM 的知识库问答。

```
solomon ingest <B站视频URL>     # 视频 → 知识库
solomon ingest docs/笔记.md      # 文档 → 知识库
solomon ask "RAG 是什么"         # 知识库问答
```

## 系统要求（先读这段）

| 你的环境 | 支持情况 |
|---|---|
| **Linux / macOS** | ✅ 完整支持（推荐） |
| **Windows + WSL2 (Ubuntu)** | ✅ 完整支持——官方部署路线，见 [DEPLOY.md 第 0 步](DEPLOY.md#0-环境准备含-wsl-安装) |
| **Windows 原生（无 WSL）** | ⚠️ 实验性：核心 CLI（文档入库/问答/删除）可用；**视频管线与渠道机器人不支持** |

> 🔴 **不要自行安装裸版 hermes 来"补全"机器人功能**。QQ/飞书/微信多渠道问答依赖 hermes-agent
> **锁定版本 + 路由补丁重放**（979 行，含确定性路由/会话续接/图文发图），裸装 hermes 跑不起来。
> 要机器人 → 走 [DEPLOY.md](DEPLOY.md)（含 WSL 安装指引），或把
> [docs/DEPLOY-WITH-AGENT.md](docs/DEPLOY-WITH-AGENT.md) 喂给任意 coding agent 全自动部署。

## 为什么是它

- **确定性**：所有步骤是脚本管线（`ingest.py`），不是 agent 自由发挥——可断点续跑、可重放、可回退
- **零框架依赖**：问答用 FTS5 全文检索（jieba 分词）+ LLM 综合，不依赖 RAG 框架
- **转写无 torch**：主引擎 sherpa-onnx SenseVoice（int8，CPU 高效），不需要 GPU/大模型栈
- **知识库用 Obsidian 格式**：产物是标准 markdown + wikilink，vault 可被 Obsidian 直接打开

## 快速开始（路线 A：核心 CLI，人人可用）

```bash
# 0. 前置：Python ≥ 3.11（路线 B 全家桶推荐 3.13——hermes 锁版要求 <3.14，见 DEPLOY.md §2；纯 CLI 3.11+ 均可）；视频入库另需 yt-dlp + ffmpeg（纯文档/问答不需要）

# 1. 安装（推荐 venv；Ubuntu 23.04+ 直接 pip 会报 externally-managed-environment）
python3 -m venv .venv && source .venv/bin/activate
pip install -e .          # 或 uv sync（推荐）
# 注：当前版本 pip 会一并安装视频管线依赖（faster-whisper/opencv 等，下载约 700MB，
# 慢网需耐心）。纯文档/问答用户也建议直接装——暂无最小安装分组。

# 2. 配置：LLM 端点（唯一必配项）
cp .env.example .env
#   编辑 .env，填两行：
#   LLM_BASE_URL=https://token.sensenova.cn/v1   （任意 OpenAI 兼容端点）
#   LLM_API_KEY=你的密钥

# 3. 初始化 + 自检
solomon init               # 建 vault 目录结构
solomon doctor             # 一键环境自检（依赖/LLM/vault）

# 4. 入库（三选一）
solomon ingest "https://www.bilibili.com/video/BVxxx"   # 视频（B站直连，无需代理）
solomon ingest path/to/doc.md                            # 文档
solomon ingest --name "视频标题"                          # 按名称搜索

# 5. 问答
solomon ask "RAG 是什么"
```

> 识图默认走端点原生视觉（base64 → image_url）。若你的端点不支持 base64 形态，
> 在 `.env` 设 `FAST_VISION_JS=<仓库>/scripts/fast-vision.js` 切换到「图床上传→URL」备选线路，
> 详见 [DEPLOY.md 辅助方案](DEPLOY.md#辅助方案llm-能对话但不能识图)。

## 路线 B：多渠道知识库机器人（QQ/飞书/微信）

在路线 A 之上加 hermes 网关（profile 体系 + 渠道长连接 + 确定性路由 + 学习路线沉淀）。
**需要 WSL2/Linux**，含版本锁定与补丁重放——全部流程见 [DEPLOY.md](DEPLOY.md)。

## 命令总览

| 命令 | 说明 |
|---|---|
| `solomon init` | 初始化 vault 目录结构 |
| `solomon doctor` | 环境自检（依赖 / LLM 端点 / vault 可写），给出修复建议 |
| `solomon ingest <URL\|文件\|--name\|--text>` | 入库。`--images` 配图版；`--force` 全量重跑；`--max-parts N` 合集限前 N 集 |
| `solomon ask "问题"` | 知识库问答（FTS5 检索 + LLM 综合回答）。`--raw` 只看检索结果 |
| `solomon index update` | 重建 FTS5 索引（入库后查询 0 命中时用） |
| `solomon verify` | 校验知识库链接/断链 |

## 知识库结构

```
vault/
├── raw/articles/   # 转写原文（frontmatter 带 source_url）
├── raw/assets/     # 视频截图/配图
├── concepts/       # 五层提炼页（核心知识）
├── entities/       # 实体页
├── comparisons/    # 对比页
├── queries/        # 问答页
├── index.md        # 分类索引（自动维护）
└── log.md          # 入库日志（自动追加）
```

## 视频入库管线（做了什么）

```
下载(yt-dlp/B站API兜底) → 字幕(B站AI字幕/转写降级) → 关键帧(场景检测+去重)
→ 识图(LLM 视觉) → 笔记(LLM, 长视频分段) → 五层提炼 → 入库(图文结合)
→ postprocess(FTS5索引/相关页/index/log/verify)
```

特性：断点续跑（产物存在则跳过）、合集分P、视频类型→模板、多模态交叉验证、音频信号标记、标注截图、英文视频中文化、CHECKPOINT B 质量门槛。

## 环境要求

- Python ≥ 3.11；推荐 3.13（hermes 锁版 v2026.9.7 要求 <3.14，一套通吃最省心；solomon 管线亦支持 3.14）
- LLM 端点：任意 OpenAI 兼容服务（`.env` 配 `LLM_BASE_URL` + `LLM_API_KEY`；不配则回落本地代理 `127.0.0.1:3456`）。⚠️ LLM 调用**直连**端点（不经 HTTP_PROXY 代理 env）——受限网络请选机器可直连的端点，或用本地代理形态
- 视频入库：yt-dlp + ffmpeg；B站直连无需代理；YouTube 等海外源需 `.env` 配 `HTTP_PROXY`
- 可选：sherpa-onnx（字幕降级转写）、opencv（关键帧，装 `pip install -e ".[video]"`）

## 文档

- [架构](docs/architecture.md)
- [部署/配置（含 WSL 安装指引）](DEPLOY.md)
- [LLM 端点配置](docs/llm-endpoint.md)
- [故障排查](docs/troubleshooting.md)

## License

MIT
