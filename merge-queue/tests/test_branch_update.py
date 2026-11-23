"""源分支 bundle 更新与提交保留的测试（真实 git 操作，临时目录）。

运行：python -m unittest discover -s tests
注意：与 test_worker_logic.py 同进程发现时共享同一个 MQ_DATA_DIR
（先导入者生效），两侧 setUp 都会清理队列表，互不干扰。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_tmp = tempfile.mkdtemp(prefix="mq-bu-test-")
os.environ["MQ_DATA_DIR"] = _tmp

from app import bundle_update, db, git_tools, retention  # noqa: E402
from app.config import settings  # noqa: E402


def git(cwd: str, *args: str) -> str:
    return git_tools.run_git(list(args), cwd=cwd).stdout.strip()


class CentralRepoTestCase(unittest.TestCase):
    """提供已初始化的中心仓库与源仓库的基类。"""

    @classmethod
    def setUpClass(cls) -> None:
        git_tools.ensure_central_repo()
        db.init_db()
        cls.src = tempfile.mkdtemp(prefix="mq-bu-src-")
        git(cls.src, "init", "-b", "main", ".")
        cls._write("app.py", "def add(a, b):\n    return a + b\n")
        git(cls.src, "add", "-A")
        git(cls.src, "commit", "-m", "base")
        cls.main_sha = git(cls.src, "rev-parse", "HEAD")
        # 多个测试类共享同一中心仓库，各自重建 main：强制覆盖
        git(cls.src, "push", "--force", settings.central_repo_dir, "main")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.src, ignore_errors=True)

    @classmethod
    def _write(cls, name: str, content: str) -> None:
        with open(os.path.join(cls.src, name), "w", encoding="utf-8") as fh:
            fh.write(content)

    def setUp(self) -> None:
        with db.transaction() as conn:
            conn.execute("DELETE FROM runs")
            conn.execute("DELETE FROM items")
        for sha in git_tools.list_retain_refs():
            git_tools.drop_retain_ref(sha)

    # ------------------------------------------------------------------
    def make_branch(self, name: str, filename: str, content: str,
                    base: str = "main") -> str:
        """在源仓库基于 base 建分支、提交一个文件并推到中心仓库。"""
        git(self.src, "switch", "-C", name, base)
        self._write(filename, content)
        git(self.src, "add", "-A")
        git(self.src, "commit", "-m", f"{name}: update {filename}")
        sha = git(self.src, "rev-parse", "HEAD")
        git(self.src, "push", "--force", settings.central_repo_dir, name)
        return sha

    def make_bundle(self, name: str, *revisions: str) -> str:
        path = os.path.join(tempfile.mkdtemp(prefix="mq-bundle-"),
                            f"{name}.bundle")
        git(self.src, "bundle", "create", path, *revisions)
        return path


class BundleUpdateTest(CentralRepoTestCase):

    def test_successful_non_ff_replace(self) -> None:
        old = self.make_branch("feat-nff", "a.txt", "old\n")
        # 从 main 重新长出分叉提交：与 old 无祖先关系 -> 非快进
        git(self.src, "switch", "-C", "feat-nff-2", "main")
        self._write("b.txt", "new\n")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-m", "divergent commit")
        new = git(self.src, "rev-parse", "HEAD")
        bundle = self.make_bundle("nff", "feat-nff-2")

        result = bundle_update.update_branch_from_bundle(
            bundle, "feat-nff", old)

        self.assertEqual(result.old_sha, old)
        self.assertEqual(result.new_sha, new)
        self.assertEqual(git_tools.branch_head("feat-nff"), new)
        # 确为非快进：旧提交不是新头的祖先
        self.assertFalse(git_tools.is_ancestor(old, new))
        # 暂存引用已清理
        refs = git(settings.central_repo_dir, "for-each-ref",
                   "refs/mq-stage")
        self.assertEqual(refs, "")

    def test_reject_main_target(self) -> None:
        self.make_branch("feat-m", "a.txt", "x\n")
        bundle = self.make_bundle("m", "feat-m")
        with self.assertRaises(bundle_update.UpdateRejected) as ctx:
            bundle_update.update_branch_from_bundle(
                bundle, "main", self.main_sha)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("main", ctx.exception.detail)

    def test_reject_bad_sha_format(self) -> None:
        self.make_branch("feat-s", "a.txt", "x\n")
        bundle = self.make_bundle("s", "feat-s")
        with self.assertRaises(bundle_update.UpdateRejected) as ctx:
            bundle_update.update_branch_from_bundle(
                bundle, "feat-s", "not-a-sha")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_reject_nonexistent_branch(self) -> None:
        self.make_branch("feat-n", "a.txt", "x\n")
        bundle = self.make_bundle("n", "feat-n")
        with self.assertRaises(bundle_update.UpdateRejected) as ctx:
            bundle_update.update_branch_from_bundle(
                bundle, "no-such-branch", "0" * 40)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_reject_stale_expected_old(self) -> None:
        old = self.make_branch("feat-stale", "a.txt", "x\n")
        self.make_branch("feat-stale", "a.txt", "y\n")  # 头已移动
        bundle = self.make_bundle("stale", "feat-stale")
        with self.assertRaises(bundle_update.UpdateRejected) as ctx:
            bundle_update.update_branch_from_bundle(bundle, "feat-stale", old)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_reject_same_commit(self) -> None:
        head = self.make_branch("feat-same", "a.txt", "x\n")
        bundle = self.make_bundle("same", "feat-same")
        with self.assertRaises(bundle_update.UpdateRejected) as ctx:
            bundle_update.update_branch_from_bundle(bundle, "feat-same", head)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("相同", ctx.exception.detail)

    def test_reject_garbage_file(self) -> None:
        head = self.make_branch("feat-g", "a.txt", "x\n")
        junk = os.path.join(tempfile.mkdtemp(prefix="mq-junk-"), "junk.bundle")
        with open(junk, "w", encoding="utf-8") as fh:
            fh.write("this is not a git bundle\n")
        with self.assertRaises(bundle_update.UpdateRejected) as ctx:
            bundle_update.update_branch_from_bundle(junk, "feat-g", head)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(git_tools.branch_head("feat-g"), head)  # 原分支不动

    def test_reject_thin_bundle(self) -> None:
        head = self.make_branch("feat-thin", "a.txt", "x\n")
        # 本地再进一步，使包尖与当前分支头不同（否则先命中“相同提交”）
        self._write("a.txt", "y\n")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-m", "one more on top")
        # main..feat-thin：薄包，前置提交 main 不在空仓库 -> 不可导入空仓库
        bundle = self.make_bundle("thin", "main..feat-thin")
        with self.assertRaises(bundle_update.UpdateRejected) as ctx:
            bundle_update.update_branch_from_bundle(bundle, "feat-thin", head)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("空仓库", ctx.exception.detail)
        self.assertEqual(git_tools.branch_head("feat-thin"), head)

    def test_reject_multi_branch_bundle(self) -> None:
        head = self.make_branch("feat-one", "a.txt", "1\n")
        self.make_branch("feat-two", "b.txt", "2\n")
        bundle = self.make_bundle("multi", "feat-one", "feat-two")
        with self.assertRaises(bundle_update.UpdateRejected) as ctx:
            bundle_update.update_branch_from_bundle(bundle, "feat-one", head)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("一个分支", ctx.exception.detail)
        self.assertEqual(git_tools.branch_head("feat-one"), head)

    def test_concurrent_replace_same_old_head_only_one_wins(self) -> None:
        old = self.make_branch("feat-race", "a.txt", "base\n")
        bundles = []
        for i in (1, 2):
            git(self.src, "switch", "-C", f"race-{i}", "main")
            self._write(f"race{i}.txt", f"{i}\n")
            git(self.src, "add", "-A")
            git(self.src, "commit", "-m", f"race candidate {i}")
            bundles.append((git(self.src, "rev-parse", "HEAD"),
                            self.make_bundle(f"race-{i}", f"race-{i}")))

        results: list[object] = []

        def attempt(bundle: str) -> None:
            try:
                results.append(bundle_update.update_branch_from_bundle(
                    bundle, "feat-race", old))
            except bundle_update.UpdateRejected as exc:
                results.append(exc)

        threads = [threading.Thread(target=attempt, args=(b,))
                   for _, b in bundles]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        wins = [r for r in results
                if isinstance(r, bundle_update.UpdateResult)]
        losses = [r for r in results
                  if isinstance(r, bundle_update.UpdateRejected)]
        self.assertEqual(len(wins), 1, f"应恰好一个成功：{results!r}")
        self.assertEqual(len(losses), 1)
        self.assertEqual(losses[0].status_code, 409)
        head = git_tools.branch_head("feat-race")
        self.assertEqual(head, wins[0].new_sha)
        self.assertIn(head, {sha for sha, _ in bundles})


class RetentionTest(CentralRepoTestCase):

    def test_pin_on_enqueue_release_after_last_item_terminal(self) -> None:
        sha = self.make_branch("feat-ret", "a.txt", "x\n")
        item1 = retention.enqueue_with_retention("feat-ret", sha)
        item2 = retention.enqueue_with_retention("feat-ret", sha)
        self.assertIn(sha, git_tools.list_retain_refs())

        # 第一项终态：仍有引用，保留不解除
        db.update_item(item1["id"], status=db.MERGED)
        retention.release_if_unused(sha)
        self.assertIn(sha, git_tools.list_retain_refs())

        # 全部终态：解除保留
        db.update_item(item2["id"], status=db.MERGED)
        retention.release_if_unused(sha)
        self.assertNotIn(sha, git_tools.list_retain_refs())

    def test_rebuild_on_startup_restores_and_cleans(self) -> None:
        sha = self.make_branch("feat-reb", "a.txt", "x\n")
        item = retention.enqueue_with_retention("feat-reb", sha)
        # 模拟保留引用丢失（如异常退出窗口）：重建应恢复
        git_tools.drop_retain_ref(sha)
        self.assertNotIn(sha, git_tools.list_retain_refs())
        retention.rebuild_on_startup()
        self.assertIn(sha, git_tools.list_retain_refs())

        # 项已终态但残留保留引用：重建应清理
        db.update_item(item["id"], status=db.FAILED)
        retention.rebuild_on_startup()
        self.assertNotIn(sha, git_tools.list_retain_refs())


class WorkerCommitMissingTest(CentralRepoTestCase):
    """登记提交对象已丢失时：判 FAILED 终态而不是无限重试。"""

    def test_missing_commit_marks_failed(self) -> None:
        from unittest import mock

        from app.worker import Worker

        sha = "b" * 40
        item = db.create_item("feat-gone", sha)
        with mock.patch.object(git_tools, "branch_head", return_value=sha), \
             mock.patch.object(git_tools, "commit_exists",
                               return_value=False):
            Worker().tick()
        updated = db.get_item(item["id"])
        self.assertEqual(updated["status"], db.FAILED)
        self.assertIn("已不在中心仓库", updated["reason"])


if __name__ == "__main__":
    unittest.main()
