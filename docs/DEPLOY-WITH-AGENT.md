# Solomon 系统 Agent 部署任务书

> 把本文档整体喂给 Claude Code / ZCode / 任意 coding agent，它就能代替你完成全部部署。
> 人类只需准备：一台 WSL2 Ubuntu 或 Linux 机器、LLM API Key、（可选）渠道凭据。
> agent 完成后按「§验收清单」逐项自检并输出报告。

---

## 角色与目标

你是部署工程师。目标：在本机部署 **Solomon 个人知识库系统**（hermes-agent 底层 + solomon 管线），
使其达到「**验收清单**（文末）全部通过」的状态。每完成一步执行该步的**验收命令**，失败则修复后重试，
连续 2 次失败停下并在报告中说明（不要跳过）。

## 全局红线（违反即任务失败）

1. **严禁** `wsl.exe --shutdown` / `wsl --terminate`（机器上可能有其他生产负载）
2. 修改任何 `git` 环境前先 `unset GIT_DIR GIT_WORK_TREE`（启动环境可能注入空串污染）
3. 凭据（API Key/token）只写入 `.env`/模板占位符，**绝不**写进会提交的文件或打印到报告
4. 每步验收失败不擅自改需求范围（不换依赖版本、不跳过步骤）
5. 所有下载走官方源；sherpa-onnx 模型较大，失败可跳过（标记「待人工」）

## 阶段 0：环境体检

```bash
uname -r                        # 确认 WSL2(*microsoft*) 或 Linux
command -v python3.14 git curl ffmpeg node pipx   # 全部存在？缺什么列什么
systemctl --user is-system-running 2>/dev/null      # systemd user 可用？
```
缺依赖 → 安装（Ubuntu: `apt install git curl ffmpeg pipx; pipx ensurepath`；
python3.14 用 deadsnakes PPA 或 `uv python install 3.14` 后建软链）。
**验收**：全部命令存在且版本合理。

## 阶段 1：solomon 仓库

```bash
git clone https://github.com/baner393/solomon.git ~/solomon
```
**验收**：`ls ~/solomon/DEPLOY.md install.sh patches/ profiles/` 存在。

## 阶段 2：hermes-agent 锁版本 + 补丁

```bash
mkdir -p ~/.local/src && git clone --filter=blob:none \
  https://github.com/NousResearch/hermes-agent.git ~/.local/src/hermes-agent
cd ~/.local/src/hermes-agent
git fetch --tags origin main && git checkout tags/v2026.9.7
git apply --check ~/solomon/patches/gateway-route-patch.diff   # 必须零报错
git apply ~/solomon/patches/gateway-route-patch.diff
pipx install --python python3.14 -e ~/.local/src/hermes-agent
```
**验收**：`hermes --version` 输出版本；`git apply --check` 无冲突。
（冲突=仓库不干净，`git status` 查明后 `git checkout -- .` 重来。）

## 阶段 3：solomon CLI

```bash
pip3.14 install -e ~/solomon
solomon --help   # 出现 ingest/ask/doctor/init/verify/index 子命令
```
**验收**：`solomon doctor` 能跑（此刻 vault 未配会报「vault 不存在」，属预期，记录即可）。

## 阶段 4：profiles 骨架

```bash
mkdir -p ~/.hermes/profiles/{coordinator,solomon,newsolomon}
cp ~/solomon/profiles/coordinator/SOUL.md ~/.hermes/profiles/coordinator/SOUL.md
cp ~/solomon/profiles/solomon/SOUL.md    ~/.hermes/profiles/solomon/SOUL.md
cp ~/solomon/profiles/newsolomon/SOUL.md ~/.hermes/profiles/newsolomon/SOUL.md
cp ~/solomon/profiles/coordinator/config.yaml.example ~/.hermes/profiles/coordinator/config.yaml
cp ~/solomon/profiles/solomon/config.yaml.example    ~/.hermes/profiles/solomon/config.yaml
for p in coordinator solomon newsolomon; do
  cp ~/solomon/profiles/env.example ~/.hermes/profiles/$p/.env
done
```
**验收**：三个 profile 目录各有 SOUL.md/config.yaml/.env。

## 阶段 5：LLM 端点

问用户要 SenseNova API Key（或用户指定的 OpenAI 兼容端点+key）。

**SenseNova 路线**：编辑 `~/solomon/scripts/sensenova-proxy/*.js` 顶部 `API_KEYS`
（sed 替换 `SK_YOUR_KEY_1` 占位符）→ 写 systemd user service（模板见
`scripts/sensenova-proxy/README.md`）→ `systemctl --user enable --now` 两个服务。
**自定义端点路线**：改两个 config.yaml 的 `providers`/`model` 段指向用户端点，跳过代理。

**验收**：`curl -s http://127.0.0.1:3456/health` 返回 `"status":"ok"`（SenseNova 路线）。

## 阶段 6：知识库初始化

```bash
export SOLOMON_VAULT=~/solomon-vault
solomon init
```
**验收**：vault 目录出现 `concepts/ raw/ index.md` 等结构；`git -C ~/solomon-vault log` 有基线提交。

## 阶段 7：端到端验证（验收清单）

1. `SOLOMON_VAULT=~/solomon-vault solomon doctor` 全绿
2. `hermes --profile solomon chat -Q -q "你是谁"` 返回带「喵」的回答（LLM 链路通）
3. `solomon ingest "https://www.bilibili.com/video/BV1C69iBgEMk"` 完整入库
   （EXIT=0；vault 出现 concepts/五层页 + raw/转写 + index.md 条目）
4. `SOLOMON_VAULT=~/solomon-vault python3.14 ~/solomon/src/solomon/query/query_kb.py "RAG的基本原理"` 命中刚入库内容
5. `hermes --profile coordinator gateway run`（后台）→ 向渠道发 `@help` 收到命令清单（有渠道凭据时）；
   无渠道则用 `hermes --profile solomon chat -Q -q "..."` 替代验证路由链路
6. （可选）vault 加 GitHub 私有 remote → 手动触发一次推送成功

## 完成报告格式

```
✅ 阶段 0-7 逐项结果（含验收命令输出摘要）
⚠️ 跳过/待人工项（如模型下载、渠道凭据）
📋 后续人类动作清单（填哪些 .env 键、要不要 systemd 常驻）
```

## 参考

- 手动部署全流程：`~/solomon/DEPLOY.md`（本任务书的展开版）
- 故障排查：`~/solomon/docs/troubleshooting.md`
- 命令清单：部署完成后在渠道发 `@help`
