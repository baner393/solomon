# Solomon 系统部署手册

> 以 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 为底层框架的个人知识库与学习系统。
> 一条 B 站链接进去，出来一份带识图配图的五层知识库笔记 + 可问答、可追溯、多路线自动沉淀的个人知识体系。

---

## 1. 系统总览

```
消息渠道（QQ/飞书/微信/邮件）            ← 凭据自备
        │
        ▼
┌─ coordinator 网关（常驻）─────────────────────┐
│  确定性路由补丁（不经 LLM）：                 │
│  @help / 入库 / 删除 / 记住 / 清理缓存 /     │
│  临时读取 / 转正 / @@默认路由 / 问答路由      │
└──────────┬───────────────────────────────────┘
           │ hermes chat -Q --resume（会话续接）
   ┌───────┴────────┐        ┌──────────────────┐
   │ solomon 问答    │        │ newsolomon 入库   │
   │ query_kb.py    │        │ ingest.py 管线    │
   │ 学习路线记忆     │        │ 下载→转写→识图→   │
   └───────┬────────┘        │ 笔记→五层→入库    │
           │                 └────────┬─────────┘
           ▼                          ▼
   ┌─────────────────────────────────────┐
   │ Obsidian vault（知识库，git 管理）    │
   │ + FTS5 检索索引 + GitHub 私有备份     │
   └─────────────────────────────────────┘
```

**核心能力**：视频/网页/文档入库（图文结合五层笔记）· 知识问答（图文引用）· 学习路线多文件自动沉淀 · 个人档案自增长 · vault 自动备份 · 全渠道消息路由

