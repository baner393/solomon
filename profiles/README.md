# Hermes 集成（可选层）

solomon **核心不依赖 Hermes**——`src/solomon/` 是纯 Python 管线，可直接 `solomon` CLI 使用。
本目录说明如何把它接进 [Hermes](https://github.com/NousResearch/hermes)（消息网关/agent 框架），
实现「QQ/飞书发 `@newsolomon` 入库、`@solomon` 问答」。

## 结构

```
profiles/
├── solomon/SOUL.md        # 问答管家 profile 模板（去凭据）
├── newsolomon/SOUL.md     # 入库管家 profile 模板（去凭据）
└── README.md              # 本文件
patches/
└── gateway-route-patch-20260912.diff   # 确定性 @/@@ profile 路由补丁（去个人路径）
```

## 接入步骤（以 Hermes 为例）

### 1. 建 profile

```bash
mkdir -p ~/.hermes/profiles/solomon ~/.hermes/profiles/newsolomon
cp profiles/solomon/SOUL.md      ~/.hermes/profiles/solomon/SOUL.md
cp profiles/newsolomon/SOUL.md   ~/.hermes/profiles/newsolomon/SOUL.md
# 编辑两个 SOUL.md，把 <SOLOMON_REPO> / <VAULT> 占位符换成你的路径
```

> profile 还需要 `config.yaml`（模型/工具配置）——从你的 Hermes 既有 profile 复制并改名，
> 或用 `hermes --profile <name> init` 生成。凭据走 `.env`。

### 2. 打确定性路由补丁（可选，强烈推荐）

Hermes 网关默认用 LLM 判断 `@profile` 前缀（不稳、慢）。补丁在网关源码加**程序化**
`@@solomon`/`@solomon` 确定性路由，不经 LLM：

```bash
# 1. 找到 hermes-agent 源码（editable install 时在 .local/src/hermes-agent）
cd <hermes-agent-src>
# 2. 把补丁里的占位符替换为你的环境
sed -i 's|__HERMES_HOME__|/home/you/.hermes|g; s|__HERMES_BIN__|/home/you/.local/share/pipx/venvs/hermes-agent/bin/hermes|g' \
    <solomon>/patches/gateway-route-patch-20260912.diff
# 3. 应用
git apply <solomon>/patches/gateway-route-patch-20260912.diff
# 4. 重启网关
hermes --profile coordinator gateway restart
```

**⚠️ 补丁是 hermes-agent 源码改动**：Hermes 升级会覆盖，升级后需重打（重新执行步骤 2-3）。
补丁文件保留在仓库里，随时可重放。

### 3. 路由规则（补丁内置白名单）

| 用户发 | 行为 |
|---|---|
| `@@solomon` | 本会话默认 profile 设为 solomon，后续无前缀消息路由给它 |
| `@solomon <问题>` | 直接 `hermes -p solomon chat -Q -q "<问题>"`，原样转发回答 |
| `@newsolomon <内容>` | 走 coordinator SOUL 规则（跑 ingest.py + 进度推送） |
| `@@` | 取消默认 profile |

> 白名单在补丁的 `_ROUTABLE_PROFILES` 常量里，按需增删。

### 4. 多轮会话（可选）

确定性路由默认每次新会话；要跨轮记忆，用
`hermes -p <profile> chat -Q -q "<msg>" --resume <session_id>`，
由网关在状态文件里跟踪 session_id（补丁已实现）。

## 不接 Hermes 也能用

所有能力通过 `solomon` CLI 直接可用（本仓库 README 快速开始）。
Hermes 集成只是「渠道化」的外壳。
