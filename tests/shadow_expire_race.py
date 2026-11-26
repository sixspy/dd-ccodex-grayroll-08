"""grayroll-08 红测争用：两路「影子发布生命周期互斥」无锁双成。

场景 duel_shadow_expire_void：双会话（admin / writer 两个登录态）各持一个
并发请求，抢同一条 release_shadow 的生命周期唯一裁决。

见红（未加互斥 / 原子状态裁决前）：
  - 两个 /promote 都返回 200（无锁双成）；
  - releases 正式区在一轮里 +2，版本被顶两次 —— 败方也写了正式；
  - contention_marks 没有任何争用痕。

救绿口径：同一影子生命周期至多一个胜方；败方 409、零正式写入，且
contention_marks 必留 loser/winner 争用痕。多轮并发压测保证不是时序偶然。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect

FID, EID = 3, 2
ROUNDS = 12


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import db

    db_file = tmp_path / "shadow_expire_race.db"
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DATA", tmp_path)
    with TestClient(app) as c:
        yield c


def _token(client, username, password):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def plant_fresh_shadow(tag, age_seconds=30):
    """非超龄 open 影子：保证胜方确实能升正，用于纯互斥验证。"""
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO release_shadows(
                flag_id, env_id, summary, kind, percent, cohort_id,
                status, created_at, created_by
            ) VALUES(?,?,?,?,?,NULL,'open',?,?)
            """,
            (FID, EID, f"互斥夹具-{tag}", "percent", 33,
             time.time() - age_seconds, "writer"),
        )
        sid = cur.lastrowid
        conn.commit()
    return sid


def duel_shadow_expire_void(client, headers_a, headers_b, shadow_id,
                            action_a="promote", action_b="promote"):
    """并发双请求：barrier 对齐后同时打出，抢影子生命周期互斥。

    action 取 "promote" 或 "void"。返回 (resp_a, resp_b)。
    """
    barrier = threading.Barrier(2)

    def call(headers, action):
        path = (
            f"/api/releases/shadow/{shadow_id}/promote"
            if action == "promote"
            else f"/api/releases/shadow/{shadow_id}/void"
        )
        barrier.wait()
        return client.post(path, headers=headers, json={})

    with ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(call, headers_a, action_a)
        fb = pool.submit(call, headers_b, action_b)
        return fa.result(), fb.result()


def _official_release_count():
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM releases WHERE flag_id=? AND env_id=? AND official=1",
            (FID, EID),
        ).fetchone()["c"]


def _contention_count():
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM contention_marks WHERE lock_key=?",
            (f"flag-release:{FID}:{EID}",),
        ).fetchone()["c"]


def test_duel_shadow_expire_void(client):
    """双会话双升正：绝不能无锁双成；每轮正式区至多 +1，败方零正式改。"""
    ha = _token(client, "admin", "admin123")
    hb = _token(client, "writer", "writer123")

    both_ok_rounds = 0
    for i in range(ROUNDS):
        sid = plant_fresh_shadow(f"r{i}")
        before = _official_release_count()
        ra, rb = duel_shadow_expire_void(client, ha, hb, sid)
        after = _official_release_count()

        codes = sorted([ra.status_code, rb.status_code])
        if ra.status_code == 200 and rb.status_code == 200:
            both_ok_rounds += 1
        # 每一轮：正式 releases 至多增加 1（败方零正式改）。
        assert after - before <= 1, (
            f"第 {i} 轮出现双正式写入 codes={codes} delta={after - before}"
        )
        # 每一轮至多一个胜方 200。
        assert codes.count(200) <= 1, f"第 {i} 轮无锁双成 codes={codes}"

    assert both_ok_rounds == 0, f"见红：{both_ok_rounds}/{ROUNDS} 轮两路无锁双成"
    # 救绿后每轮恰有一次正式写入，累计 ROUNDS。
    assert _official_release_count() == ROUNDS
    # 争用痕必有（锁层 409 或生命周期状态裁决败方留痕）。
    assert _contention_count() >= 1, "双会话争用必须写 contention_marks"


