"""worker 关键分支的单元测试（不跑真实测试，用桩替换 git/runner）。

运行：MQ_DATA_DIR 指向临时目录，python -m unittest discover -s tests
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_tmp = tempfile.mkdtemp(prefix="mq-unittest-")
os.environ["MQ_DATA_DIR"] = _tmp

from app import db, git_tools  # noqa: E402
from app.config import settings  # noqa: E402
from app.worker import Worker  # noqa: E402


class WorkerLogicTest(unittest.TestCase):
    def setUp(self) -> None:
        # 全局连接在模块导入时已建立，删文件不会清空表；直接清表保证用例隔离
        db.init_db()
        with db.transaction() as conn:
            conn.execute("DELETE FROM runs")
            conn.execute("DELETE FROM items")
        self.worker = Worker()

    def _enqueue(self) -> dict:
        return db.create_item("feature-x", "a" * 40)

    def test_conflict_then_main_changed_marks_obsolete(self) -> None:
        """合并冲突的同时主干已推进：应作废重做，而不是 CONFLICT。"""
        item = self._enqueue()

        heads = iter(["deadbeef" * 5, "cafebabe" * 5])  # base, 处理后 current
        with mock.patch.object(git_tools, "main_head",
                               side_effect=lambda: next(heads)), \
             mock.patch.object(git_tools, "branch_head",
                               return_value=item["commit_sha"]), \
             mock.patch.object(git_tools, "create_worktree",
                               return_value="/tmp/wt-fake"), \
             mock.patch.object(git_tools, "remove_worktree"), \
             mock.patch.object(git_tools, "merge_in_worktree",
                               return_value=git_tools.MergeOutcome(
                                   False, None, reason="冲突")), \
             mock.patch("app.worker.db.start_run", wraps=db.start_run) as sr:
            self.worker.tick()

        updated = db.get_item(item["id"])
        self.assertEqual(updated["status"], db.PENDING)
        self.assertIn("作废", updated["reason"])
        runs = db.list_runs(item["id"])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], db.RUN_OBSOLETE)

    def test_conflict_with_stable_base_marks_conflict(self) -> None:
        """基底未变时冲突应置 CONFLICT 终态，队列放行下一项。"""
        item = self._enqueue()
        base = "11111111" * 5
        with mock.patch.object(git_tools, "main_head", return_value=base), \
             mock.patch.object(git_tools, "branch_head",
                               return_value=item["commit_sha"]), \
             mock.patch.object(git_tools, "create_worktree",
                               return_value="/tmp/wt-fake"), \
             mock.patch.object(git_tools, "remove_worktree"), \
             mock.patch.object(git_tools, "merge_in_worktree",
                               return_value=git_tools.MergeOutcome(
                                   False, None, reason="冲突文件 x")):
            self.worker.tick()

        updated = db.get_item(item["id"])
        self.assertEqual(updated["status"], db.CONFLICT)
        self.assertIn("冲突文件 x", updated["reason"])
        self.assertEqual(db.list_runs(item["id"])[0]["status"],
                         db.RUN_CONFLICT)

    def test_cas_failure_marks_obsolete_and_merges_on_retry(self) -> None:
        """测试通过但 CAS 推进失败（竞争）：作废；下一轮用新主干成功。"""
        item = self._enqueue()
        base1, base2 = "22222222" * 5, "33333333" * 5
        result1, result2 = "44444444" * 5, "55555555" * 5

        from app.runner import TestResult
        ok_test = TestResult(True, "all green", "sh run_tests.sh")
        good_merge = git_tools.MergeOutcome(True, result1)

        # 第一轮：CAS 失败
        with mock.patch.object(git_tools, "main_head",
                               side_effect=[base1, base1, base2]), \
             mock.patch.object(git_tools, "branch_head",
                               return_value=item["commit_sha"]), \
             mock.patch.object(git_tools, "create_worktree",
                               return_value="/tmp/wt-fake"), \
             mock.patch.object(git_tools, "remove_worktree"), \
             mock.patch.object(git_tools, "merge_in_worktree",
                               return_value=good_merge), \
             mock.patch("app.worker.run_tests", return_value=ok_test), \
             mock.patch.object(git_tools, "cas_update_main",
                               return_value=False):
            self.worker.tick()

        updated = db.get_item(item["id"])
        self.assertEqual(updated["status"], db.PENDING)
        self.assertEqual(db.list_runs(item["id"])[0]["status"],
                         db.RUN_OBSOLETE)

        # 第二轮：新基底，CAS 成功
        with mock.patch.object(git_tools, "main_head", return_value=base2), \
             mock.patch.object(git_tools, "branch_head",
                               return_value=item["commit_sha"]), \
             mock.patch.object(git_tools, "create_worktree",
                               return_value="/tmp/wt-fake2"), \
             mock.patch.object(git_tools, "remove_worktree"), \
             mock.patch.object(git_tools, "merge_in_worktree",
                               return_value=git_tools.MergeOutcome(True, result2)), \
             mock.patch("app.worker.run_tests", return_value=ok_test), \
             mock.patch.object(git_tools, "cas_update_main",
                               return_value=True) as cas:
            self.worker.tick()
            cas.assert_called_once_with(result2, base2)

        updated = db.get_item(item["id"])
        self.assertEqual(updated["status"], db.MERGED)
        self.assertEqual(updated["base_sha"], base2)
        self.assertEqual(updated["result_sha"], result2)
        self.assertEqual(updated["attempts"], 2)


if __name__ == "__main__":
    unittest.main()
