"""世代对账口径回归：跨代物化不得对齐、对账锁争用、对账册偏离拒写、五处同态。

本文件先于写路径改造落地，用于钉死“甲路径：世代过滤查询”。
"""
from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

import db
from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "gen.db")
    init_db()
    yield


def _auth(c: TestClient, username: str = "admin", password: str = "123456") -> dict:
    tok = c.post(
        "/api/login", json={"username": username, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _insert_event(conn, window_id, biz_key, amount, generation):
    """夹具：按世代写入窗内事件；列未迁移时退化为旧结构。"""
    ts = "2026-11-02T10:00:00"
    created = "2026-11-02T10:00:01"
    if "generation" in _columns(conn, "events"):
        conn.execute(
            """INSERT INTO events(window_id,biz_key,event_ts,amount,disposition,generation,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (window_id, biz_key, ts, amount, "窗内", generation, created),
        )
    else:
        conn.execute(
            """INSERT INTO events(window_id,biz_key,event_ts,amount,disposition,created_at)
               VALUES (?,?,?,?,?,?)""",
            (window_id, biz_key, ts, amount, "窗内", created),
        )


def _insert_mat(conn, window_id, row_count, total, generation, partition):
    created = "2026-11-02T23:00:00"
    if "generation" in _columns(conn, "materializations"):
        conn.execute(
            """INSERT INTO materializations(window_id,status,row_count,total_amount,partition_key,generation,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (window_id, "成功", row_count, total, partition, generation, created),
        )
    else:
        conn.execute(
            """INSERT INTO materializations(window_id,status,row_count,total_amount,partition_key,created_at)
               VALUES (?,?,?,?,?,?)""",
            (window_id, "成功", row_count, total, partition, created),
        )


def _cross_generation_fixture():
    """窗口已到 g2，最新物化仍是 g1；旧口径把 g1 行与跨代合计对齐。"""
    with db.connect() as conn:
        conn.execute(
            "UPDATE windows SET status='已关闭', generation=2 WHERE id=2"
        )
        _insert_event(conn, 2, "OLD-G1", 10.0, generation=1)
        _insert_mat(conn, 2, 1, 10.0, generation=1, partition="W-1102-g1")
        conn.commit()


def test_cross_generation_materialization_must_not_align():
    """红测 1：跨世代物化行不得与窗内合计对齐。"""
    _cross_generation_fixture()
    c = TestClient(app)
    h = _auth(c)
    body = c.get("/api/reconcile/2", headers=h).json()

    assert body["aligned"] is False, "跨代物化行被误判为对齐"
    assert body["window_generation"] == 2
    assert body["window_events"] == 0
    assert body["materialized_generation"] in (None, 2)
    assert body["stale_materialization"] is not None
    assert body["stale_materialization"]["generation"] == 1
    assert body["doctrine_path"] == "甲"


def test_reconcile_lock_contention_late_arrival_only_logs():
    """红测 2：同窗对账并发，后到只记争用，不返回对账结果。"""
    c = TestClient(app)
    admin = _auth(c)
    closer = _auth(c, "closer", "close123")

    # 第一路刷新正在对账：持有该窗口的对账锁
    held = c.post(
        "/api/locks/acquire",
        headers=closer,
        json={"lock_key": "reconcile:2"},
    )
    assert held.status_code == 200

    late = c.get("/api/reconcile/2", headers=admin)
    assert late.status_code == 409, "后到对账未被对账锁挡住"
    assert "aligned" not in late.json(), "后到争用不应返回对账结果"

    contention = c.get("/api/contention", headers=admin).json()["items"]
    assert any(
        x["lock_key"] == "reconcile:2" and x["actor"] == "admin"
        for x in contention
    ), "后到争用未登记"

    # 持锁者自己可正常对账，且结束后释放锁
    ok = c.get("/api/reconcile/2", headers=closer)
    assert ok.status_code == 200
    assert c.get("/api/reconcile/2", headers=admin).status_code == 200


def test_same_account_two_sessions_contend():
    """同一账号两个页签同刷同窗，也须占对账锁互斥。"""
    c = TestClient(app)
    a1 = _auth(c)
    a2 = _auth(c)

    assert c.post(
        "/api/locks/acquire", headers=a1, json={"lock_key": "reconcile:2"}
    ).status_code == 200
    late = c.get("/api/reconcile/2", headers=a2)
    assert late.status_code == 409
    assert "aligned" not in late.json()
    cont = c.get("/api/contention", headers=a2).json()["items"]
    assert any(x["lock_key"] == "reconcile:2" for x in cont)

    # 总览也必须同锁：该窗被他人持锁时不给对齐结论，其余窗照常
    overview = c.get("/api/reconcile", headers=a2).json()["items"]
    row = next(r for r in overview if r["window_id"] == 2)
    assert row["contended"] is True and row["aligned"] is None
    other = next(r for r in overview if r["window_id"] == 1)
    assert other["contended"] is False and other["aligned"] is not None


def test_current_generation_materialization_aligns():
    """当前世代事件与当前世代物化一致时仍应对齐。"""
    with db.connect() as conn:
        conn.execute(
            "UPDATE windows SET status='已关闭', generation=2 WHERE id=2"
        )
        _insert_event(conn, 2, "NEW-G2", 5.0, generation=2)
        _insert_mat(conn, 2, 1, 5.0, generation=2, partition="W-1102-g2")
        conn.commit()

    c = TestClient(app)
    h = _auth(c)
    body = c.get("/api/reconcile/2", headers=h).json()
    assert body["window_generation"] == 2
    assert body["materialized_generation"] == 2
    assert body["window_events"] == 1
    assert body["aligned"] is True


def test_doctrine_deviation_refused_and_recorded():
    """世代对账册当天已登记乙路径时，走甲的代码必须拒写并记“对账偏了”。"""
    today = date.today().isoformat()
    with db.connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO generation_ledger
               (ledger_date,path,doctrine,created_at,actor)
               VALUES (?,?,?,?,?)""",
            (
                today,
                "乙",
                "世代快照物化复检（测试注入的偏离口径）",
                today + "T00:00:00",
                "test",
            ),
        )
        conn.commit()

    c = TestClient(app)
    h = _auth(c)
    r = c.get("/api/reconcile/2", headers=h)
    assert r.status_code == 409
    assert "对账偏了" in r.json()["detail"]

    audits = c.get("/api/audit?limit=50", headers=h).json()["items"]
    dev = [a for a in audits if a["action"] == "reconcile_deviation"]
    assert dev, "偏离世代对账册未记审计"
    payload = json.loads(dev[0]["payload"])
    assert payload["reason"] == "对账偏了"
    assert payload["ledger_path"] == "乙"
    assert payload["code_path"] == "甲"


def test_five_surfaces_share_generation():
    """对账页、窗口世代、物化台账、事件列表、审计五处世代同态。"""
    _cross_generation_fixture()
    c = TestClient(app)
    h = _auth(c)

    rec = c.get("/api/reconcile/2", headers=h).json()
    assert rec["window_generation"] == 2
    assert rec["stale_materialization"]["generation"] == 1

    all_rows = c.get("/api/reconcile", headers=h).json()
    assert all_rows["doctrine_path"] == "甲"
    row2 = next(r for r in all_rows["items"] if r["window_id"] == 2)
    assert row2["window_generation"] == rec["window_generation"]
    assert row2["aligned"] == rec["aligned"]
    assert row2["stale_generation"] == rec["stale_materialization"]["generation"]

    windows = c.get("/api/windows", headers=h).json()["items"]
    w2 = next(w for w in windows if w["id"] == 2)
    assert w2["generation"] == rec["window_generation"]

    mats = c.get("/api/materializations", headers=h).json()["items"]
    m2 = [m for m in mats if m["window_id"] == 2]
    assert m2 and all("generation" in m for m in m2)
    assert m2[0]["generation"] == rec["stale_materialization"]["generation"]
    assert m2[0]["window_generation"] == rec["window_generation"]

    events = c.get("/api/events", headers=h).json()["items"]
    old = next(e for e in events if e["biz_key"] == "OLD-G1")
    assert old["generation"] == 1

    audits = c.get("/api/audit?limit=50", headers=h).json()["items"]
    recon_audit = [a for a in audits if a["action"] == "reconcile_run"]
    assert recon_audit, "对账未写审计"
    ap = json.loads(recon_audit[0]["payload"])
    assert ap["generation"] == rec["window_generation"]
    assert ap["aligned"] is False


def test_write_paths_stamp_generation():
    """写路径：入账/关窗/物化都必须钉住当时窗口世代。"""
    c = TestClient(app)
    h = _auth(c)

    r = c.post(
        "/api/events",
        headers=h,
        json={
            "window_name": "W-1102",
            "biz_key": "G1-EVT",
            "event_ts": "2026-11-02T09:00:00",
            "amount": 7.0,
        },
    )
    assert r.status_code == 200 and r.json()["generation"] == 1

    assert c.post("/api/windows/2/close", headers=h).status_code == 200
    close_audit = [
        a for a in c.get("/api/audit?limit=20", headers=h).json()["items"]
        if a["action"] == "window_close"
    ]
    assert json.loads(close_audit[0]["payload"])["generation"] == 1

    # 重开后世代升到 2：旧世代事件与物化都不得再参与对齐
    with db.connect() as conn:
        conn.execute(
            "UPDATE windows SET status='开启', generation=2 WHERE id=2"
        )
        conn.commit()
    assert c.get("/api/reconcile/2", headers=h).json()["aligned"] is False
    assert c.post("/api/windows/2/close", headers=h).status_code == 200

    m = c.post("/api/windows/2/materialize", headers=h).json()
    assert m["generation"] == 2 and m["row_count"] == 0

    body = c.get("/api/reconcile/2", headers=h).json()
    assert body["materialized_generation"] == 2
    assert body["window_events"] == 0
    assert body["aligned"] is True
    assert body["stale_materialization"] is None