def test_forced_lock_contention_leaves_mark_and_zero_official(client):
    """确定性争用痕：test 侧持住行锁，writer 升正必须 409 + loser/winner 痕。"""
    ha = _token(client, "admin", "admin123")
    hb = _token(client, "writer", "writer123")
    sid = plant_fresh_shadow("forced-lock")

    # 模拟 admin 会话已原子持锁并提交可见。
    from locks import acquire

    holder_conn = connect()
    acquire(holder_conn, f"flag-release:{FID}:{EID}", "admin")
    holder_conn.commit()
    try:
        before = _official_release_count()
        r = client.post(f"/api/releases/shadow/{sid}/promote", headers=hb, json={})
        assert r.status_code == 409, r.text
        after = _official_release_count()
        assert after == before, "锁争用败方不得写正式"
    finally:
        from locks import release

        release(holder_conn, f"flag-release:{FID}:{EID}", "admin")
        holder_conn.commit()
        holder_conn.close()

    with connect() as conn:
        mark = conn.execute(
            "SELECT * FROM contention_marks WHERE lock_key=? ORDER BY id DESC LIMIT 1",
            (f"flag-release:{FID}:{EID}",),
        ).fetchone()
    assert mark is not None, "争用痕必有"
    assert mark["loser"] == "writer"
    assert mark["winner"] == "admin"


def test_promote_vs_void_duel_ends_in_one_terminal_state(client):
    """升正 vs 作废同抢：结果必须自洽，不允许既 promoted 又留 open/双终态。"""
    ha = _token(client, "admin", "admin123")
    hb = _token(client, "writer", "writer123")
    sid = plant_fresh_shadow("promote-vs-void")

    ra, rb = duel_shadow_expire_void(
        client, ha, hb, sid, action_a="promote", action_b="void"
    )
    codes = sorted([ra.status_code, rb.status_code])
    # 不能是 404（路由缺失造成的假绿），必须是真实的一胜一争用败。
    assert 404 not in codes, f"作废路由必须存在：{ra.text}|{rb.text}"
    assert codes == [200, 409], f"升正/作废必须恰为一胜一409：codes={codes} {ra.text}|{rb.text}"
    assert _contention_count() >= 1, "升正/作废决斗必须留争用痕"

    with connect() as conn:
        st = conn.execute(
            "SELECT status FROM release_shadows WHERE id=?", (sid,)
        ).fetchone()["status"]
        nrel = conn.execute(
            "SELECT COUNT(*) c FROM releases WHERE flag_id=? AND env_id=? AND official=1",
            (FID, EID),
        ).fetchone()["c"]

    if st == "promoted":
        assert nrel == 1, "升正胜则恰好一次正式写入"
    else:
        assert st in ("void", "voided", "expired", "discarded")
        assert nrel == 0, "作废胜则正式区零增"


def test_same_username_two_sessions_are_mutex(client):
    """同名 writer 的两次独立登录（不同 token=不同 owner）也必须一胜一409。"""
    h1 = _token(client, "writer", "writer123")
    h2 = _token(client, "writer", "writer123")
    ok = 0
    for i in range(6):
        sid = plant_fresh_shadow(f"same-user-{i}")
        ra, rb = duel_shadow_expire_void(client, h1, h2, sid)
        codes = sorted([ra.status_code, rb.status_code])
        assert codes == [200, 409], f"同名双会话也必须互斥 codes={codes} {ra.text}|{rb.text}"
        ok += 1
    assert _official_release_count() == 6


def test_same_token_double_request_cas_backstop(client):
    """同一登录态并发双请求（前端决斗按钮场景）：锁可重入，但 CAS 兜底只许一次正式写。"""
    h = _token(client, "admin", "admin123")
    for i in range(6):
        sid = plant_fresh_shadow(f"same-token-{i}")
        ra, rb = duel_shadow_expire_void(client, h, h, sid)
        codes = sorted([ra.status_code, rb.status_code])
        assert codes[0] == 200 and codes[1] in (409, 400), f"同 token 双请求 codes={codes} {ra.text}|{rb.text}"
        assert codes.count(200) == 1
    assert _official_release_count() == 6
