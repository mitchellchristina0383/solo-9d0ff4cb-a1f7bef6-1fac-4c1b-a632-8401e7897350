"""中心裸仓库与临时 worktree 上的 Git 操作。

设计要点：
- 中心仓库是 bare repo，所有合并在独立 linked worktree 中进行，互不干扰；
- worktree 与 bare 仓库共享对象库，合成提交天然可被 update-ref 引用；
- 主干推进一律使用 ``git update-ref <new> <old>`` 比较交换（CAS），
  因此测试期间主干被外部推进不会被覆盖。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

from .config import settings

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "merge-queue",
    "GIT_AUTHOR_EMAIL": "merge-queue@local",
    "GIT_COMMITTER_NAME": "merge-queue",
    "GIT_COMMITTER_EMAIL": "merge-queue@local",
}


class GitError(RuntimeError):
    def __init__(self, args: list[str], cwd: str, code: int, output: str):
        super().__init__(f"git {' '.join(args)} failed in {cwd} ({code}):\n{output}")
        self.code = code
        self.output = output


def _git_env() -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "GIT_TERMINAL_PROMPT": "0",
    }
    env.update(GIT_IDENTITY)
    return env


def run_git(args: list[str], cwd: str | None = None,
            check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_git_env(),
        capture_output=True,
        text=True,
        input=input_text,
    )
    if check and proc.returncode != 0:
        raise GitError(args, cwd or os.getcwd(), proc.returncode,
                       (proc.stdout or "") + (proc.stderr or ""))
    return proc


def ensure_central_repo() -> None:
    os.makedirs(settings.data_dir, exist_ok=True)
    os.makedirs(settings.worktrees_dir, exist_ok=True)
    if not os.path.isdir(os.path.join(settings.central_repo_dir, "refs")):
        run_git(["init", "--bare", "--initial-branch=main",
                 settings.central_repo_dir])


def branch_head(branch: str) -> str | None:
    proc = run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                   cwd=settings.central_repo_dir, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def is_valid_branch_name(branch: str) -> bool:
    """校验分支名能否安全地拼进 refs/heads/<branch>。"""
    if not branch or branch != branch.strip():
        return False
    proc = run_git(["check-ref-format", f"refs/heads/{branch}"],
                   cwd=settings.central_repo_dir, check=False)
    return proc.returncode == 0


def commit_exists(sha: str) -> bool:
    """登记提交的对象是否仍在中心仓库（gc 后可能丢失）。"""
    proc = run_git(["cat-file", "-e", f"{sha}^{{commit}}"],
                   cwd=settings.central_repo_dir, check=False)
    return proc.returncode == 0


def list_branches() -> list[str]:
    proc = run_git(["for-each-ref", "--format=%(refname:short)", "refs/heads"],
                   cwd=settings.central_repo_dir)
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def main_head() -> str:
    head = branch_head("main")
    if head is None:
        raise RuntimeError("中心仓库缺少 main 分支")
    return head


def create_worktree(commit: str, name: str) -> str:
    path = os.path.join(settings.worktrees_dir, name)
    if os.path.exists(path):
        shutil.rmtree(path)
    run_git(["worktree", "add", "--detach", path, commit],
            cwd=settings.central_repo_dir)
    return path


def remove_worktree(path: str) -> None:
    if not os.path.exists(path):
        return
    run_git(["worktree", "remove", "--force", path],
            cwd=settings.central_repo_dir, check=False)
    if os.path.exists(path):
        shutil.rmtree(path, ignore_errors=True)
    run_git(["worktree", "prune"], cwd=settings.central_repo_dir, check=False)


def is_ancestor(ancestor: str, descendant: str) -> bool:
    proc = run_git(["merge-base", "--is-ancestor", ancestor, descendant],
                   cwd=settings.central_repo_dir, check=False)
    return proc.returncode == 0


@dataclass
class MergeOutcome:
    ok: bool
    result_sha: str | None
    reason: str | None = None


def merge_in_worktree(worktree: str, branch: str, commit: str,
                      base: str) -> MergeOutcome:
    """在 worktree（已检出 base）上合并分支提交。"""
    if is_ancestor(commit, base):
        return MergeOutcome(True, base,
                            reason="分支提交已包含在主干，无需合并")
    args = ["merge", "--no-ff", "--no-edit", commit,
            "-m", f"merge-queue: merge '{branch}' ({commit[:10]}) "
                  f"into main ({base[:10]})"]
    proc = run_git(args, cwd=worktree, check=False)
    if proc.returncode != 0:
        # 收集冲突文件清单后中止合并，保持 worktree 干净
        names = run_git(["diff", "--name-only", "--diff-filter=U"],
                        cwd=worktree, check=False).stdout.strip()
        run_git(["merge", "--abort"], cwd=worktree, check=False)
        if names:
            detail = f"合并冲突（{', '.join(names.splitlines())}）"
        else:
            detail = "合并失败（未产生文件级冲突标记，可能是提交不可达等原因）"
        reason = f"{detail}。git 输出：\n{proc.stdout}{proc.stderr}".strip()
        return MergeOutcome(False, None, reason=reason)
    result = run_git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip()
    return MergeOutcome(True, result)


def cas_update_main(new_sha: str, expected_old: str) -> bool:
    """比较交换推进主干；主干已变化时返回 False 而不报错。"""
    proc = run_git(["update-ref", "refs/heads/main", new_sha, expected_old],
                   cwd=settings.central_repo_dir, check=False)
    return proc.returncode == 0


# ----------------------------------------------------------------------
# 队列保留引用：防止已入队登记提交在非快进替换/重启后被 gc 回收
# ----------------------------------------------------------------------

RETAIN_PREFIX = "refs/queue-retain/"


def retain_ref_name(sha: str) -> str:
    return f"{RETAIN_PREFIX}{sha}"


def ensure_retain_ref(sha: str) -> None:
    """为登记提交建立保留引用（幂等）。提交对象必须存在。"""
    run_git(["update-ref", retain_ref_name(sha), sha],
            cwd=settings.central_repo_dir)


def drop_retain_ref(sha: str) -> None:
    """删除保留引用；不存在时静默成功。"""
    run_git(["update-ref", "-d", retain_ref_name(sha)],
            cwd=settings.central_repo_dir, check=False)


def list_retain_refs() -> list[str]:
    """列出当前所有被保留的提交号。"""
    proc = run_git(["for-each-ref", "--format=%(refname)", RETAIN_PREFIX],
                   cwd=settings.central_repo_dir)
    out = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith(RETAIN_PREFIX):
            out.append(line[len(RETAIN_PREFIX):])
    return out


def advance_main_with_commit(filename: str = "mainlog.md",
                             content_line: str | None = None) -> str:
    """模拟“测试期间主干照常推进”：在临时 worktree 基于当前 main 追加提交。"""
    base = main_head()
    wt = create_worktree(base, f"sim-advance-{base[:8]}")
    try:
        target = os.path.join(wt, filename)
        os.makedirs(os.path.dirname(target) or wt, exist_ok=True)
        line = content_line or f"main advanced at commit based on {base[:10]}"
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        run_git(["add", filename], cwd=wt)
        run_git(["commit", "-m", f"simulate external main advance: {filename}"],
                cwd=wt)
        new_sha = run_git(["rev-parse", "HEAD"], cwd=wt).stdout.strip()
        if not cas_update_main(new_sha, base):
            raise RuntimeError("模拟推进时主干已被他人更新，请重试")
        return new_sha
    finally:
        remove_worktree(wt)
