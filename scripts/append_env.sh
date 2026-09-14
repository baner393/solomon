#!/bin/bash
# 给 solomon/newsolomon/coordinator 三个 profile 的 .env 追加 solomon 管线变量（幂等）
set -uo pipefail
for p in solomon newsolomon coordinator; do
  f="/home/baner/.hermes/profiles/$p/.env"
  if [ ! -f "$f" ]; then
    echo "$p: .env 不存在，跳过"
    continue
  fi
  if grep -q "^SOLOMON_VAULT=" "$f" 2>/dev/null; then
    echo "$p: SOLOMON_VAULT 已存在，跳过"
    continue
  fi
  printf "\n# solomon 知识库管线（2026-09-13 切换至 solomon 仓库后新增）\nSOLOMON_VAULT=/mnt/d/all/ai_agent_about/solomon\nWORK_ROOT=/mnt/d/ObsidianSpace/VideoNotes\n" >> "$f"
  echo "$p: 已追加"
done
echo "=== 验证 ==="
for p in solomon newsolomon coordinator; do
  echo "--- $p ---"
  grep "^SOLOMON_VAULT\|^WORK_ROOT" "/home/baner/.hermes/profiles/$p/.env" 2>/dev/null || echo "(无)"
done