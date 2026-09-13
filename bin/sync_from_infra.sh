#!/bin/bash
# 从 .hermes/infra 同步核心代码到 solomon 仓库（复制，不改原文件）
set -uo pipefail
SRC=/home/baner/.hermes/infra
DST=/home/baner/solomon/src/solomon

echo "=== 同步前 DST 内容 ==="
ls -la "$DST" 2>/dev/null | head

# 1. pipeline
cp -v "$SRC"/pipeline/*.py "$DST"/pipeline/ 2>&1 | tail -8
# 2. query
cp -v "$SRC"/query/query_kb.py "$DST"/query/ 2>/dev/null
# 3. assets 脚本 + 模板
cp -v "$SRC"/assets/*.py "$DST"/assets/ 2>&1 | tail -15
cp -rv "$SRC"/assets/SKILL_TEMPLATES "$DST"/assets/ 2>&1 | tail -3

echo "=== 同步后统计 ==="
find "$DST" -name "*.py" | wc -l
du -sh "$DST"