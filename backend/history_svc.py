"""变更历史。"""
from __future__ import annotations

from fastapi import APIRouter, Header

from auth import current_user
from db import connect

router = APIRouter(prefix="/api/history", tags=["history"])

ACTION_LABELS = {
    "publish": "正式发布",
    "release_shadow": "影子发布草案",
    "release_promote": "影子升正",
    "release_shadow_void": "超龄影子作废",
    "rule_shadow": "规则影子绑定",
    "rule_promote": "规则影子升正",
    "rule_shadow_void": "规则影子作废",
    "kill": "熔断",
    "kill_shadow": "熔断影子草案",
    "kill_promote": "熔断影子升正",
}


@router.get("")
def list_history(
    authorization: str | None = Header(default=None),
    limit: int = 100,
    action: str | None = None,
    flag_id: int | None = None,
    void_only: int = 0,
):
    current_user(authorization)
    where = []
    params: list[object] = []
    if action:
        where.append("h.action = ?")
        params.append(action)
    if flag_id is not None:
        where.append("h.flag_id = ?")
        params.append(flag_id)
    if void_only:
        where.append("h.action LIKE '%void%'")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    with connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                f"""
                SELECT h.*, f.key AS flag_key, e.code AS env_code
                FROM change_history h
                LEFT JOIN flags f ON f.id=h.flag_id
                LEFT JOIN environments e ON e.id=h.env_id
                {where_sql}
                ORDER BY h.id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        ]
        void_count = conn.execute(
            "SELECT COUNT(*) c FROM change_history WHERE action LIKE '%void%'"
        ).fetchone()["c"]
    for it in rows:
        it["action_label"] = ACTION_LABELS.get(it["action"], it["action"])
    return {"items": rows, "void_count": void_count, "action_labels": ACTION_LABELS}
