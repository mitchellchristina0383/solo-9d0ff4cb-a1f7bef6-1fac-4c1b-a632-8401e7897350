"""SQLite 持久化 + 进程内锁（单 worker 线程模型）。"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable

from .config import settings

# 状态枚举
PENDING = "PENDING"        # 排队中（含因主干推进被作废后等待重试）
TESTING = "TESTING"        # 正在合并/测试
MERGED = "MERGED"          # 已推进主干
CONFLICT = "CONFLICT"      # 合并冲突（针对当前基底）
FAILED = "FAILED"          # 测试失败（针对当前基底）
CANCELLED = "CANCELLED"    # 人工取消

ACTIVE_STATUSES = (PENDING, TESTING)
TERMINAL_STATUSES = (MERGED, CONFLICT, FAILED, CANCELLED)

# 单次尝试（run）的结果
RUN_OBSOLETE = "OBSOLETE"  # 测试期间主干推进，结果作废
RUN_CONFLICT = "CONFLICT"
RUN_FAILED = "FAILED"
RUN_MERGED = "MERGED"

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    os.makedirs(settings.data_dir, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    global _conn
    with _lock:
        if _conn is None:
            _conn = _connect()
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                branch      TEXT NOT NULL,
                commit_sha  TEXT NOT NULL,
                status      TEXT NOT NULL,
                attempts    INTEGER NOT NULL DEFAULT 0,
                base_sha    TEXT,
                result_sha  TEXT,
                reason      TEXT,
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id      INTEGER NOT NULL REFERENCES items(id),
                base_sha     TEXT NOT NULL,
                result_sha   TEXT,
                status       TEXT NOT NULL,
                reason       TEXT,
                test_output  TEXT,
                started_at   REAL NOT NULL,
                finished_at  REAL
            );
            CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
            CREATE INDEX IF NOT EXISTS idx_runs_item ON runs(item_id);
            """
        )
        _conn.commit()


@contextmanager
def transaction():
    assert _conn is not None
    with _lock:
        try:
            yield _conn
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise


@contextmanager
def locked():
    """暴露进程内锁：入队钉住提交 / 终态释放保留需要与读写同一把锁。"""
    with _lock:
        yield


def now() -> float:
    return time.time()


def create_item(branch: str, commit_sha: str) -> dict[str, Any]:
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO items(branch, commit_sha, status, attempts, "
            "created_at, updated_at) VALUES(?, ?, ?, 0, ?, ?)",
            (branch, commit_sha, PENDING, now(), now()),
        )
        item_id = cur.lastrowid
    return get_item(item_id)  # type: ignore[return-value]


def get_item(item_id: int) -> dict[str, Any] | None:
    with _lock:
        assert _conn is not None
        row = _conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return dict(row) if row else None


def list_items(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    with _lock:
        assert _conn is not None
        if status:
            rows = _conn.execute(
                "SELECT * FROM items WHERE status=? ORDER BY id LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = _conn.execute(
                "SELECT * FROM items ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def take_next_pending() -> dict[str, Any] | None:
    """取出最早的 PENDING 项并原子置为 TESTING（FIFO 队首）。"""
    with transaction() as conn:
        row = conn.execute(
            "SELECT * FROM items WHERE status=? ORDER BY id LIMIT 1", (PENDING,)
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE items SET status=?, updated_at=? WHERE id=?",
            (TESTING, now(), row["id"]),
        )
        item = dict(row)
        item["status"] = TESTING
        return item


def update_item(item_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now()
    assignments = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [item_id]
    with transaction() as conn:
        conn.execute(f"UPDATE items SET {assignments} WHERE id=?", values)


def start_run(item_id: int, base_sha: str) -> int:
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO runs(item_id, base_sha, status, started_at) "
            "VALUES(?, ?, ?, ?)",
            (item_id, base_sha, "RUNNING", now()),
        )
        conn.execute(
            "UPDATE items SET attempts=attempts+1, base_sha=?, updated_at=? "
            "WHERE id=?",
            (base_sha, now(), item_id),
        )
        return cur.lastrowid  # type: ignore[return-value]


def finish_run(run_id: int, status: str, reason: str | None,
               test_output: str | None, result_sha: str | None) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE runs SET status=?, reason=?, test_output=?, result_sha=?, "
            "finished_at=? WHERE id=?",
            (status, reason, test_output, result_sha, now(), run_id),
        )


def list_runs(item_id: int) -> list[dict[str, Any]]:
    with _lock:
        assert _conn is not None
        rows = _conn.execute(
            "SELECT * FROM runs WHERE item_id=? ORDER BY id", (item_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def reset_to_pending(item_id: int, reason: str) -> None:
    """作废当前尝试：回到 PENDING，等待用新主干重试。"""
    update_item(item_id, status=PENDING, reason=reason)


def count_active_with_commit(commit_sha: str) -> int:
    """仍引用某提交的活跃（非终态）队列项数量，用于保留引用计数。"""
    with _lock:
        assert _conn is not None
        row = _conn.execute(
            "SELECT COUNT(*) AS n FROM items WHERE commit_sha=? AND status IN "
            f"({','.join('?' * len(ACTIVE_STATUSES))})",
            (commit_sha, *ACTIVE_STATUSES),
        ).fetchone()
        return int(row["n"])


def list_active_commits() -> list[str]:
    """所有活跃队列项登记的去重提交号（启动时重建保留引用用）。"""
    with _lock:
        assert _conn is not None
        rows = _conn.execute(
            "SELECT DISTINCT commit_sha FROM items WHERE status IN "
            f"({','.join('?' * len(ACTIVE_STATUSES))})",
            ACTIVE_STATUSES,
        ).fetchall()
        return [r["commit_sha"] for r in rows]


def cancel_item(item_id: int) -> dict[str, Any] | None:
    item = get_item(item_id)
    if item is None:
        return None
    if item["status"] in TERMINAL_STATUSES:
        return item
    update_item(item_id, status=CANCELLED, reason="人工取消")
    return get_item(item_id)
