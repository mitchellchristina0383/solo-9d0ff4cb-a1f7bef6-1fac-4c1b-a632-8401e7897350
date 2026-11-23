"""源分支更新：上传单分支 Git bundle，校验后非快进替换非 main 分支。

流程（验包不持任何锁，入队等其它请求照常进行）：
1. ``git bundle list-heads``：必须恰好一个 ``refs/heads/*`` 引用；
2. 在临时**空仓库**中 ``git bundle verify`` + ``git fetch``：证明包自包含、
   可导入空仓库（薄包/缺前置提交/损坏包在此被拒）；
3. 快速失败检查：目标分支存在、当前头等于预期旧提交、新旧提交不同；
4. 把包内分支抓取进中心仓库的暂存引用（对象入库）；
5. ``git update-ref <new> <expected_old>`` CAS 替换目标分支——并发下
   同一旧头最多一个请求成功；最后清理暂存引用。
"""
from __future__ import annotations

import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass

from . import git_tools
from .config import settings

SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
STAGING_PREFIX = "refs/mq-stage/"


class UpdateRejected(RuntimeError):
    """更新请求被拒；status_code 映射为 HTTP 响应码。"""

    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass
class UpdateResult:
    branch: str
    old_sha: str
    new_sha: str


def _valid_sha(sha: str) -> bool:
    return bool(SHA_RE.fullmatch(sha or ""))


def inspect_bundle(bundle_path: str) -> list[tuple[str, str]]:
    """列出 bundle 中的引用，返回 [(sha, refname)]；文件不是 bundle 时拒绝。"""
    proc = git_tools.run_git(["bundle", "list-heads", bundle_path],
                             cwd=settings.central_repo_dir, check=False)
    if proc.returncode != 0:
        raise UpdateRejected(
            "上传的文件不是有效的 Git bundle。git 输出：\n"
            f"{(proc.stdout + proc.stderr).strip()}")
    heads: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and _valid_sha(parts[0]):
            heads.append((parts[0].lower(), parts[1]))
    return heads


def _prove_importable_into_empty_repo(bundle_path: str, refname: str) -> None:
    """在临时空仓库中 verify + fetch，证明 bundle 自包含且对象完整。"""
    tmp = tempfile.mkdtemp(prefix="mq-bundle-verify-")
    try:
        git_tools.run_git(["init", "--bare", "--quiet", tmp])
        verify = git_tools.run_git(["bundle", "verify", bundle_path],
                                   cwd=tmp, check=False)
        if verify.returncode != 0:
            raise UpdateRejected(
                "bundle 校验失败：不是自包含的完整包（薄包缺前置提交、"
                "或文件损坏），无法导入空仓库。请用完整分支重新生成，例如 "
                "`git bundle create out.bundle <branch>`。git 输出：\n"
                f"{(verify.stdout + verify.stderr).strip()}")
        fetch = git_tools.run_git(
            ["fetch", "--quiet", bundle_path, f"{refname}:refs/heads/__import__"],
            cwd=tmp, check=False)
        if fetch.returncode != 0:
            raise UpdateRejected(
                "bundle 无法导入空仓库（对象不完整）。git 输出：\n"
                f"{(fetch.stdout + fetch.stderr).strip()}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def update_branch_from_bundle(bundle_path: str, target_branch: str,
                              expected_old_sha: str) -> UpdateResult:
    """校验 bundle 并 CAS 替换目标分支；拒绝时抛 UpdateRejected。"""
    # ---- 参数检查（廉价，先做） --------------------------------------
    if not git_tools.is_valid_branch_name(target_branch):
        raise UpdateRejected(f"非法目标分支名 {target_branch!r}")
    if target_branch == "main":
        raise UpdateRejected("主干 main 受保护：只能由合并队列推进，"
                             "不允许用 bundle 替换")
    if not _valid_sha(expected_old_sha):
        raise UpdateRejected(
            f"预期旧提交号 {expected_old_sha!r} 不是 40 位十六进制 SHA")

    # ---- 包内容检查：必须只带一个分支 --------------------------------
    heads = inspect_bundle(bundle_path)
    branch_refs = [(sha, ref) for sha, ref in heads
                   if ref.startswith("refs/heads/")]
    if len(heads) != 1 or len(branch_refs) != 1:
        found = ", ".join(ref for _, ref in heads) or "（无引用）"
        raise UpdateRejected(
            "bundle 必须只携带一个分支引用（refs/heads/<branch>），"
            f"实际包含：{found}")
    new_sha, bundle_ref = branch_refs[0]

    # ---- 分支现状检查（快速失败；最终一致性由 CAS 保证） --------------
    current = git_tools.branch_head(target_branch)
    if current is None:
        raise UpdateRejected(
            f"目标分支 {target_branch!r} 不存在于中心仓库，无法替换。"
            f"现有分支：{git_tools.list_branches()}", status_code=404)
    if current != expected_old_sha.lower():
        raise UpdateRejected(
            f"分支 {target_branch!r} 当前头为 {current}，"
            f"与预期旧提交 {expected_old_sha} 不一致；可能已被他人更新，"
            "请重新获取最新头后再试", status_code=409)
    if new_sha == current:
        raise UpdateRejected(
            "bundle 分支头与当前分支头相同：新旧提交号必须不同，无需替换")

    # ---- 验包：可导入空仓库（耗时步骤，不持锁，入队照常） --------------
    _prove_importable_into_empty_repo(bundle_path, bundle_ref)

    # ---- 对象入库 + CAS 替换 -----------------------------------------
    staging = f"{STAGING_PREFIX}update-{uuid.uuid4().hex}"
    try:
        fetch = git_tools.run_git(
            ["fetch", "--quiet", bundle_path, f"{bundle_ref}:{staging}"],
            cwd=settings.central_repo_dir, check=False)
        if fetch.returncode != 0:
            raise UpdateRejected(
                "bundle 对象导入中心仓库失败。git 输出：\n"
                f"{(fetch.stdout + fetch.stderr).strip()}")
        imported = git_tools.run_git(
            ["rev-parse", staging], cwd=settings.central_repo_dir
        ).stdout.strip()
        if imported != new_sha:
            raise UpdateRejected(
                f"导入结果 {imported} 与 bundle 声明的分支头 {new_sha} 不一致")

        cas = git_tools.run_git(
            ["update-ref", f"refs/heads/{target_branch}", new_sha,
             expected_old_sha.lower()],
            cwd=settings.central_repo_dir, check=False)
        if cas.returncode != 0:
            # 验包期间旧头被并发替换：拒绝并保留现有分支
            latest = git_tools.branch_head(target_branch)
            raise UpdateRejected(
                f"替换失败：分支 {target_branch!r} 的头在验包期间已变为 "
                f"{latest}，与预期旧提交 {expected_old_sha} 不一致；"
                "现有分支保持不变，请重新获取最新头后再试",
                status_code=409)
    finally:
        git_tools.run_git(["update-ref", "-d", staging],
                          cwd=settings.central_repo_dir, check=False)

    return UpdateResult(branch=target_branch,
                        old_sha=expected_old_sha.lower(), new_sha=new_sha)
