# Solomon 系统部署手册

> 以 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 为底层框架的个人知识库与学习系统。
> 一条 B 站链接进去，出来一份带识图配图的五层知识库笔记 + 可问答、可追溯、多路线自动沉淀的个人知识体系。

---

## 0. 环境准备（含 WSL 安装）

solomon 完整系统（含 hermes 网关与渠道）运行在 **Linux 环境**。Windows 用户需要先装 WSL2。

### 0.1 判断你的环境

- **Linux 用户**：跳过本节，直接进 §2。
- **Windows 用户**：打开 PowerShell（普通权限即可）执行 `wsl --status`：
  - 显示版本信息且默认版本为 2 → 已有 WSL，进 0.3；
  - 提示「未安装」/ 命令不存在 → 按 0.2 安装。

### 0.2 安装 WSL2（Windows 10 2004+ / Windows 11）

以**管理员身份**打开 PowerShell，执行：

```powershell
wsl --install -d Ubuntu-22.04      # 一键安装 WSL2 + Ubuntu 22.04
# wsl --list --online              # （可选）查看全部可安装发行版
```

安装完成后**重启电脑**，Ubuntu 首次启动会让你创建 Linux 用户名和密码（与 Windows 账户无关，记住密码即可，后面 sudo 要用）。

确认 WSL2 生效（版本列应为 2）：

```powershell
wsl -l -v
#   NAME            STATE           VERSION
# * Ubuntu-22.04    Running         2
```

> 若 VERSION 是 1：管理员 PowerShell 执行 `wsl --set-version Ubuntu-22.04 2`。

### 0.3 WSL 内基础工具

进入 WSL（开始菜单点 Ubuntu，或任意终端执行 `wsl`），安装基础工具：

```bash
sudo apt update && sudo apt install -y git curl ffmpeg pipx
pipx ensurepath && source ~/.bashrc
```

Python 3.14（管线生产验证版本；≥3.10 亦可跑）：

```bash
sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt install -y python3.13 python3.13-venv
# 或用 uv：curl -LsSf https://astral.sh/uv/install.sh | sh && uv python install 3.13
```

> 💡 **PEP 668 提醒（Ubuntu 23.04+）**：直接 `pip install` 会报 `externally-managed-environment`。
> 一律用 venv（路线 A：`python3 -m venv .venv && source .venv/bin/activate`）或 pipx/uv——
> **不要**用 `--break-system-packages` 硬装进系统。

> 💡 **把项目放进 WSL 自己的文件系统**（如 `~/solomon`），不要放 `/mnt/c/...`——
> 跨文件系统 IO 慢 5-10 倍，且部分文件监听/权限行为不一致。

### 0.4 路线选择

| 路线 | 内容 | 适合 |
|---|---|---|
| **A. 核心 CLI** | `pip install -e .` + `.env` 配 LLM 端点 → `solomon ingest/ask`（见 [README 快速开始](README.md)） | 只要个人知识库 + 命令行问答 |
| **B. 完整系统** | 路线 A 之上加 hermes 网关：QQ/飞书/微信 @ 机器人、确定性路由、会话续接、图文回复、学习路线自动沉淀、vault 自动备份（本文档 §3 起） | 要在聊天软件里用机器人 |

路线 B 已含路线 A 的全部能力。**不要试图用裸装 hermes 拼装路线 B**——路由/续接/发图功能
在锁定版本 + 补丁里（§4.1），版本不对整条链路残缺。

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
- **Python 3.13**（推荐，一套通吃：hermes 锁版 v2026.9.7 声明 `requires-python >=3.11,<3.14`——**3.14 装 hermes 会被 pip 硬拒**；solomon 管线兼容 >=3.11。Ubuntu 用 deadsnakes PPA 或 `uv python install 3.13`）
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
git apply ~/solomon/patches/gateway-route-patch.diff   # 979 行，3 文件，零冲突（对 v2026.9.7）
pipx install --python python3.13 -e ~/.local/src/hermes-agent   # hermes 必须 <3.14
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
mkdir -p ~/models && cd ~/models
# sherpa-onnx SenseVoice int8（~250MB，中文 SOTA，CPU 高效无 torch）
curl -L -o sv.tar.bz2 https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2
tar xzf sv.tar.bz2 && mv sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17 sherpa-onnx-sense-voice
```

> **没有这个模型也能用**：B 站视频大多有 AI 字幕（直接抓取，不需要本地转写）。
> 只有「无 CC 且无 ASR 字幕」的视频才回落本地转写——届时装上模型即可，管线自动探测。

### 4.6 网关常驻（systemd user service）

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/hermes-gateway.service <<UNIT
[Unit]
Description=Solomon coordinator gateway
[Service]
ExecStart=%h/.local/bin/hermes --profile coordinator gateway run
Restart=always
[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload && systemctl --user enable --now hermes-gateway
journalctl --user -u hermes-gateway -f    # 看渠道连接日志
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

**QQ 机器人**（官方 API，WebSocket 接入，无需公网）：
1. [q.qq.com](https://q.qq.com) 注册开发者 → 创建机器人 → 拿 `AppID`/`AppSecret`（.env 的 `QQ_APP_ID`/`QQ_CLIENT_SECRET`）
2. **沙箱环境先测**：平台「沙箱配置」里把你的 QQ 号加为沙箱成员（正式上线需审核）
3. 消息权限：平台「机器人功能」里开通「C2C 单聊/群聊」消息能力
4. .env 填好 → `gateway.platforms.qqbot.enabled: true` → 重启网关 → 沙箱群里发 `@help`

**飞书**（WSS 长连接，无需公网服务器）：
1. [open.feishu.cn](https://open.feishu.cn) 创建企业自建应用 → 凭据页拿 `App ID`/`App Secret`
2. 权限：开「接收群聊@机器人消息」「读取用户发给机器人的单聊消息」；事件订阅选「**使用长连接接收事件**」（免公网回调）
3. .env 填 `FEISHU_APP_ID/APP_SECRET` → `gateway.platforms.feishu.enabled: true` → 重启 → 私聊或群里 @ 发 `@help`

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

### 辅助方案：LLM 能对话但不能识图

**症状**：`solomon doctor` 全绿、问答正常，但 `--images` 入库时识图全部 `❌ 识图失败`。

**原因**：solomon 默认用端点原生视觉（图片 base64 → OpenAI `image_url` 格式）。部分端点
不接受 base64 data URL（只收公网图片 URL），或所选模型没有视觉能力。

**方案 A（推荐）**：换支持标准 OpenAI 视觉格式的端点/模型（如 SenseNova 视觉系列、
GPT-4o 系、Qwen-VL 系）。

**方案 B**：切换「图床上传 → 公网 URL」备选线路（仓库自带脚本，需 node ≥18 + curl）：

```bash
# .env 追加一行（指向仓库内脚本），solomon 识图自动改走该脚本：
FAST_VISION_JS=~/solomon/scripts/fast-vision.js
```

脚本行为：本地图片先上传到公共图床换取公网 URL，再以 `image_url` 传给端点。
kimi 系模型自动改走 base64 直传（官方仅支持 base64）；其他模型可加 `--direct` 强制 base64。
端点/密钥与主管线同源（读同一份 `.env` 的 `LLM_BASE_URL`/`LLM_API_KEY`）。
独立测试：`node ~/solomon/scripts/fast-vision.js -p "图里有什么" <图片路径>`。

**注意**：图床上传会把图片发到公共图床（uguu.se / temp.sh，短时效）——**敏感图片不要走方案 B**。

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
