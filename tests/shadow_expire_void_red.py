"""grayroll-08 红测甲（发布服务）：超龄影子仍可被升正写入正式。

夹具：「开放时长已超阈值的影子发布」——一条 status='open' 但 created_at
远早于开放时长阈值的 release_shadows 行。

见红（未改发布服务前）：
  - POST /api/releases/shadow/{id}/promote 仍返回 200 mode=official；
  - releases / flag_rules 正式区被写入（正式零增被打破）；
  - 没有作废标记，也没有作废历史。

救绿口径（择一贯彻甲路径）：超龄检测与作废在同一事务完成，作废标记落库后
该影子不可升正，正式区 releases / flag_rules 零增。不引入“作废意图草案”，
不与乙路径混用。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect

# 足够老：无论服务端阈值取 1h / 24h / 72h / 7d 都必然超龄。
OVERDUE_AGE_SECONDS = 400 * 24 * 3600
VOID_STATUSES = ("void", "voided", "expired", "expired_void", "discarded")

FID, EID = 3, 2  # feed.ranking_v2 @ staging，全新种子下无正式规则


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import db

    db_file = tmp_path / "shadow_expire_void_red.db"
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DATA", tmp_path)
    with TestClient(app) as c:
        yield c


def _login(client, username="admin", password="admin123"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def plant_overdue_shadow(percent=66, status="open", age=OVERDUE_AGE_SECONDS):
    """夹具：直接落一条开放时长已超阈值的影子发布。"""
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO release_shadows(
                flag_id, env_id, summary, kind, percent, cohort_id,
                status, created_at, created_by
            ) VALUES(?,?,?,?,?,NULL,?,?,?)
            """,
            (FID, EID, "超龄未升正影子夹具", "percent", percent,
             status, time.time() - age, "writer"),
        )
        sid = cur.lastrowid
        conn.commit()
    return sid


def official_counts():
    with connect() as conn:
        releases = conn.execute(
            "SELECT COUNT(*) c FROM releases WHERE flag_id=? AND env_id=?", (FID, EID)
        ).fetchone()["c"]
        rules = conn.execute(
            "SELECT COUNT(*) c FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1",
            (FID, EID),
        ).fetchone()["c"]
        max_ver = conn.execute(
            "SELECT COALESCE(MAX(version),0) v FROM flag_rules WHERE flag_id=? AND env_id=?",
            (FID, EID),
        ).fetchone()["v"]
    return releases, rules, max_ver


def shadow_row(sid):
    with connect() as conn:
        return conn.execute("SELECT * FROM release_shadows WHERE id=?", (sid,)).fetchone()


def test_fixture_overdue_shadow_reproduces(client):
    """夹具先跑通：确实是 open 且开放时长远超阈值。"""
    sid = plant_overdue_shadow()
    row = shadow_row(sid)
    assert row["status"] == "open"
    assert time.time() - row["created_at"] >= OVERDUE_AGE_SECONDS - 5


def test_overdue_shadow_still_promotable_is_the_red(client):
    """红测甲本体：现行代码会把超龄影子升正并写正式 —— 这必须被改掉。"""
    h = _login(client)
    sid = plant_overdue_shadow(percent=66)
    before = official_counts()

    r = client.post(f"/api/releases/shadow/{sid}/promote", headers=h, json={})

    # 正确口径：超龄影子必须在同事务被作废并拒绝升正（400 状态非法 / 409 冲突均可）。
    assert r.status_code >= 400, (
        f"见红：超龄影子竟仍可升正成功 {r.status_code} {r.text}"
    )
    assert r.status_code in (400, 409), r.text

    row = shadow_row(sid)
    assert row["status"] in VOID_STATUSES, f"超龄影子未被作废，status={row['status']}"

    after = official_counts()
    assert after == before, f"作废/拒绝升正不得写正式区：before={before} after={after}"

    with connect() as conn:
        void_hist = conn.execute(
            "SELECT COUNT(*) c FROM change_history "
            "WHERE flag_id=? AND env_id=? AND action LIKE '%void%'",
            (FID, EID),
        ).fetchone()["c"]
    assert void_hist >= 1, "同事务作废必须留变更历史（action 含 void）"


def test_explicit_void_endpoint_marks_shadow_void(client):
    """显式作废入口：POST /api/releases/shadow/{id}/void。"""
    h = _login(client, "writer", "writer123")
    sid = plant_overdue_shadow()

    r = client.post(f"/api/releases/shadow/{sid}/void", headers=h, json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("voided") is True or body.get("status") in VOID_STATUSES

    row = shadow_row(sid)
    assert row["status"] in VOID_STATUSES


def test_expire_overdue_sweep_voids_fixture(client):
    """批量超龄检测+作废入口（甲路径集合版，仍是检测与作废同事务）。"""
    h = _login(client)
    sid = plant_overdue_shadow(percent=71)

    r = client.post("/api/releases/expire-overdue", headers=h, json={})
    assert r.status_code == 200, r.text
    body = r.json()
    voided = body.get("voided") or body.get("ids") or []
    assert sid in voided

    row = shadow_row(sid)
    assert row["status"] in VOID_STATUSES
