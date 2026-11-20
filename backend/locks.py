"""窗口锁与争用登记（公共并发面，题面争用句应对得上）。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user, session_holder
from db import connect, write_audit

router = APIRouter(tags=["locks"])


class LockIn(BaseModel):
    lock_key: str
    action: str = "acquire"


def try_acquire(conn, lock_key: str, holder: str, actor: str | None = None) -> bool:
    """原子占锁：INSERT OR IGNORE 靠主键唯一性裁决并发，后到只记争用。"""
    now = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT OR IGNORE INTO window_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
        (lock_key, holder, now),
    )
    if cur.rowcount == 1:
        return True
    row = conn.execute(
        "SELECT holder FROM window_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row is None:  # 极端并发下被释放，重试视为成功
        return try_acquire(conn, lock_key, holder, actor)
    if row["holder"] == holder:
        return True
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (lock_key, actor or holder, "争用", f"持有者={row['holder']}", now),
    )
    return False


def release(conn, lock_key: str, holder: str) -> None:
    conn.execute(
        "DELETE FROM window_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )


def require_lock(conn, lock_key: str, holder: str) -> None:
    if not try_acquire(conn, lock_key, holder):
        # 争用先落库，避免外层回滚把“后到只记争用”一起抹掉。
        conn.commit()
        raise HTTPException(409, "争用：后到只记争用，不写业务")


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM window_locks ORDER BY acquired_at DESC"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/contention")
def list_contention(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_log ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/locks/acquire")
def acquire_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    holder = session_holder(user, authorization)
    with connect() as conn:
        ok = try_acquire(conn, body.lock_key, holder, actor=user)
        write_audit(
            conn,
            user,
            "lock_acquire" if ok else "lock_contention",
            body.lock_key,
            {"ok": ok, "holder": holder},
        )
        conn.commit()
        if not ok:
            raise HTTPException(409, "争用：后到只记争用")
        return {"ok": True, "lock_key": body.lock_key, "holder": holder}


@router.post("/api/locks/release")
def release_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    holder = session_holder(user, authorization)
    with connect() as conn:
        release(conn, body.lock_key, holder)
        write_audit(conn, user, "lock_release", body.lock_key, {"holder": holder})
        conn.commit()
    return {"ok": True}
