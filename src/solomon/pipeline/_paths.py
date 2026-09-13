"""路径解析辅助：核心脚本共享的默认路径（无个人痕迹）。

设计：
- 优先环境变量（CLI 通过 config.to_env() 注入真实值）
- 缺省时自动定位仓库内资源：assets/ 与 SKILL_TEMPLATES 随包走
- VAULT/WORK_ROOT 缺省给空串（preflight 会报「vault 不存在」给出清晰提示，
  避免静默指向某个不存在/错误的目录）
"""

from __future__ import annotations

import os
from pathlib import Path

# 本文件在 src/solomon/pipeline/ 下 → 包根 = parents[1]（/src/solomon/）
_PKG_ROOT = Path(__file__).resolve().parents[1]


def default_assets() -> str:
    """脚本资产目录：优先 SKILL_ASSETS env，缺省 src/solomon/assets。"""
    return os.environ.get("SKILL_ASSETS", str(_PKG_ROOT / "assets"))


def default_templates() -> str:
    """笔记模板目录：优先 TEMPLATE_DIR env，缺省 src/solomon/assets/SKILL_TEMPLATES。"""
    return os.environ.get("TEMPLATE_DIR", str(_PKG_ROOT / "assets" / "SKILL_TEMPLATES"))


def default_vault() -> str:
    """知识库根：优先 SOLOMON_VAULT env，缺省空串（由 preflight 明确报错）。"""
    return os.environ.get("SOLOMON_VAULT", "")


def default_work_root() -> str:
    """视频中间产物根：优先 WORK_ROOT env，缺省空串（缺省由调用处判断）。"""
    return os.environ.get("WORK_ROOT", "")


def default_python() -> str:
    """子进程 python：优先 PYTHON_BIN env，缺省当前解释器。"""
    import sys
    return os.environ.get("PYTHON_BIN", sys.executable or "python3")


def default_pythonpath() -> str:
    """子进程 PYTHONPATH：优先 SOLOMON_PYTHONPATH env，缺省空（继承）。"""
    return os.environ.get("SOLOMON_PYTHONPATH", "")
