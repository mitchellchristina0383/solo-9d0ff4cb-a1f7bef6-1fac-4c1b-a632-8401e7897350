"""FastAPI 入口：/docs 可直接入队、查结果、模拟主干推进。"""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Query

from . import db, git_tools
from .config import settings
from .schemas import (
    AdvanceMainRequest,
    BranchOut,
    EnqueueRequest,
    EnqueueResponse,
    ItemDetail,
    ItemOut,
    MainOut,
    MessageOut,
    STATUS_DESCRIPTIONS,
)
from .worker import worker

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(
    title="单仓库 Git 合并队列",
    version="1.0.0",
    description=(
        "## 工作方式\n\n"
        "1. 分支入队时记录其当前提交号，按入队顺序（FIFO）与当前主干 main 合并；\n"
        "2. 在临时 worktree 中对合成提交运行样例测试，**测试期间主干可照常推进**；\n"
        "3. 测试通过且主干仍指向本次合并基底时，才把主干推进到受测提交（CAS）；\n"
        "4. 主干先变 -> 本次结果作废（OBSOLETE），项回到队首用新主干重做；\n"
        "5. 基底未变时遇到合并冲突或测试失败 -> 记录原因、置终态，继续处理下一项。\n\n"
        f"状态含义：{STATUS_DESCRIPTIONS}"
    ),
)


@app.on_event("startup")
def _startup() -> None:
    git_tools.ensure_central_repo()
    db.init_db()
    # 上次进程异常退出时可能残留 TESTING，复位为 PENDING
    for item in db.list_items(status=db.TESTING, limit=1000):
        db.reset_to_pending(item["id"], "服务重启，复位后重新验证")
    worker.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    worker.stop()


@app.post("/queue", response_model=EnqueueResponse, tags=["队列"],
          summary="分支入队（登记当前提交号）")
def enqueue(req: EnqueueRequest) -> EnqueueResponse:
    head = git_tools.branch_head(req.branch)
    if head is None:
        raise HTTPException(
            status_code=404,
            detail=f"分支 {req.branch!r} 不存在于中心仓库，"
                   f"现有分支：{git_tools.list_branches()}")
    item = db.create_item(req.branch, head)
    return EnqueueResponse(id=item["id"], branch=req.branch,
                           commit_sha=head, status=item["status"])


@app.get("/queue", response_model=list[ItemOut], tags=["队列"],
         summary="查看队列（可按状态过滤）")
def list_queue(status: str | None = Query(
        None, description="PENDING/TESTING/MERGED/CONFLICT/FAILED/CANCELLED")) \
        -> list[dict]:
    if status is not None and status not in (
            *db.ACTIVE_STATUSES, *db.TERMINAL_STATUSES):
        raise HTTPException(status_code=400, detail=f"未知状态 {status!r}")
    return db.list_items(status=status)


@app.get("/queue/{item_id}", response_model=ItemDetail, tags=["队列"],
         summary="查询单项结果（含每次尝试与作废记录）")
def get_item(item_id: int) -> ItemDetail:
    item = db.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"队列项 {item_id} 不存在")
    return ItemDetail(item=item, runs=db.list_runs(item_id))


@app.post("/queue/{item_id}/cancel", response_model=ItemOut, tags=["队列"],
          summary="取消排队中的项")
def cancel(item_id: int) -> dict:
    item = db.cancel_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"队列项 {item_id} 不存在")
    return item


@app.get("/repo/main", response_model=MainOut, tags=["仓库"],
         summary="查看当前主干 main 的提交号")
def repo_main() -> MainOut:
    return MainOut(branch="main", head=git_tools.main_head())


@app.get("/repo/branches", response_model=BranchOut, tags=["仓库"],
         summary="列出中心仓库全部分支")
def repo_branches() -> BranchOut:
    return BranchOut(branches=git_tools.list_branches())


@app.post("/repo/advance-main", response_model=MainOut, tags=["仓库（演示用）"],
          summary="模拟外部推进主干（演示“测试中主干推进→作废重做”）")
def advance_main(req: AdvanceMainRequest) -> MainOut:
    new_sha = git_tools.advance_main_with_commit(req.filename, req.content_line)
    return MainOut(branch="main", head=new_sha)


@app.get("/health", response_model=MessageOut, tags=["系统"])
def health() -> MessageOut:
    return MessageOut(detail="ok")
