"""grayroll-08 红测乙（求值服务）：求值继续把未作废影子当生效。

正确口径：求值只认正式规则版本（flag_rules.official=1），并且对点名的影子
必须联查 release_shadows 的作废标记 / 开放时长；超龄 open 影子在联查事务内
一并作废，随后明确拒认（shadow_recognized=False），绝不能把影子比例当成
生效比例返回。

见红（未改求值服务前）：/api/evaluate 忽略 shadow_id，响应里没有任何
联查 / 拒认 / 来源字段，超龄影子在求值侧“查无此闸”，等于仍被默认放行。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect

OVERDUE_AGE_SECONDS = 400 * 24 * 3600
VOID_STATUSES = ("void", "voided", "expired", "expired_void", "discarded")

FID, EID = 3, 2  # feed.ranking_v2 @ staging


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import db

    db_file = tmp_path / "shadow_expire_eval_fake.db"
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DATA", tmp_path)
    with TestClient(app) as c:
        yield c


def _login(client):
    t = client.post(
        "/api/login", json={"username": "admin", "password": "admin123"}
    ).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def plant_overdue_shadow(percent=100):
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO release_shadows(
                flag_id, env_id, summary, kind, percent, cohort_id,
                status, created_at, created_by
            ) VALUES(?,?,?,?,?,NULL,'open',?,?)
            """,
            (FID, EID, "求值联查超龄影子", "percent", percent,
             time.time() - OVERDUE_AGE_SECONDS, "writer"),
        )
        sid = cur.lastrowid
        conn.commit()
    return sid


def plant_official(percent=0, version=1):
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO flag_rules(
                flag_id, env_id, kind, percent, cohort_id,
                version, official, updated_at
            ) VALUES(?,?, 'percent', ?, NULL, ?, 1, ?)
            """,
            (FID, EID, percent, version, time.time()),
        )
        rid = cur.lastrowid
        conn.commit()
    return rid


def _is_void_in_db(sid):
    with connect() as conn:
        st = conn.execute(
            "SELECT status FROM release_shadows WHERE id=?", (sid,)
        ).fetchone()["status"]
    return st in VOID_STATUSES, st


def test_eval_joint_query_rejects_overdue_shadow(client):
    """乙红本体：点名超龄影子求值，必须联查作废并明确拒认。"""
    h = _login(client)
    sid = plant_overdue_shadow(percent=100)

    r = client.post(
        "/api/evaluate",
        headers=h,
        json={
            "flag_key": "feed.ranking_v2",
            "env_code": "staging",
            "subject_key": "u-shadow-probe",
            "shadow_id": sid,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # 联查结论：不认这个影子。
    assert body.get("shadow_recognized") is False, (
        f"见红：求值仍默认认影子/无拒认闸：{body}"
    )
    assert body.get("shadow_rejected") is True
    assert str(body.get("reason", "")).find("shadow") >= 0

    # 联查事务必须把作废标记落库（作废标记与求值拒认联查）。
    voided, status = _is_void_in_db(sid)
    assert voided, f"求值联查后超龄影子应已作废，实际 status={status}"
    assert body.get("shadow_status") in VOID_STATUSES

    # 影子 100% 不得生效：该 flag/env 无正式规则，只能关闭。
    assert body["enabled"] is False


def test_eval_only_serves_official_rule_version(client):
    """求值只认正式规则版本：高比例影子对任何主体都不得泄漏。"""
    h = _login(client)
    plant_official(percent=0, version=7)
    sid = plant_overdue_shadow(percent=100)

    for i in range(40):
        r = client.post(
            "/api/evaluate",
            headers=h,
            json={
                "flag_key": "feed.ranking_v2",
                "env_code": "staging",
                "subject_key": f"u-{i}",
                "shadow_id": sid,
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("effective_source", "official") == "official"
        assert body["version"] == 7, "求值版本必须是正式规则版本"
        assert body["percent"] == 0, "求值比例必须来自正式规则，不能是影子比例"
        assert body["enabled"] is False


def test_eval_board_is_official_only_and_reports_void_joint(client):
    """求值规则板只列 official=1，并给出影子作废联查计数（增量字段）。"""
    h = _login(client)
    sid = plant_overdue_shadow()
    r = client.get("/api/evaluate/board", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    for row in body["rules"]:
        assert row.get("official", 1) == 1
    # 触发一次联查拒认后，作废计数应 >=1。
    client.post(
        "/api/evaluate",
        headers=h,
        json={
            "flag_key": "feed.ranking_v2",
            "env_code": "staging",
            "subject_key": "u-board",
            "shadow_id": sid,
        },
    )
    r2 = client.get("/api/evaluate/board", headers=h)
    assert r2.status_code == 200
    b2 = r2.json()
    if "void_shadow_count" in b2:
        assert b2["void_shadow_count"] >= 1
