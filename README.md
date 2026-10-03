# solomon

视频/文档 → 知识库 → 语义问答的**确定性管线**：一条命令完成视频下载、转写、关键帧、识图、笔记、五层提炼、入库，以及基于 FTS5 + LLM 的知识库问答。

```
solomon ingest <B站视频URL>     # 视频 → 知识库
solomon ingest docs/笔记.md      # 文档 → 知识库
solomon ask "RAG 是什么"         # 知识库问答
```

## 为什么是它

- **确定性**：所有步骤是脚本管线（`ingest.py`），不是 agent 自由发挥——可断点续跑、可重放、可回退
- **零框架依赖**：问答用 FTS5 全文检索（jieba 分词）+ LLM 综合，不依赖 RAG 框架
- **转写无 torch**：主引擎 sherpa-onnx SenseVoice（int8，CPU 高效），不需要 GPU/大模型栈
- **知识库用 Obsidian 格式**：产物是标准 markdown + wikilink，vault 可被 Obsidian 直接打开

## 快速开始

```bash
# 1. 安装
pip install -e .          # 或 uv sync（推荐）
pip install yt-dlp ffmpeg  # 下载/转码工具

# 2. 配置（全部可选，默认即可跑）
cp .env.example .env       # 改 vault 路径 / LLM 端点 / 代理，见文件内注释

# 3. 初始化 + 自检
solomon init               # 建 vault 目录结构
solomon doctor             # 一键环境自检（依赖/LLM/vault）

# 4. 入库（三选一）
solomon ingest "https://www.bilibili.com/video/BVxxx"   # 视频
solomon ingest path/to/doc.md                            # 文档
solomon ingest --name "视频标题"                          # 按名称搜索

# 5. 问答
solomon ask "RAG 是什么"
```

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
→ 识图(SenseNova/LLM) → 笔记(LLM, 长视频分段) → 五层提炼 → 入库(图文结合)
→ postprocess(FTS5索引/相关页/index/log/verify)
```

特性：断点续跑（产物存在则跳过）、合集分P、视频类型→模板、多模态交叉验证、音频信号标记、标注截图、英文视频中文化、CHECKPOINT B 质量门槛。

## 环境要求

- Python ≥ 3.10
- yt-dlp + ffmpeg（视频入库）
- OpenAI 兼容 LLM 端点（默认本地 `http://127.0.0.1:3458/v1`，可通过 `.env` 改）
- 可选：sherpa-onnx（转写）、opencv（关键帧）

## 文档

- [架构](docs/architecture.md)
- [部署/配置](docs/deploy.md)
- [LLM 端点配置](docs/llm-endpoint.md)
- [故障排查](docs/troubleshooting.md)

## License

MIT
