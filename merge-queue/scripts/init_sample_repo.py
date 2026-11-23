"""创建样例仓库（bare）并推送到中心仓库路径。

主干 main 与四个分支：
- feature-good    ：正常新增功能，测试通过，干净合并；
- feature-conflict：与 main 修改同一行，合并必冲突；
- feature-broken  ：合并干净，但新增的测试会失败；
- feature-slow    ：测试睡眠 8 秒，便于在测试期间推进主干触发“作废重做”。

幂等：仅当中心仓库不存在 refs/ 时执行。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import settings  # noqa: E402
from app import git_tools  # noqa: E402

RUN_TESTS_SH = """#!/bin/sh
# 样例测试入口：合并队列对合成提交执行此脚本
set -e
python3 -m unittest discover -s tests -p 'test_*.py' -v
"""

INITIAL_TEST = """import unittest

from app import add


class TestAdd(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(1, 2), 3)


if __name__ == "__main__":
    unittest.main()
"""

APP_PY_MAIN = '''"""样例应用：合并队列对合并后的完整代码跑测试。"""


def add(a: int, b: int) -> int:
    return a + b
'''


def git(args: list[str], cwd: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd,
                          env=git_tools._git_env(),
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{proc.stdout}{proc.stderr}")
    return proc.stdout


def write(path: str, content: str, executable: bool = False) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    if executable:
        os.chmod(path, 0o755)


def commit_all(cwd: str, message: str) -> str:
    git(["add", "-A"], cwd)
    git(["commit", "-m", message], cwd)
    return git(["rev-parse", "HEAD"], cwd).strip()


def main() -> None:
    git_tools.ensure_central_repo()
    if git_tools.branch_head("main") is not None:
        print("中心仓库已存在 main，跳过样例初始化")
        return

    tmp = tempfile.mkdtemp(prefix="sample-init-")
    try:
        git(["init", "-b", "main", tmp], cwd=tmp)
        write(os.path.join(tmp, "app.py"), APP_PY_MAIN)
        write(os.path.join(tmp, "tests", "__init__.py"), "")
        write(os.path.join(tmp, "tests", "test_app.py"), INITIAL_TEST)
        write(os.path.join(tmp, "run_tests.sh"), RUN_TESTS_SH, executable=True)
        write(os.path.join(tmp, "README.md"),
              "# 样例仓库\n\n由 scripts/init_sample_repo.py 生成，供合并队列演示。\n")
        base = commit_all(tmp, "initial commit with passing tests")

        # feature-good：新增 multiply，测试通过
        git(["switch", "-c", "feature-good"], cwd=tmp)
        write(os.path.join(tmp, "app.py"),
              APP_PY_MAIN + "\n\ndef multiply(a: int, b: int) -> int:\n"
                            "    return a * b\n")
        write(os.path.join(tmp, "tests", "test_multiply.py"),
              "import unittest\n\nfrom app import multiply\n\n\n"
              "class TestMultiply(unittest.TestCase):\n"
              "    def test_multiply(self):\n"
              "        self.assertEqual(multiply(3, 4), 12)\n\n\n"
              "if __name__ == '__main__':\n"
              "    unittest.main()\n")
        commit_all(tmp, "feature-good: add multiply")
        good_head = git(["rev-parse", "HEAD"], cwd=tmp).strip()

        # feature-conflict：与 main 都改 README 标题行，合并必冲突
        git(["switch", "main"], cwd=tmp)
        git(["switch", "-c", "feature-conflict"], cwd=tmp)
        write(os.path.join(tmp, "README.md"),
              "# 样例仓库（冲突分支版）\n\n由 scripts/init_sample_repo.py 生成，"
              "供合并队列演示。\n")
        commit_all(tmp, "feature-conflict: tweak README title (will conflict)")

        # feature-broken：干净合并，但测试是坏的
        git(["switch", "main"], cwd=tmp)
        git(["switch", "-c", "feature-broken"], cwd=tmp)
        write(os.path.join(tmp, "tests", "test_broken.py"),
              "import unittest\n\n\nclass TestBroken(unittest.TestCase):\n"
              "    def test_always_fails(self):\n"
              "        self.assertEqual(1, 2, 'feature-broken 故意失败的测试')\n\n\n"
              "if __name__ == '__main__':\n"
              "    unittest.main()\n")
        commit_all(tmp, "feature-broken: add a failing test")

        # feature-slow：测试睡眠，留出“测试期间推进主干”的窗口
        git(["switch", "main"], cwd=tmp)
        git(["switch", "-c", "feature-slow"], cwd=tmp)
        write(os.path.join(tmp, "tests", "test_slow.py"),
              "import time\nimport unittest\n\n\n"
              "class TestSlow(unittest.TestCase):\n"
              "    def test_sleeps(self):\n"
              "        time.sleep(8)\n"
              "        self.assertTrue(True)\n\n\n"
              "if __name__ == '__main__':\n"
              "    unittest.main()\n")
        commit_all(tmp, "feature-slow: add slow test (8s)")

        git(["switch", "main"], cwd=tmp)
        # 让 main 自己也演进一次：改 README 标题行，与 feature-conflict 对冲
        write(os.path.join(tmp, "README.md"),
              "# 样例仓库（主干版）\n\n由 scripts/init_sample_repo.py 生成，"
              "供合并队列演示。\n")
        main_head = commit_all(tmp, "main: evolve README (conflicts with feature-conflict)")

        # 推送全部分支到中心 bare 仓库
        git(["push", settings.central_repo_dir,
             "main", "feature-good", "feature-conflict",
             "feature-broken", "feature-slow"], cwd=tmp)
        print("样例仓库初始化完成：")
        print(f"  main             = {main_head}")
        print(f"  feature-good     = {good_head}")
        for name in ("feature-conflict", "feature-broken", "feature-slow"):
            print(f"  {name:<16} = {git(['rev-parse', name], cwd=tmp).strip()}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
