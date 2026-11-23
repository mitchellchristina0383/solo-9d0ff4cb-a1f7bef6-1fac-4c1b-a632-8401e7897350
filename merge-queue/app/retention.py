"""入队提交的保留（防 git gc）与释放。

背景：源分支可被 bundle 非快进替换，旧提交随即脱离任何分支；若此时仍有
队列项登记着旧提交，``git gc --prune=now`` 会把对象回收，重启后这些项将
永远无法处理。因此：

- 入队时为登记提交建立 ``refs/queue-retain/<sha>`` 保留引用（与写库在
  同一把进程锁内完成，避免与释放竞态）；
- 队列项进入终态后检查：若已没有任何活跃项引用该提交，删除保留引用；
- 服务启动时按活跃项重建保留引用，并清掉不再被引用的残留引用。
"""
from __future__ import annotations

import logging
from typing import Any

from . import db, git_tools

log = logging.getLogger("merge-queue.retention")


def enqueue_with_retention(branch: str, commit_sha: str) -> dict[str, Any]:
    """建立保留引用并登记队列项（原子于进程内锁）。"""
    with db.locked():
        git_tools.ensure_retain_ref(commit_sha)
        return db.create_item(branch, commit_sha)


def release_if_unused(commit_sha: str) -> None:
    """引用该提交的活跃项归零时，解除队列对它的保留。

    在队列项置终态之后调用：释放失败（如 git 异常）只允许遗留一个
    无害的引用（启动时重建会清理），绝不能把已终态的项弹回队列，
    因此这里吞掉异常只记日志。
    """
    try:
        with db.locked():
            if db.count_active_with_commit(commit_sha) == 0:
                git_tools.drop_retain_ref(commit_sha)
                log.info("提交 %s 已无活跃队列项引用，解除保留",
                         commit_sha[:10])
    except Exception:
        log.warning("释放保留引用失败（将在下次启动重建时清理）: %s",
                    commit_sha, exc_info=True)


def rebuild_on_startup() -> None:
    """重启后按活跃项重建保留；清理不再被引用的残留保留引用。"""
    active = set(db.list_active_commits())
    for sha in sorted(active):
        try:
            git_tools.ensure_retain_ref(sha)
        except Exception:
            log.warning("重建保留引用失败（提交对象可能已丢失）: %s", sha,
                        exc_info=True)
    for sha in git_tools.list_retain_refs():
        if sha not in active:
            git_tools.drop_retain_ref(sha)
            log.info("清理无活跃项引用的残留保留引用: %s", sha[:10])