**组成仓库**：
| 仓库 | 内容 | 是否必需 |
|---|---|---|
| [baner393/solomon](https://github.com/baner393/solomon) | 管线代码 + profiles 模板 + 网关补丁 + 本文档 | ✅ |
| [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) | 底层框架（锁定 `v2026.9.7` + 1 个补丁） | ✅ |
| Obsidian vault | 你的知识库数据（git 管理） | ✅（自建） |
| SenseNova 代理 | LLM 端点（或换任意 OpenAI 兼容端点） | ✅（或用其他） |

---

## 2. 前置要求

- **WSL2 (Ubuntu 22.04+) 或 Linux + systemd**
- `python3.14`（管线锁定版本；Ubuntu 用 deadsnakes PPA 或 `uv python install 3.14`）
- `pipx`、`git`、`curl`、`ffmpeg`、`node`（≥18）
- LLM：SenseNova API Key（[申请](https://console.sensecore.cn)）或任意 OpenAI 兼容端点
- 可选：7890 HTTP 代理（视频下载/YouTube）、QQ 机器人/飞书应用凭据

## 3. 快速安装

```bash
git clone https://github.com/baner393/solomon.git ~/solomon
cd ~/solomon
bash install.sh          # 交互式；SKIP_INTERACTIVE=1 bash install.sh 全默认
```

脚本做 7 件事：环境检查 → 克隆 → hermes-agent 锁版本+打补丁 → Python 包 → profiles 骨架 → SenseNova 代理（可选）→ 验证。**装完必须回到「§5 配置」填凭据**。

## 4. 手动安装（分步）

### 4.1 hermes-agent + 路由补丁

```bash
git clone --filter=blob:none https://github.com/NousResearch/hermes-agent.git ~/.local/src/hermes-agent
cd ~/.local/src/hermes-agent
git fetch --tags origin main && git checkout tags/v2026.9.7
git apply ~/solomon/patches/gateway-route-patch.diff   # 979 行，4 文件，零冲突（对 v2026.9.7）
pipx install --python python3.14 -e ~/.local/src/hermes-agent
```

> **为什么锁 v2026.9.7**：补丁基于该版本源码（上游更新极快，v0.21.5 起架构大改）。
> 补丁内容：确定性 @/@@ 路由、入库参数剥离、会话续接恢复（死锁清锁+重试）、
> QQ/飞书/微信渠道 MEDIA 图片投递、飞书 @mention 兼容。

### 4.2 solomon 包

```bash
git clone https://github.com/baner393/solomon.git ~/solomon
pip3.14 install -e ~/solomon        # 提供 solomon CLI
```

### 4.3 profiles（三个 agent 人格）

```bash
mkdir -p ~/.hermes/profiles/{coordinator,solomon,newsolomon}
cp ~/solomon/profiles/coordinator/{SOUL.md,config.yaml.example} ~/.hermes/profiles/coordinator/
mv ~/.hermes/profiles/coordinator/config.yaml.example ~/.hermes/profiles/coordinator/config.yaml
cp ~/solomon/profiles/solomon/SOUL.md ~/.hermes/profiles/solomon/SOUL.md
cp ~/solomon/profiles/solomon/config.yaml.example ~/.hermes/profiles/solomon/config.yaml
mv ~/.hermes/profiles/solomon/config.yaml.example ~/.hermes/profiles/solomon/config.yaml
cp ~/solomon/profiles/newsolomon/SOUL.md ~/.hermes/profiles/newsolomon/SOUL.md
cp ~/solomon/profiles/env.example ~/.hermes/profiles/{coordinator,solomon,newsolomon}/.env
```

### 4.4 LLM 代理（SenseNova 4Key 轮换）

```bash
cd ~/solomon/scripts/sensenova-proxy
# 编辑三个 .js 顶部 API_KEYS 数组，填入你的 Key
node sensenova-proxy.js &            # 3456 通用池
node sensenova-flashlite-proxy.js &  # 3458 flash-lite 池
curl http://127.0.0.1:3456/health    # 验证
```

建议配 systemd user service 常驻（见 `scripts/sensenova-proxy/README.md`）。
**不用 SenseNova？** 把 `config.yaml` 的 `providers` 段改成任意 OpenAI 兼容端点即可（`docs/llm-endpoint.md`）。

### 4.5 转写模型（视频入库需要）

```bash
mkdir -p ~/models
# sherpa-onnx SenseVoice（int8，~250MB，中文 SOTA）
# 下载地址见 solomon 仓库 docs/troubleshooting.md「模型位置」节
```

## 5. 配置详解

每个 profile 的 `.env`（键名全集见 `profiles/env.example`）：

| 键 | 说明 | 必填 |
|---|---|---|
| `SOLOMON_VAULT` | 知识库根目录（自动 git init） | ✅ |
| `WORK_ROOT` | 视频处理工作目录 | ✅ |
| `SOLOMON_LOCATE_ROOTS` | 文件自动定位搜索根 | 建议 |
| `QQ_APP_ID/CLIENT_SECRET` | QQ 机器人凭据 | 用 QQ 则必填 |
| `FEISHU_APP_ID/APP_SECRET` | 飞书应用凭据 | 用飞书则必填 |
| `HTTP_PROXY/HTTPS_PROXY` | 下载代理 | 建议 |

`config.yaml`：模型 provider 指向本地代理端口；渠道开关在 `gateway.platforms`。

**知识库初始化**：`SOLOMON_VAULT=<你的库路径> solomon init`

## 6. 渠道接入

凭据自行申请：QQ 机器人（q.qq.com 开放平台）、飞书自建应用（open.feishu.cn，需开机器人能力+事件订阅）。
填入 `.env` → `gateway.platforms.<渠道>.enabled: true` → 重启网关。接手者的渠道凭据与部署者无关，配自己的即可。

## 7. 启动与验证

```bash
# 网关（常驻；建议 systemd user service）
hermes --profile coordinator gateway run

# 验证清单（逐项通过 = 部署成功）
solomon doctor                                      # ① 环境自检全绿
curl http://127.0.0.1:3456/health                   # ② LLM 代理健康
hermes --profile solomon chat -Q -q "你是谁"          # ③ 问答 agent 回应（带喵）
solomon ingest "https://www.bilibili.com/video/BVxxx"  # ④ 真实入库一个短视频（EXIT=0 + vault 出现五层页）
hermes --profile solomon chat -Q -q "刚入库的视频讲了什么"  # ⑤ 问答命中新内容
```

## 8. 日常使用（发给网关的命令）

```
@help                          # 全部命令清单（永远最新）
@newsolomon 入库 <URL|文件>      # 入库（--images 图文版 / --force 重跑 / 末尾「全部入库」「前3集」批量）
@solomon <问题>                # 知识库问答（自动引用资料截图）
@newsolomon 临时读取 <URL>       # 不入库先看（--fast 快速）
@newsolomon 转正 <标题>          # 临时区 → 正式入库
@newsolomon 删除 <标题>          # 预览→「确认删除」执行
@newsolomon 记住：xxx           # 个人档案
@newsolomon 清理缓存            # 中间产物清理
@@solomon                      # 本会话默认路由（免 @）
```

## 9. 维护

### 内建定时任务（gateway cron，`hermes cron list`）
- vault 知识库每日同步 GitHub（需给 vault 加 origin remote）
- 学习路线每日自动沉淀（会话库 → `memories/roadmaps/`）

### 更新与补丁重放
hermes-agent 升级会**覆盖补丁**。流程：`git fetch && git checkout <新tag>` →
`git apply --check ~/solomon/patches/gateway-route-patch.diff`（冲突则手工重放）→ 应用 → 重启网关。
solomon 管线更新：`git -C ~/solomon pull`（补丁与管线解耦，互不影响）。

### 备份
vault 是 git 仓库：加 `origin remote` 后系统自动推送（事件驱动 + 每日兜底）。

## 10. 故障排查（高频坑速查）

| 症状 | 原因 | 解法 |
|---|---|---|
| `hermes -C` 看到 wrong 仓库提交 | 启动环境 `GIT_DIR=` 空串污染 | 脚本头部 `unset GIT_DIR GIT_WORK_TREE`，**禁用** `GIT_DIR= git ...` 前缀 |
| 查询 0 命中 | FTS 索引过期 | `solomon index build` |
| 问答超时 | LLM 上游挂流 | 代理 `/health` 看 key 状态；网关有 fresh 兜底，重发一次 |
| 「solomon 无输出」 | resume 撞死进程锁 | 网关已带清锁自愈；仍见则删 state.db 的 `session_turn_leases` 行 |
| 入库静默无推送 | 管线崩溃 | 查 `/tmp/ingest_stderr_*.log`（强制留痕设计） |
| 微信/QQ 收不到回复 | 渠道凭据/DNS | `resolv.conf` 固定公共 DNS + wsl.conf `generateResolvConf=false` |

完整事故史与运维红线：原部署者的 `infra/HANDOFF.md`（未开源，坑已泛化进本文档）。

## 11. 已知限制

- hermes-agent 锁定 v2026.9.7（上游更新快，跨大版本需重放补丁）
- 转写仅 sherpa-onnx 路线（GPU/torch 栈在 4GB WSL 会 OOM，设计如此）
- 飞书/邮件入库管线未接（需凭据），PPTX/网页/文档已支持
