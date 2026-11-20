"""窗内事件与物化结果互证（钉死世代口径）。

世代对账册口径（当天固定，禁止改口）：**甲路径**
  - 对账只统计当前 generation 的窗内事件；
  - 只认当前 generation 的物化行，跨代行整笔排除；
  - 乙路径 reconcile_snap 快照复检不实现，两条路径不得混用。
偏离对账册口径时拒写并记「对账偏了」。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, session_holder
from db import RECONCILE_DOCTRINE, RECONCILE_PATH, connect, write_audit
from locks import release, try_acquire

router = APIRouter(tags=["reconcile"])


def _today() -> str:
    return datetime.now().date().isoformat()


def _ledger_path(conn) -> str | None:
    row = conn.execute(
        "SELECT path FROM generation_ledger WHERE ledger_date=?", (_today(),)
    ).fetchone()
    return row["path"] if row else None


def _guard_doctrine(conn, actor: str, wid: int | None) -> None:
    """世代对账册已改口则拒写；偏离记「对账偏了」。"""
    ledger_path = _ledger_path(conn)
    if ledger_path is None:
        conn.execute(
            """INSERT INTO generation_ledger(ledger_date,path,doctrine,actor,created_at)
               VALUES (?,?,?,?,?)""",
            (
                _today(),
                RECONCILE_PATH,
                RECONCILE_DOCTRINE,
                "system",
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        return
    if ledger_path != RECONCILE_PATH:
        write_audit(
            conn,
            actor,
            "reconcile_deviation",
            str(wid) if wid is not None else None,
            {
                "reason": "对账偏了",
                "ledger_path": ledger_path,
                "code_path": RECONCILE_PATH,
                "ledger_date": _today(),
            },
        )
        conn.commit()
        # 偏离即不出数：拒绝对账结果，避免页面出现非对账册口径的对齐。
        raise HTTPException(409, "对账偏了：世代对账册口径与代码路径不一致，拒绝出数")


def _current_generation_event_sum(conn, wid: int, generation: int):
    return conn.execute(
        """SELECT COUNT(*) c, COALESCE(SUM(amount),0) s
           FROM events
           WHERE window_id=? AND disposition='窗内' AND generation=?""",
        (wid, generation),
    ).fetchone()


def _current_generation_materialization(conn, wid: int, generation: int):
    return conn.execute(
        """SELECT * FROM materializations
           WHERE window_id=? AND generation=?
           ORDER BY id DESC LIMIT 1""",
        (wid, generation),
    ).fetchone()


def _stale_materialization(conn, wid: int, generation: int):
    """跨代物化最新行：只用于展示与告警，绝不参与对齐。"""
    return conn.execute(
        """SELECT * FROM materializations
           WHERE window_id=? AND (generation IS NULL OR generation<>?)
           ORDER BY id DESC LIMIT 1""",
        (wid, generation),
    ).fetchone()


def compute(conn, w) -> dict:
    """按甲路径计算某窗口的对账结果（纯读，不写审计/锁）。"""
    generation = int(w["generation"])
    ev = _current_generation_event_sum(conn, w["id"], generation)
    mat = _current_generation_materialization(conn, w["id"], generation)
    stale = _stale_materialization(conn, w["id"], generation)
    late = conn.execute(
        "SELECT COUNT(*) c FROM late_bucket WHERE window_name=?", (w["name"],)
    ).fetchone()["c"]
    rejects = conn.execute(
        "SELECT COUNT(*) c FROM reject_slips WHERE window_name=?", (w["name"],)
    ).fetchone()["c"]

    aligned = bool(
        mat
        and mat["status"] == "成功"
        and mat["generation"] == generation
        and mat["row_count"] == ev["c"]
        and abs(mat["total_amount"] - ev["s"]) < 1e-6
    )
    return {
        "window_id": w["id"],
        "name": w["name"],
        "status": w["status"],
        "window_generation": generation,
        "window_events": ev["c"],
        "window_amount": ev["s"],
        "materialized": dict(mat) if mat else None,
        "materialized_generation": mat["generation"] if mat else None,
        "stale_materialization": dict(stale) if stale else None,
        "late_bucket": late,
        "rejects": rejects,
        "aligned": aligned,
        "doctrine_path": RECONCILE_PATH,
        "doctrine": RECONCILE_DOCTRINE,
    }


@router.get("/api/reconcile/{wid}")
def reconcile(wid: int, authorization: AuthHeader = None):
    user = require_user(authorization)
    holder = session_holder(user, authorization)
    lock_key = f"reconcile:{wid}"
    with connect() as conn:
        acquired = try_acquire(conn, lock_key, holder, actor=user)
        # 争用/持锁事实先落库：后到只记争用，不能因异常回滚掉。
        conn.commit()
        if not acquired:
            raise HTTPException(409, "争用：后到只记争用，不写业务")
        try:
            w = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
            if not w:
                raise HTTPException(404, "窗口不存在")
            _guard_doctrine(conn, user, wid)
            body = compute(conn, w)
            write_audit(
                conn,
                user,
                "reconcile_run",
                str(wid),
                {
                    "generation": body["window_generation"],
                    "aligned": body["aligned"],
                    "window_events": body["window_events"],
                    "window_amount": body["window_amount"],
                    "materialized_generation": body["materialized_generation"],
                    "stale_generation": (
                        body["stale_materialization"]["generation"]
                        if body["stale_materialization"]
                        else None
                    ),
                    "doctrine_path": RECONCILE_PATH,
                    "lock_key": lock_key,
                },
            )
            conn.commit()
            body["window"] = dict(w)
            return body
        finally:
            release(conn, lock_key, holder)
            conn.commit()


@router.get("/api/reconcile")
def reconcile_all(authorization: AuthHeader = None):
    """总览：逐窗占对账锁按同口径计算；争用的窗口不给对齐结论。"""
    user = require_user(authorization)
    holder = session_holder(user, authorization)
    with connect() as conn:
        _guard_doctrine(conn, user, None)
        windows = conn.execute("SELECT * FROM windows ORDER BY id").fetchall()
        items = []
        for w in windows:
            lock_key = f"reconcile:{w['id']}"
            acquired = try_acquire(conn, lock_key, holder, actor=user)
            conn.commit()
            if not acquired:
                items.append(
                    {
                        "window_id": w["id"],
                        "name": w["name"],
                        "status": w["status"],
                        "generation": int(w["generation"]),
                        "window_generation": int(w["generation"]),
                        "aligned": None,
                        "contended": True,
                        "window_events": None,
                        "window_amount": None,
                        "materialized_generation": None,
                        "stale_generation": None,
                        "late_bucket": None,
                        "doctrine_path": RECONCILE_PATH,
                    }
                )
                continue
            try:
                r = compute(conn, w)
            finally:
                release(conn, lock_key, holder)
            items.append(
                {
                    "window_id": r["window_id"],
                    "name": r["name"],
                    "status": r["status"],
                    "generation": r["window_generation"],
                    "window_generation": r["window_generation"],
                    "aligned": r["aligned"],
                    "contended": False,
                    "window_events": r["window_events"],
                    "window_amount": r["window_amount"],
                    "materialized_generation": r["materialized_generation"],
                    "stale_generation": (
                        r["stale_materialization"]["generation"]
                        if r["stale_materialization"]
                        else None
                    ),
                    "late_bucket": r["late_bucket"],
                    "doctrine_path": RECONCILE_PATH,
                }
            )
        conn.commit()
        return {
            "items": items,
            "doctrine_path": RECONCILE_PATH,
            "doctrine": RECONCILE_DOCTRINE,
        }
