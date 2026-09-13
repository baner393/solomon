"""solomon — 统一配置入口。

设计目标：
1. 所有可配置项集中在这里，脚本不再散落硬编码路径。
2. 加载顺序（后者覆盖前者）：系统环境变量 > 项目 .env 文件 > 代码默认值。
3. 默认值面向「新用户」：不含任何个人机器痕迹。个人化配置只进 .env（gitignore）。
4. 兼容旧环境变量名：SOLOMON_VAULT / WORK_ROOT / SKILL_ASSETS / TEMPLATE_DIR /
   HTTP_PROXY / PYTHON_BIN / WHISPER_MODEL —— 生产环境已有的 env 无需改动。

典型用法（在别的模块里）：
    from solomon import config
    vault = config.VAULT
"""

from __future__ import annotations

import os
from pathlib import Path

# ── 项目根定位 ────────────────────────────────────────────────
# src/solomon/config.py -> 仓库根（向上 2 层）
REPO_ROOT = Path(__file__).resolve().parents[2]

# 用户数据根（vault/work 默认放这里，与代码彻底分离；
# 知识库是私有内容，默认不进仓库。可用 .env 指向任意 Obsidian 库）
_DATA_ROOT = Path(os.environ.get("SOLOMON_DATA_ROOT", str(Path.home() / ".solomon")))

# ── .env 加载（最简单的手写 dotenv，避免额外依赖）─────────────
_ENV_FILE = Path(os.environ.get("SOLOMON_ENV_FILE", str(REPO_ROOT / ".env")))


def _load_dotenv(path: Path) -> None:
    """把 .env 里未设置的 KEY=value 灌进 os.environ（不覆盖已有环境变量）。"""
    if not path.exists():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError:
        pass


_load_dotenv(_ENV_FILE)


# ── 取值辅助 ───────────────────────────────────────────────────
def _get(key: str, default: str) -> str:
    return os.environ.get(key, default)


def sys_executable() -> str:
    import sys
    return sys.executable or "python3"


# ── 路径配置 ───────────────────────────────────────────────────
# 知识库根目录（vault）。默认 ~/.solomon/vault —— 用 `solomon init` 初始化，
# 或通过 SOLOMON_VAULT 指向自己的 Obsidian 库。⚠️ vault 内容私有，不进开源仓库。
VAULT = Path(_get("SOLOMON_VAULT", str(_DATA_ROOT / "vault")))

# 视频中间产物根目录（下载/转写/关键帧等 workdir 的父目录）
WORK_ROOT = Path(_get("WORK_ROOT", str(_DATA_ROOT / "work")))

# 脚本资产目录（关键帧/识图/标注/verify 等辅助脚本）
SKILL_ASSETS = Path(_get("SKILL_ASSETS", str(REPO_ROOT / "src" / "solomon" / "assets")))

# 笔记模板目录（视频类型 -> 笔记 prompt 模板）
TEMPLATE_DIR = Path(_get(
    "TEMPLATE_DIR",
    str(REPO_ROOT / "src" / "solomon" / "assets" / "SKILL_TEMPLATES"),
))


# ── 运行时配置 ─────────────────────────────────────────────────
# 出网代理（yt-dlp / 下载用）。空串 = 不走代理。常见 http://127.0.0.1:7890
PROXY = _get("HTTP_PROXY", "")

# 转写/子进程 python 解释器。默认用当前解释器（这是最稳的：与主进程同环境）。
PYTHON = _get("PYTHON_BIN", sys_executable())

# 子进程可用的 PYTHONPATH（转写子进程、assets 脚本需要）。空 = 继承当前。
SUBPROCESS_PYTHONPATH = _get("SOLOMON_PYTHONPATH", "")

# 字幕降级转写模型（whisper 系列甜点）
WHISPER_MODEL = _get("WHISPER_MODEL", "medium")

# 会话/仓库 key 等（供 HITL 审批 / 多级部署）
MAX_PARTS = int(_get("SOLOMON_MAX_PARTS", "5"))


def ensure_dirs() -> None:
    """确保 vault 与 work 目录存在（幂等）。"""
    VAULT.mkdir(parents=True, exist_ok=True)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    (VAULT / ".kb").mkdir(parents=True, exist_ok=True)


def to_env() -> dict:
    """导出当前配置为子进程环境变量（继承 os.environ 并覆盖已配项）。"""
    env = dict(os.environ)
    env["SOLOMON_VAULT"] = str(VAULT)
    env["WORK_ROOT"] = str(WORK_ROOT)
    env["SKILL_ASSETS"] = str(SKILL_ASSETS)
    env["TEMPLATE_DIR"] = str(TEMPLATE_DIR)
    # FTS 索引/查询缓存目录（默认 vault/.kb，随 vault 走；可显式指定）
    env["SOLOMON_FTS_DB"] = _get("SOLOMON_FTS_DB", str(VAULT / ".kb"))
    if PROXY:
        env["HTTP_PROXY"] = PROXY
    env["PYTHON_BIN"] = PYTHON
    if SUBPROCESS_PYTHONPATH:
        env["PYTHONPATH"] = SUBPROCESS_PYTHONPATH
    return env


def describe() -> str:
    """简述当前配置（供 `solomon doctor` / 启动横幅展示）。"""
    lines = [
        f"  vault        : {VAULT}",
        f"  work_root    : {WORK_ROOT}",
        f"  assets       : {SKILL_ASSETS}",
        f"  templates    : {TEMPLATE_DIR}",
        f"  proxy        : {PROXY or '(none)'}",
        f"  python       : {PYTHON}",
        f"  whisper_model: {WHISPER_MODEL}",
    ]
    return "\n".join(lines)