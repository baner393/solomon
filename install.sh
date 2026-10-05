#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# Solomon 系统一键安装（WSL2 Ubuntu / Linux + systemd）
# 用法：bash install.sh          # 交互式
#       SKIP_INTERACTIVE=1 bash install.sh   # 全默认（之后手填配置）
# ═══════════════════════════════════════════════════════════════
set -euo pipefail
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE

HERMES_TAG="v2026.9.7"          # 网关补丁锁定版本（route-stable 基点）
REPO_DIR="${REPO_DIR:-$HOME/solomon}"
HERMES_SRC="${HERMES_SRC:-$HOME/.local/src/hermes-agent}"

say() { echo -e "\n\033[1;36m══ $* ══\033[0m"; }
die() { echo -e "\033[1;31m❌ $*\033[0m"; exit 1; }
ok()  { echo -e "\033[1;32m✓ $*\033[0m"; }

say "① 环境检查"
[[ "$(uname -r)" == *microsoft* || -d /run/systemd/system ]] || die "需要 WSL2(Ubuntu, systemd) 或 Linux"
for cmd in git curl ffmpeg node;  do command -v $cmd >/dev/null || die "缺 $cmd（apt install $cmd）"; done
command -v python3.14 >/dev/null || die "缺 python3.14（Ubuntu: deadsnakes PPA / 或用 uv python install 3.14）"
command -v pipx >/dev/null || pipx --version >/dev/null 2>&1 || die "缺 pipx（apt install pipx）"
ok "基础依赖齐备"

say "② 克隆 solomon → $REPO_DIR"
[[ -d $REPO_DIR ]] || git clone https://github.com/baner393/solomon.git "$REPO_DIR"
ok "solomon 就绪"

say "③ 安装 hermes-agent（锁定 $HERMES_TAG + 网关路由补丁）"
mkdir -p "$(dirname "$HERMES_SRC")"
if [[ ! -d $HERMES_SRC ]]; then
  git clone --filter=blob:none https://github.com/NousResearch/hermes-agent.git "$HERMES_SRC"
fi
git -C "$HERMES_SRC" fetch --tags origin main
git -C "$HERMES_SRC" checkout -q "tags/$HERMES_TAG"
# 补丁（含：确定性 @ 路由/入库 flag 剥离/续接恢复/MEDIA 渠道投递/飞书 mention 兼容）
git -C "$HERMES_SRC" apply --check "$REPO_DIR/patches/gateway-route-patch.diff" \
  || die "补丁与 $HERMES_TAG 源码冲突——检查 hermes-agent 是否为干净 $HERMES_TAG"
git -C "$HERMES_SRC" apply "$REPO_DIR/patches/gateway-route-patch.diff"
ok "网关补丁已应用（route-robust 979 行，4 文件）"

say "④ 安装 Python 包"
pipx install --python python3.14 -e "$HERMES_SRC" 2>/dev/null \
  || pipx upgrade --python python3.14 -e hermes-agent
export PATH="$HOME/.local/bin:$PATH"
hermes --version || die "hermes 安装失败"
pip3.14 install -e "$REPO_DIR" --quiet || pip3.14 install -e "$REPO_DIR"
solomon --help >/dev/null 2>&1 && ok "solomon CLI 就绪" || die "solomon CLI 安装失败"

say "⑤ 生成 profiles（配置骨架，凭据后填）"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
for p in coordinator solomon newsolomon; do
  mkdir -p "$HERMES_HOME/profiles/$p"
  cp -n "$REPO_DIR/profiles/$p/SOUL.md" "$HERMES_HOME/profiles/$p/SOUL.md" 2>/dev/null || true
  [[ -f "$HERMES_HOME/profiles/$p/config.yaml" ]] || \
    cp "$REPO_DIR/profiles/$p/config.yaml.example" "$HERMES_HOME/profiles/$p/config.yaml" 2>/dev/null || true
  [[ -f "$HERMES_HOME/profiles/$p/.env" ]] || \
    cp "$REPO_DIR/profiles/env.example" "$HERMES_HOME/profiles/$p/.env" 2>/dev/null || true
done
ok "profiles 骨架就绪：$HERMES_HOME/profiles/"

say "⑥ SenseNova 代理（可选，node 常驻）"
if [[ "${SKIP_INTERACTIVE:-0}" != "1" ]]; then
  read -rp "现在配置 SenseNova API Key 并装为 systemd 服务？[y/N] " yn
  if [[ $yn == y* ]]; then
    read -rp "SenseNova API Key: " SK
    for f in sensenova-proxy.js sensenova-flashlite-proxy.js; do
      sed -i "s/SK_YOUR_KEY_1/$SK/g" "$REPO_DIR/scripts/sensenova-proxy/$f"
    done
    mkdir -p ~/.config/systemd/user
    for pair in "sensenova-proxy:sensenova-proxy.js" "sensenova-flashlite-proxy:sensenova-flashlite-proxy.js"; do
      svc=${pair%%:*}; js=${pair##*:}
      cat > ~/.config/systemd/user/$svc.service <<UNIT
[Unit]
Description=Solomon $svc
[Service]
ExecStart=/usr/bin/node $REPO_DIR/scripts/sensenova-proxy/$js
WorkingDirectory=$REPO_DIR/scripts/sensenova-proxy
Restart=always
[Install]
WantedBy=default.target
UNIT
    done
    systemctl --user daemon-reload
    systemctl --user enable --now sensenova-proxy sensenova-flashlite-proxy
    sleep 2 && curl -s http://127.0.0.1:3456/health && ok "代理 3456 健康"
  fi
fi

say "⑦ 验证"
export SOLOMON_VAULT="${SOLOMON_VAULT:-$HOME/solomon-vault}"
solomon doctor && ok "solomon doctor 通过"
echo
echo -e "\033[1;32m══ 安装完成。接下来：══\033[0m"
echo "  1. 编辑 $HERMES_HOME/profiles/*/.env（渠道凭据/SOLOMON_VAULT 等真实值）"
echo "  2. solomon init && solomon ingest <B站视频URL>   # 初始化知识库并入库第一个视频"
echo "  3. hermes --profile coordinator gateway run      # 启动网关（或配 systemd）"
echo "  4. 完整文档：$REPO_DIR/DEPLOY.md；Agent 辅助部署：docs/DEPLOY-WITH-AGENT.md"
