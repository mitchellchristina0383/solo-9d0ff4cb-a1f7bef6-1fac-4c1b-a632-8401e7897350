"""请求/响应模型与中文 OpenAPI 描述。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from . import db


class EnqueueRequest(BaseModel):
    branch: str = Field(..., description="要并入 main 的分支名（需已存在于中心仓库）",
                        examples=["feature-good"])


class EnqueueResponse(BaseModel):
    id: int
    branch: str
    commit_sha: str
    status: str
    detail: str = "入队成功：已按当前分支头提交号登记，将按入队顺序处理"


class ItemOut(BaseModel):
    id: int
    branch: str
    commit_sha: str
    status: Literal["PENDING", "TESTING", "MERGED", "CONFLICT", "FAILED",
                    "CANCELLED"]
    attempts: int
    base_sha: str | None
    result_sha: str | None
    reason: str | None
    created_at: float
    updated_at: float


class RunOut(BaseModel):
    id: int
    item_id: int
    base_sha: str
    result_sha: str | None
    status: Literal["RUNNING", "OBSOLETE", "CONFLICT", "FAILED", "MERGED"]
    reason: str | None
    test_output: str | None
    started_at: float
    finished_at: float | None


class ItemDetail(BaseModel):
    item: ItemOut
    runs: list[RunOut]


class MainOut(BaseModel):
    branch: str
    head: str


class AdvanceMainRequest(BaseModel):
    filename: str = Field("mainlog.md", description="在该文件追加一行并提交到 main")
    content_line: str | None = Field(
        None, description="追加内容；留空自动生成（用于模拟测试期间主干推进）")


class BranchOut(BaseModel):
    branches: list[str]


class UpdateBranchResponse(BaseModel):
    branch: str
    old_sha: str
    new_sha: str
    detail: str = "替换成功：分支已指向新提交；之后入队的队列项将登记新提交，"
    "此前已受理的项仍按原登记提交处理（对象由队列保留，重启+gc 后仍可处理）"


class MessageOut(BaseModel):
    detail: str


STATUS_DESCRIPTIONS = {
    db.PENDING: "排队中（若曾因主干推进被作废，会在队首用新主干重做）",
    db.TESTING: "正在合并并运行样例测试",
    db.MERGED: "测试通过且基底未变，主干已推进到受测提交",
    db.CONFLICT: "合并冲突（针对当前基底），已记录原因，队列继续处理下一项",
    db.FAILED: "样例测试失败（针对当前基底），已记录原因，队列继续处理下一项",
    db.CANCELLED: "已人工取消",
}
