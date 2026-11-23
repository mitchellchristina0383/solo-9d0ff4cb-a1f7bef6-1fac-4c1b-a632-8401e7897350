"""在合成提交的 worktree 中运行样例测试。

测试入口优先级：
1. 仓库根目录的 ``run_tests.sh``（可执行则直接执行，否则用 sh 运行）；
2. ``pytest``（存在测试目录时）；
3. ``python3 -m unittest discover``。
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from .config import settings


@dataclass
class TestResult:
    passed: bool
    output: str
    command: str
    timed_out: bool = False


def _clean_env(worktree: str) -> dict[str, str]:
    # 最小化环境，避免宿主变量污染；PATH 保留以找到 git/python
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
        "HOME": worktree,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TMPDIR": "/tmp",
    }


def _run(cmd: list[str], cwd: str) -> TestResult:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=_clean_env(cwd),
            capture_output=True,
            text=True,
            timeout=settings.test_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return TestResult(
            False,
            f"测试超过 {settings.test_timeout}s 被终止。\n"
            f"stdout: {exc.stdout or ''}\nstderr: {exc.stderr or ''}",
            " ".join(cmd),
            timed_out=True,
        )
    output = (
        f"$ {' '.join(cmd)}\n--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}\n(exit={proc.returncode})"
    )
    return TestResult(proc.returncode == 0, output, " ".join(cmd))


def run_tests(worktree: str) -> TestResult:
    script = os.path.join(worktree, "run_tests.sh")
    if os.path.isfile(script):
        if os.access(script, os.X_OK):
            return _run(["./run_tests.sh"], worktree)
        return _run(["sh", "run_tests.sh"], worktree)

    has_pytest_dirs = any(
        os.path.isdir(os.path.join(worktree, d)) for d in ("tests", "test")
    )
    if has_pytest_dirs:
        try:
            return _run(["python3", "-m", "pytest", "-q", "tests"], worktree)
        except FileNotFoundError:
            pass
    return _run(["python3", "-m", "unittest", "discover", "-s", "tests",
                 "-p", "test_*.py"], worktree)
