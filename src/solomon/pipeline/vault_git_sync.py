#!/usr/bin/env python3
"""vault_git_sync.py — vault 知识库 git 自动同步（solomon 系统内建推送能力）。

vault 本身是 git 仓库且配置了 origin remote 时，把变更自动 commit + push。
触发点：入库完成 / 笔记删除 / 个人档案写入（fire-and-forget 后台）+ cron 每日兜底。

设计：
- 通用：不关心远端在哪（GitHub/任意 git remote），vault 有 origin 就推；
  无 .git / 无 origin / 无变更 → 静默跳过（开源用户无此配置即无感）。
- 不阻塞主线：调用方以「独立子进程」方式拉起本模块（start_new_session），
  推送耗时（图片上传）不影响入库返回；本进程失败也不影响入库结果。
- 防重入：flock（/tmp）；GIT_DIR 等环境污染主动清除（启动环境有 GIT_DIR= 空串）。

用法：
    python3.14 vault_git_sync.py          # 同步执行（cron / 调用方 Popen 拉起）
    python3.14 -c "from vault_git_sync import auto_sync; auto_sync()"
"""

import fcntl
import os
import subprocess
import sys

from _paths import default_vault  # noqa: E402

LOCK = "/tmp/solomon_vault_git_sync.lock"


def _git(vault, *args, check=True):
    env = dict(os.environ)
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):  # 启动环境可能注入空串
        env.pop(k, None)
    return subprocess.run(["git", "-C", vault, *args], capture_output=True,
                          text=True, timeout=300, env=env, check=check)


def auto_sync() -> bool:
    """同步 vault 到其 origin。返回是否发生了推送（跳过/失败返回 False）。"""
    vault = os.environ.get("SOLOMON_VAULT") or default_vault()
    if not vault or not os.path.isdir(os.path.join(vault, ".git")):
        return False
    try:
        remotes = _git(vault, "remote").stdout.split()
        if "origin" not in remotes:
            return False

        lock = open(LOCK, "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False  # 上一次同步还在跑

        _git(vault, "add", "-A")
        staged = _git(vault, "diff", "--cached", "--numstat").stdout.strip()
        if not staged:
            return False
        n = len(staged.splitlines())
        # branch --show-current 不依赖 HEAD 有提交（新仓库首推也能拿到分支名）
        branch = _git(vault, "branch", "--show-current").stdout.strip() or "master"
        _git(vault, "commit", "-q", "-m", f"vault 自动同步：{n} 个文件变更")
        _git(vault, "push", "-q", "origin", branch)
        print(f"[vault-sync] 推送成功：{n} 个文件 → origin/{branch}", file=sys.stderr)
        return True
    except subprocess.CalledProcessError as e:
        print(f"[vault-sync] 同步失败（下次触发重试）: {e.stderr[-500:]}", file=sys.stderr)
        return False
    except Exception as e:  # noqa: BLE001 同步失败绝不影响调用方（入库/删除）
        print(f"[vault-sync] 异常: {e}", file=sys.stderr)
        return False


def spawn_async():
    """独立子进程拉起自动同步（fire-and-forget：不阻塞调用方、失败不外抛）。

    供入库/删除/档案写入的完成路径调用——推送耗时（图片上传）不拖慢主线。"""
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    auto_sync()
