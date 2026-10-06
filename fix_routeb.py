#!/usr/bin/env python3
"""路线 B 可执行性修复：Python 版本口径统一（hermes 锁版 <3.14）+ vault git 基线 + 模板补齐。
每处替换断言命中。"""
def patch(path, old, new, count=1):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    assert old in text, f"NOT FOUND in {path}: {old[:90]!r}"
    assert text.count(old) >= count, f"COUNT<{count} in {path}"
    text = text.replace(old, new, count)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"  ✓ {path}: {old.strip().splitlines()[0][:60]!r}")

# ═══ 1. install.sh：Python 3.13 全家桶统一 + 补丁文案 3 文件 ═══
P = "install.sh"
patch(P,
'''for cmd in git curl ffmpeg node;  do command -v $cmd >/dev/null || die "缺 $cmd（apt install $cmd）"; done
command -v python3.14 >/dev/null || die "缺 python3.14（Ubuntu: deadsnakes PPA / 或用 uv python install 3.14）"
command -v pipx >/dev/null || pipx --version >/dev/null 2>&1 || die "缺 pipx（apt install pipx）"
ok "基础依赖齐备"''',
'''for cmd in git curl ffmpeg node;  do command -v $cmd >/dev/null || die "缺 $cmd（apt install $cmd）"; done
# Python 版本：推荐 3.13 一套通吃——hermes-agent 锁版 v2026.9.7 声明 requires-python
# ">=3.11,<3.14"（3.14 会被 pip 硬拒）；solomon 管线兼容 >=3.11。
PY388=""
for cand in python3.13 python3.12 python3.11; do
  command -v $cand >/dev/null && { PY388=$cand; break; }
done
[[ -n $PY388 ]] || die "缺 python3.13（推荐；hermes 锁版要求 >=3.11,<3.14，solomon 兼容 >=3.11。Ubuntu: deadsnakes PPA / uv python install 3.13）"
command -v pipx >/dev/null || pipx --version >/dev/null 2>&1 || die "缺 pipx（apt install pipx）"
ok "基础依赖齐备（python=$PY388）"''')
patch(P,
'''pipx install --python python3.14 -e "$HERMES_SRC" 2>/dev/null \\
  || pipx upgrade --python python3.14 -e hermes-agent''',
'''pipx install --python "$PY388" -e "$HERMES_SRC" 2>/dev/null \\
  || pipx upgrade --python "$PY388" -e hermes-agent''')
patch(P,
'''ok "网关补丁已应用（route-robust 979 行，4 文件）"''',
'''ok "网关补丁已应用（979 行，3 文件：qqbot/feishu adapter + run_inbound）"''')
patch(P,
'''pip3.14 install -e "$REPO_DIR" --quiet || pip3.14 install -e "$REPO_DIR"''',
'''"$PY388" -m pip install -e "$REPO_DIR" --quiet 2>/dev/null || "$PY388" -m pip install -e "$REPO_DIR"''')

# ═══ 2. DEPLOY.md：前置要求/手动分步的 Python 版本 + vault git ═══
P = "DEPLOY.md"
patch(P,
'''sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt install -y python3.14 python3.14-venv
# 或用 uv：curl -LsSf https://astral.sh/uv/install.sh | sh && uv python install 3.14''',
'''sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt install -y python3.13 python3.13-venv
# 或用 uv：curl -LsSf https://astral.sh/uv/install.sh | sh && uv python install 3.13''')
patch(P,
'''- **WSL2 (Ubuntu 22.04+) 或 Linux + systemd**
- `python3.14`（管线锁定版本；Ubuntu 用 deadsnakes PPA 或 `uv python install 3.14`）''',
'''- **WSL2 (Ubuntu 22.04+) 或 Linux + systemd**
- **Python 3.13**（推荐，一套通吃：hermes 锁版 v2026.9.7 声明 `requires-python >=3.11,<3.14`——**3.14 装 hermes 会被 pip 硬拒**；solomon 管线兼容 >=3.11。Ubuntu 用 deadsnakes PPA 或 `uv python install 3.13`）''')
patch(P,
'''git apply ~/solomon/patches/gateway-route-patch.diff   # 979 行，4 文件，零冲突（对 v2026.9.7）
pipx install --python python3.14 -e ~/.local/src/hermes-agent''',
'''git apply ~/solomon/patches/gateway-route-patch.diff   # 979 行，3 文件，零冲突（对 v2026.9.7）
pipx install --python python3.13 -e ~/.local/src/hermes-agent   # hermes 必须 <3.14''')

# ═══ 3. DEPLOY-WITH-AGENT.md：阶段 0/2/3 Python 版本 + 阶段 6 vault git + 持久化 ═══
P = "docs/DEPLOY-WITH-AGENT.md"
patch(P,
'''uname -r                        # 确认 WSL2(*microsoft*) 或 Linux
command -v python3.14 git curl ffmpeg node pipx   # 全部存在？缺什么列什么''',
'''uname -r                        # 确认 WSL2(*microsoft*) 或 Linux
command -v python3.13 git curl ffmpeg node pipx   # 全部存在？缺什么列什么
# ⚠️ Python 必须 3.11~3.13（hermes 锁版 v2026.9.7 声明 requires-python ">=3.11,<3.14"，
# 3.14 会被 pip 硬拒；solomon 管线兼容 >=3.11）。推荐 3.13。''')
patch(P,
'''缺依赖 → 安装（Ubuntu: `apt install git curl ffmpeg pipx; pipx ensurepath`；
python3.14 用 deadsnakes PPA 或 `uv python install 3.14` 后建软链）。
**验收**：平台为 WSL2/Linux；全部命令存在且版本合理。''',
'''缺依赖 → 安装（Ubuntu: `apt install git curl ffmpeg pipx; pipx ensurepath`；
python3.13 用 deadsnakes PPA 或 `uv python install 3.13` 后建软链）。
**验收**：平台为 WSL2/Linux；python3.13 存在且 `python3.13 --version` 正常；全部命令存在且版本合理。''')
patch(P,
'''git clone --filter=blob:none https://github.com/NousResearch/hermes-agent.git ~/.local/src/hermes-agent
cd ~/.local/src/hermes-agent
git fetch --tags origin main && git checkout tags/v2026.9.7
git apply ~/solomon/patches/gateway-route-patch.diff   # 979 行，4 文件''',
'''git clone --filter=blob:none https://github.com/NousResearch/hermes-agent.git ~/.local/src/hermes-agent
cd ~/.local/src/hermes-agent
git fetch --tags origin main && git checkout tags/v2026.9.7
git apply ~/solomon/patches/gateway-route-patch.diff   # 979 行，3 文件''')
patch(P,
'''pipx install --python python3.14 -e ~/.local/src/hermes-agent''',
'''pipx install --python python3.13 -e ~/.local/src/hermes-agent   # 必须 <3.14（pyproject 硬约束）''')
print("\n基础 patch 完成")
