"""灰度规则只读与影子绑定（乙路径草案）。

grayroll-08：规则影子与发布影子共用同一套超龄作废口径与生命周期互斥，
求值最终只认 official=1 的正式规则版本。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from audit_svc import write_audit, write_history
from auth import current_user, require_writer, session_owner_token
from db import connect
from locks import acquire, release, write_contention
from policies import (
    SHADOW_STATUS_VOID,
    assert_flag_publishable,
    assert_not_killed_or_allow_kill,
    classify_shadow,
    describe_path,
    shadow_is_overdue,
    shadow_is_void_status,
)
from validators import require_int, require_percent, require_rule_kind

router = APIRouter(prefix="/api/rules", tags=["rules"])


class ShadowBindIn(BaseModel):
    flag_id: int
    env_id: int
    kind: str
    percent: int = 0
    cohort_id: int | None = None


def _key(fid: int, eid: int) -> str:
    return f"flag-rule:{fid}:{eid}"


def _annotate(row) -> dict:
    d = dict(row)
    info = classify_shadow(d.get("status", ""), d.get("created_at", 0.0))
    d.update(info)
    d["voided"] = bool(info["voided"])
    return d


def _sweep_overdue_rule_shadows(conn, actor: str = "system") -> list[int]:
    now = time.time()
    from policies import SHADOW_OPEN_TTL_SECONDS

    rows = conn.execute(
        "SELECT * FROM rule_shadows WHERE status='open' AND created_at < ?",
        (now - SHADOW_OPEN_TTL_SECONDS,),
    ).fetchall()
    ids = []
    for sh in rows:
        cur = conn.execute(
            "UPDATE rule_shadows SET status=?, voided_at=?, void_reason=?, voided_by=? "
            "WHERE id=? AND status='open'",
            (SHADOW_STATUS_VOID, now, "overdue_open_ttl", actor, sh["id"]),
        )
        if cur.rowcount == 1:
            ids.append(sh["id"])
            write_history(
                conn, actor, "rule_shadow_void",
                f"超龄规则影子 #{sh['id']} 同事务作废",
                flag_id=sh["flag_id"], env_id=sh["env_id"],
            )
    return ids


@router.get("")
def list_rules(authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        _sweep_overdue_rule_shadows(conn)
        conn.commit()
        official = [
            dict(r)
            for r in conn.execute(
                """
                SELECT r.*, f.key AS flag_key, e.code AS env_code
                FROM flag_rules r
                JOIN flags f ON f.id=r.flag_id
                JOIN environments e ON e.id=r.env_id
                WHERE r.official=1
                ORDER BY r.id
                """
            ).fetchall()
        ]
        shadows = [_annotate(r) for r in conn.execute("SELECT * FROM rule_shadows ORDER BY id DESC").fetchall()]
    return {"official": official, "shadows": shadows}


@router.post("/shadow-bind")
def shadow_bind(body: ShadowBindIn, authorization: str | None = Header(default=None)):
    """乙路径：只写影子人群/比例绑定，正式规则零改。"""
    user = current_user(authorization)
    require_writer(user)
    fid = require_int(body.flag_id, "开关", 1)
    eid = require_int(body.env_id, "环境", 1)
    kind = require_rule_kind(body.kind)
    pct = require_percent(body.percent)
    key = _key(fid, eid)
    owner = session_owner_token(authorization)
    with connect() as conn:
        acquire(conn, key, user["username"], owner)
        try:
            fl = conn.execute("SELECT * FROM flags WHERE id=?", (fid,)).fetchone()
            if not fl:
                raise HTTPException(404, detail="开关不存在")
            assert_flag_publishable(fl["status"])
            assert_not_killed_or_allow_kill(fl["status"])
            if not conn.execute("SELECT 1 FROM environments WHERE id=?", (eid,)).fetchone():
                raise HTTPException(404, detail="环境不存在")
            if body.cohort_id and not conn.execute(
                "SELECT 1 FROM cohorts WHERE id=?", (body.cohort_id,)
            ).fetchone():
                raise HTTPException(404, detail="人群不存在")
            now = time.time()
            cur = conn.execute(
                "INSERT INTO rule_shadows(flag_id, env_id, kind, percent, cohort_id, status, created_at, created_by) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (fid, eid, kind, pct, body.cohort_id, "open", now, user["username"]),
            )
            sid = cur.lastrowid
            write_audit(conn, user["username"], "rule_shadow", "rule_shadow", sid, describe_path("cohort_bind", True))
            write_history(conn, user["username"], "rule_shadow", f"影子绑定 flag={fid} env={eid}", flag_id=fid, env_id=eid)
            conn.commit()
            return {"mode": "shadow", "id": sid, "path": describe_path("cohort_bind", True)}
        finally:
            release(conn, key, user["username"], owner)
            conn.commit()


@router.post("/shadow/{shadow_id}/void")
def void_rule_shadow(shadow_id: int, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    require_writer(user)
    owner = session_owner_token(authorization)
    with connect() as conn:
        sh = conn.execute("SELECT * FROM rule_shadows WHERE id=?", (shadow_id,)).fetchone()
        if not sh:
            raise HTTPException(404, detail="规则影子不存在")
        key = _key(sh["flag_id"], sh["env_id"])
        acquire(conn, key, user["username"], owner)
        try:
            sh = conn.execute("SELECT * FROM rule_shadows WHERE id=?", (shadow_id,)).fetchone()
            if shadow_is_void_status(sh["status"]):
                return {"id": shadow_id, "voided": True, "already_void": True, "status": sh["status"]}
            if sh["status"] != "open":
                write_contention(conn, key, user["username"], "winner")
                raise HTTPException(409, detail="rule_shadow_not_open")
            now = time.time()
            conn.execute(
                "UPDATE rule_shadows SET status=?, voided_at=?, void_reason=?, voided_by=? WHERE id=?",
                (SHADOW_STATUS_VOID, now, "manual_void", user["username"], shadow_id),
            )
            write_history(conn, user["username"], "rule_shadow_void", f"规则影子 #{shadow_id} 作废",
                          flag_id=sh["flag_id"], env_id=sh["env_id"])
            conn.commit()
            return {"id": shadow_id, "voided": True, "status": SHADOW_STATUS_VOID}
        finally:
            release(conn, key, user["username"], owner)
            conn.commit()


@router.post("/shadow/{shadow_id}/promote")
def promote_rule_shadow(shadow_id: int, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    require_writer(user)
    owner = session_owner_token(authorization)
    with connect() as conn:
        sh = conn.execute("SELECT * FROM rule_shadows WHERE id=?", (shadow_id,)).fetchone()
        if not sh:
            raise HTTPException(404, detail="影子绑定不存在")
        fid, eid = sh["flag_id"], sh["env_id"]
        key = _key(fid, eid)
        acquire(conn, key, user["username"], owner)
        try:
            sh = conn.execute("SELECT * FROM rule_shadows WHERE id=?", (shadow_id,)).fetchone()
            if shadow_is_void_status(sh["status"]):
                write_contention(conn, key, user["username"], sh["voided_by"] or "void")
                raise HTTPException(409, detail="rule_shadow_already_void")
            if sh["status"] != "open":
                write_contention(conn, key, user["username"], "winner")
                raise HTTPException(409, detail="影子绑定已被另一会话升正")
            if shadow_is_overdue(sh["created_at"]):
                now = time.time()
                conn.execute(
                    "UPDATE rule_shadows SET status=?, voided_at=?, void_reason=?, voided_by=? WHERE id=? AND status='open'",
                    (SHADOW_STATUS_VOID, now, "overdue_promote_attempt", user["username"], shadow_id),
                )
                write_history(conn, user["username"], "rule_shadow_void",
                              f"超龄规则影子 #{shadow_id} 同事务作废，拒绝升正", flag_id=fid, env_id=eid)
                raise HTTPException(400, detail="rule_shadow_expired_void: 超龄规则影子已作废，不可升正")
            now = time.time()
            old = conn.execute(
                "SELECT * FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1", (fid, eid)
            ).fetchone()
            ver = (old["version"] + 1) if old else 1
            if old:
                conn.execute("DELETE FROM flag_rules WHERE id=?", (old["id"],))
            cur = conn.execute(
                "INSERT INTO flag_rules(flag_id, env_id, kind, percent, cohort_id, version, official, updated_at) "
                "VALUES(?,?,?,?,?,?,1,?)",
                (fid, eid, sh["kind"], sh["percent"], sh["cohort_id"], ver, now),
            )
            rid = cur.lastrowid
            cas = conn.execute(
                "UPDATE rule_shadows SET status='promoted' WHERE id=? AND status='open'", (shadow_id,)
            )
            if cas.rowcount != 1:
                conn.rollback()
                with connect() as mc:
                    write_contention(mc, key, user["username"], "winner")
                    mc.commit()
                raise HTTPException(409, detail="rule_promote_cas_lost")
            if conn.execute("SELECT status FROM flags WHERE id=?", (fid,)).fetchone()["status"] == "draft":
                conn.execute("UPDATE flags SET status='active' WHERE id=?", (fid,))
            write_history(conn, user["username"], "rule_promote", f"影子规则升正 v{ver}", flag_id=fid, env_id=eid)
            write_audit(conn, user["username"], "rule_promote", "flag_rule", rid, f"from_shadow={shadow_id}")
            conn.commit()
            return {"id": rid, "version": ver, "mode": "official"}
        finally:
            release(conn, key, user["username"], owner)
            conn.commit()
