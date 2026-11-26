"""在线求值：给定主体键，返回开关在某环境是否命中。

grayroll-08：求值只认正式规则版本（flag_rules.official=1）。对请求点名的影子
（shadow_id / release_shadow_id），必须联查 release_shadows 的状态作废标记与
开放时长；超龄 open 影子在同一连接事务内原子作废，随后明确拒认
（shadow_recognized=False, shadow_rejected=True），影子比例永远不参与求值。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from audit_svc import write_audit, write_history
from auth import current_user
from db import connect
from policies import (
    SHADOW_STATUS_VOID,
    shadow_is_overdue,
    shadow_is_void_status,
)
from policies import combine_rule, eval_percent
from validators import require_str

router = APIRouter(prefix="/api/evaluate", tags=["evaluate"])


class EvalIn(BaseModel):
    flag_key: str
    env_code: str
    subject_key: str
    # 兼容两种点名影子的字段命名。
    shadow_id: int | None = None
    release_shadow_id: int | None = None
    shadow: int | None = None


def _shadow_joint_gate(conn, shadow_id: int | None) -> dict:
    """联查作废标记 + 超龄口径；超龄 open 影子同事务作废。

    返回挂到求值响应上的 shadow_* 字段。任何影子都不会被当作生效来源
    （shadow_recognized 恒为 False）。
    """
    if shadow_id is None:
        return {}
    ts = time.time()
    sh = conn.execute(
        "SELECT * FROM release_shadows WHERE id=?", (shadow_id,)
    ).fetchone()
    if not sh:
        return {
            "shadow_id": shadow_id,
            "shadow_found": False,
            "shadow_recognized": False,
            "shadow_rejected": True,
            "shadow_status": "missing",
            "shadow_reason": "shadow_not_found",
        }

    status = sh["status"]
    overdue = status == "open" and shadow_is_overdue(sh["created_at"], now=ts)
    if overdue:
        cur = conn.execute(
            "UPDATE release_shadows SET status=?, voided_at=?, void_reason=?, voided_by=? "
            "WHERE id=? AND status='open'",
            (SHADOW_STATUS_VOID, ts, "overdue_eval_joint", "evaluate", shadow_id),
        )
        if cur.rowcount == 1:
            write_history(
                conn,
                "evaluate",
                "release_shadow_void",
                f"求值联查发现超龄影子发布 #{shadow_id}，同事务作废并拒认",
                flag_id=sh["flag_id"],
                env_id=sh["env_id"],
            )
            write_audit(
                conn, "evaluate", "release_shadow_void", "release_shadow",
                shadow_id, "eval_joint_query shadow_expired_void",
            )
            conn.commit()
        status = SHADOW_STATUS_VOID

    voided = shadow_is_void_status(status)
    if voided:
        return {
            "shadow_id": shadow_id,
            "shadow_found": True,
            "shadow_recognized": False,
            "shadow_rejected": True,
            "shadow_voided": True,
            "shadow_overdue": bool(overdue),
            "shadow_status": status,
            "shadow_reason": "shadow_expired_void" if overdue else "shadow_void",
        }

    # open 且未超龄 / 已升正：仍然不是生效来源，求值只走正式规则。
    return {
        "shadow_id": shadow_id,
        "shadow_found": True,
        "shadow_recognized": False,
        "shadow_rejected": False,
        "shadow_voided": False,
        "shadow_overdue": False,
        "shadow_status": status,
        "shadow_reason": "shadow_not_effective_official_only",
    }


@router.post("")
def evaluate(body: EvalIn, authorization: str | None = Header(default=None)):
    current_user(authorization)
    flag_key = require_str(body.flag_key, "开关键", 2)
    env_code = require_str(body.env_code, "环境", 2)
    subject = require_str(body.subject_key, "主体键", 1)
    shadow_id = (
        body.shadow_id
        if body.shadow_id is not None
        else body.release_shadow_id
        if body.release_shadow_id is not None
        else body.shadow
    )
    with connect() as conn:
        fl = conn.execute("SELECT * FROM flags WHERE key=?", (flag_key,)).fetchone()
        if not fl:
            raise HTTPException(404, detail="开关不存在")
        env = conn.execute("SELECT * FROM environments WHERE code=?", (env_code,)).fetchone()
        if not env:
            raise HTTPException(404, detail="环境不存在")

        gate = _shadow_joint_gate(conn, shadow_id)
        if gate:
            # 任何影子都不是生效来源（兼容别名字段）。
            gate["shadow_effective"] = False

        def emit(payload: dict) -> dict:
            # 求值来源恒为正式规则版本；若联查硬拒影子，把 shadow 原因前置到 reason。
            payload["effective_source"] = "official"
            payload["source"] = "official"
            if payload.get("version") is not None:
                payload["effective_version"] = payload["version"]
            payload.update(gate)
            if gate.get("shadow_rejected"):
                payload["reason"] = f"{gate['shadow_reason']}|{payload.get('reason', 'official')}"
            return payload

        if fl["status"] == "killed":
            return emit({
                "enabled": False,
                "reason": "flag_killed",
                "flag_key": flag_key,
                "env_code": env_code,
                "subject_key": subject,
            })
        if fl["status"] == "archived":
            return emit({
                "enabled": False,
                "reason": "flag_archived",
                "flag_key": flag_key,
                "env_code": env_code,
                "subject_key": subject,
            })
        ov = conn.execute(
            "SELECT * FROM overrides WHERE flag_id=? AND env_id=? AND subject_key=? AND active=1",
            (fl["id"], env["id"], subject),
        ).fetchone()
        if ov:
            enabled = ov["effect"] == "force_on"
            return emit({
                "enabled": enabled,
                "reason": f"override:{ov['effect']}",
                "flag_key": flag_key,
                "env_code": env_code,
                "subject_key": subject,
                "version": None,
            })
        rule = conn.execute(
            "SELECT * FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1",
            (fl["id"], env["id"]),
        ).fetchone()
        if not rule:
            return emit({
                "enabled": False,
                "reason": "no_official_rule",
                "version": None,
                "percent": None,
                "flag_key": flag_key,
                "env_code": env_code,
                "subject_key": subject,
            })
        percent_hit = eval_percent(subject, int(rule["percent"]), flag_key)
        cohort_hit = False
        if rule["cohort_id"]:
            cohort_hit = bool(
                conn.execute(
                    "SELECT 1 FROM cohort_members WHERE cohort_id=? AND subject_key=?",
                    (rule["cohort_id"], subject),
                ).fetchone()
            )
        enabled = combine_rule(rule["kind"], percent_hit, cohort_hit)
        return emit({
            "enabled": enabled,
            "reason": "rule",
            "kind": rule["kind"],
            "percent": rule["percent"],
            "percent_hit": percent_hit,
            "cohort_hit": cohort_hit,
            "version": rule["version"],
            "official": 1,
            "flag_key": flag_key,
            "env_code": env_code,
            "subject_key": subject,
        })


@router.get("/board")
def eval_board(authorization: str | None = Header(default=None)):
    """总览：各环境正式规则摘要；附影子作废联查计数。"""
    current_user(authorization)
    with connect() as conn:
        # 打开求值板也顺手按甲路径作废超龄影子，保证计数与联查口径一致。
        from releases_svc import void_overdue_release_shadows

        void_overdue_release_shadows(conn, actor="system:auto")
        conn.commit()
        rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT f.key AS flag_key, f.status, e.code AS env_code, r.kind, r.percent, r.version, r.cohort_id, r.official
                FROM flag_rules r
                JOIN flags f ON f.id=r.flag_id
                JOIN environments e ON e.id=r.env_id
                WHERE r.official=1
                ORDER BY f.key, e.risk_level
                """
            ).fetchall()
        ]
        by_status = [
            dict(x)
            for x in conn.execute("SELECT status, COUNT(*) c FROM flags GROUP BY status").fetchall()
        ]
        open_shadow_count = conn.execute(
            "SELECT COUNT(*) c FROM release_shadows WHERE status='open'"
        ).fetchone()["c"]
        void_shadow_count = conn.execute(
            "SELECT COUNT(*) c FROM release_shadows "
            "WHERE status IN ('void','voided','expired','expired_void','discarded')"
        ).fetchone()["c"]
        overdue_open_count = conn.execute(
            "SELECT COUNT(*) c FROM release_shadows WHERE status='open' AND created_at < ?",
            (time.time() - 3600,),
        ).fetchone()["c"]
    return {
        "rules": rows,
        "by_status": by_status,
        "official_only": True,
        "open_shadow_count": open_shadow_count,
        "void_shadow_count": void_shadow_count,
        "overdue_open_count": overdue_open_count,
    }
