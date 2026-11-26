"""行锁与争用痕（嵌进发布/升正主写路径）。

grayroll-08：锁获取改成“原子插入占位 + 立即提交可见”，避免旧实现
SELECT-then-UPSERT 在并发下两边都判定无锁（无锁双成）。owner_token 用来
区分“同名用户的不同会话”；不传时退化为按 holder 重入，兼容旧调用。
"""
from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, Header, HTTPException

from auth import current_user
from db import connect

router = APIRouter(prefix="/api/locks", tags=["locks"])
TTL = 20.0


def _same_owner(row, holder: str, owner_token: str) -> bool:
    try:
        row_token = row["owner_token"]
    except (IndexError, KeyError):
        row_token = ""
    return row["holder"] == holder and (row_token or "") == (owner_token or "")


def write_contention(conn, key: str, loser: str, winner: str) -> None:
    """写一条争用痕（调用方决定何时提交；独立成败时可自行 commit）。"""
    conn.execute(
        "INSERT INTO contention_marks(lock_key, loser, winner, created_at) VALUES(?,?,?,?)",
        (key, loser, winner or "winner", time.time()),
    )


def acquire(
    conn,
    key: str,
    holder: str,
    owner_token: str | None = None,
    ttl: float = TTL,
) -> None:
    """获取行锁；被其他有效会话持有时写争用痕并抛 409。

    成功路径会立即 commit，让锁行对并发连接可见（甲路径同事务业务写入不受影响：
    锁占位先行落库，业务写与最终释放仍在调用方事务里提交）。
    """
    token = owner_token or ""
    now = time.time()
    try:
        conn.execute(
            "INSERT INTO row_locks(lock_key, holder, owner_token, expires_at) VALUES(?,?,?,?)",
            (key, holder, token, now + ttl),
        )
        conn.commit()
        return
    except sqlite3.IntegrityError:
        pass

    row = conn.execute("SELECT * FROM row_locks WHERE lock_key=?", (key,)).fetchone()
    if row and row["expires_at"] > now and not _same_owner(row, holder, token):
        write_contention(conn, key, holder, row["holder"])
        conn.commit()
        raise HTTPException(409, detail="发布锁争用，败方零写入，请刷新后重试")

    # 同一持有者重入，或锁已过期：乐观接管。
    prev_expires = row["expires_at"] if row else 0.0
    cur = conn.execute(
        "UPDATE row_locks SET holder=?, owner_token=?, expires_at=? "
        "WHERE lock_key=? AND expires_at=?",
        (holder, token, now + ttl, key, prev_expires),
    )
    if cur.rowcount == 0:
        fresh = conn.execute("SELECT * FROM row_locks WHERE lock_key=?", (key,)).fetchone()
        winner = fresh["holder"] if fresh else "winner"
        write_contention(conn, key, holder, winner)
        conn.commit()
        raise HTTPException(409, detail="发布锁争用，败方零写入，请稍后重试")
    conn.commit()


def release(conn, key: str, holder: str, owner_token: str | None = None) -> None:
    """释放锁。传了 owner_token 时只删本会话的锁，避免误删胜方的锁。"""
    token = owner_token or ""
    if owner_token is None:
        conn.execute("DELETE FROM row_locks WHERE lock_key=? AND holder=?", (key, holder))
    else:
        conn.execute(
            "DELETE FROM row_locks WHERE lock_key=? AND holder=? AND owner_token=?",
            (key, holder, token),
        )


@router.get("/contention")
def list_contention(authorization: str | None = Header(default=None), limit: int = 40):
    current_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_marks ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return {"items": [dict(r) for r in rows]}
