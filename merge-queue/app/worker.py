"""后台合并队列 worker。

处理流程（严格按入队顺序，每次只处理队首一项）：
1. 取最早的 PENDING 项 -> TESTING，记录当前主干 base；
2. 在独立 worktree 检出 base，合并入队时记下的分支提交；
3. 对合成提交跑样例测试（测试期间主干允许照常推进）；
4. 测试通过后再读主干：
   - 主干仍等于 base：update-ref CAS 推进到合成提交，成功则 MERGED；
   - 主干已变化：本次结果作废（run=OBSOLETE），项回到 PENDING，
     下一轮用新主干重做；
5. 基底仍是当前主干时，合并冲突 -> CONFLICT、测试失败 -> FAILED，
   记录原因并继续处理下一项；若处理失败过程中主干也变了，则同样作废重做。
"""
from __future__ import annotations

import logging
import threading
import time

from . import db, git_tools, retention
from .config import settings
from .runner import run_tests

log = logging.getLogger("merge-queue.worker")


class Worker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="mq-worker",
                                        daemon=True)
        self._thread.start()
        log.info("worker started")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # pragma: no cover - 兜底，线程不能死
                log.exception("worker tick 异常，稍后重试")
            self._stop.wait(settings.worker_interval)

    # ------------------------------------------------------------------
    def tick(self) -> bool:
        """处理一次队首项；返回是否处理了一项。"""
        item = db.take_next_pending()
        if item is None:
            return False
        log.info("开始处理 item=%s branch=%s", item["id"], item["branch"])
        try:
            self._process(item)
        except Exception:
            log.exception("item=%s 处理异常，回到 PENDING 等待重试", item["id"])
            db.update_item(item["id"], status=db.PENDING,
                           reason="内部异常，等待自动重试")
        return True

    def _process(self, item: dict) -> None:
        item_id = item["id"]
        branch = item["branch"]
        commit = item["commit_sha"]

        head = git_tools.branch_head(branch)
        if head is None:
            # 分支被删除：无法再合并，记为失败并放行后续项
            db.update_item(item_id, status=db.FAILED,
                           reason=f"分支 {branch} 在中心仓库中已不存在")
            retention.release_if_unused(commit)
            return
        if head != commit:
            # 分支在入队后被更新（含 bundle 非快进替换）：仍按入队时
            # 登记的提交处理，对象由队列保留引用保护
            log.warning("item=%s 分支头 %s 与入队提交 %s 不一致，按入队提交处理",
                        item_id, head[:8], commit[:8])
        if not git_tools.commit_exists(commit):
            # 保留引用失效等极端情况：对象已被回收，无法验证，判终态防死循环
            db.update_item(item_id, status=db.FAILED,
                           reason=f"登记提交 {commit} 的对象已不在中心仓库"
                                  "（可能被 gc 回收），无法继续验证")
            retention.release_if_unused(commit)
            return

        base = git_tools.main_head()
        run_id = db.start_run(item_id, base)
        worktree = git_tools.create_worktree(base, f"item-{item_id}-run-{run_id}")
        try:
            outcome = git_tools.merge_in_worktree(worktree, branch, commit, base)
            if not outcome.ok:
                self._finish_negative(item_id, run_id, db.RUN_CONFLICT,
                                      outcome.reason, worktree, base, commit)
                return

            # 分支已包含在主干：无需测试，直接判定完成
            if outcome.result_sha == base:
                db.finish_run(run_id, db.RUN_MERGED, outcome.reason,
                              "跳过测试：无新增提交", base)
                db.update_item(item_id, status=db.MERGED, base_sha=base,
                               result_sha=base, reason=outcome.reason)
                retention.release_if_unused(commit)
                return

            test = run_tests(worktree)
            if not test.passed:
                self._finish_negative(item_id, run_id, db.RUN_FAILED,
                                      test.output, worktree, base, commit,
                                      result_sha=outcome.result_sha)
                return

            # 测试通过：CAS 推进主干
            current = git_tools.main_head()
            if current != base:
                self._obsolete(item_id, run_id, base, current,
                               outcome.result_sha, test.output,
                               reason="测试期间主干被推进")
                return
            if git_tools.cas_update_main(outcome.result_sha, base):
                db.finish_run(run_id, db.RUN_MERGED, None, test.output,
                              outcome.result_sha)
                db.update_item(item_id, status=db.MERGED, base_sha=base,
                               result_sha=outcome.result_sha, reason=None)
                retention.release_if_unused(commit)
                log.info("item=%s 已合并，main -> %s", item_id,
                         outcome.result_sha[:8])
            else:
                current = git_tools.main_head()
                self._obsolete(item_id, run_id, base, current,
                               outcome.result_sha, test.output,
                               reason="CAS 推进失败：主干已变化")
        finally:
            git_tools.remove_worktree(worktree)

    # ------------------------------------------------------------------
    def _finish_negative(self, item_id: int, run_id: int, run_status: str,
                         detail: str | None, worktree: str, base: str,
                         commit_sha: str,
                         result_sha: str | None = None) -> None:
        """冲突/失败收尾：若主干已变则作废重做，否则置终态并放行下一项。"""
        current = git_tools.main_head()
        if current != base:
            self._obsolete(item_id, run_id, base, current, result_sha, detail,
                           reason=f"处理期间主干被推进（原判定 {run_status} 作废）")
            return
        item_status = db.CONFLICT if run_status == db.RUN_CONFLICT else db.FAILED
        db.finish_run(run_id, run_status, detail, detail, result_sha)
        db.update_item(item_id, status=item_status, base_sha=base,
                       result_sha=result_sha, reason=detail)
        retention.release_if_unused(commit_sha)
        log.info("item=%s 终态 %s", item_id, item_status)

    def _obsolete(self, item_id: int, run_id: int, base: str, current: str,
                  result_sha: str | None, test_output: str | None,
                  reason: str) -> None:
        full_reason = (f"{reason}：base={base[:8]} -> 当前 main={current[:8]}，"
                       f"本次结果作废，将用新主干重新验证")
        db.finish_run(run_id, db.RUN_OBSOLETE, full_reason, test_output,
                      result_sha)
        db.reset_to_pending(item_id, full_reason)
        log.warning("item=%s %s", item_id, full_reason)


worker = Worker()
