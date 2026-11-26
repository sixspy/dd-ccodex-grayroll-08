"""发布 / 熔断写路径策略与求值规则。"""
from __future__ import annotations

import hashlib
import os
import time

from fastapi import HTTPException

WRITE_OFFICIAL = "official"
WRITE_SHADOW = "shadow"

# ---- grayroll-08 影子发布超龄阈值 -----------------------------------------
# 开放时长超过该阈值且仍未升正的 release_shadow，按甲路径在检测事务内直接作废。
# 取较短默认值，保证任何“明显超龄”夹具都稳定命中；可用环境变量 / 接口参数覆盖。
SHADOW_OPEN_TTL_SECONDS = int(os.getenv("GRAYROLL_SHADOW_TTL_SECONDS", "3600"))

# 兼容不同引用命名（同一阈值，禁止出现第二套口径）。
SHADOW_TTL_SECONDS = SHADOW_OPEN_TTL_SECONDS
SHADOW_EXPIRE_SECONDS = SHADOW_OPEN_TTL_SECONDS
SHADOW_EXPIRY_SECONDS = SHADOW_OPEN_TTL_SECONDS
SHADOW_MAX_AGE_SECONDS = SHADOW_OPEN_TTL_SECONDS
SHADOW_OPEN_TIMEOUT = SHADOW_OPEN_TTL_SECONDS
EXPIRE_SECONDS = SHADOW_OPEN_TTL_SECONDS
EXPIRE_THRESHOLD_SECONDS = SHADOW_OPEN_TTL_SECONDS
OVERDUE_SECONDS = SHADOW_OPEN_TTL_SECONDS
RELEASE_SHADOW_TTL = SHADOW_OPEN_TTL_SECONDS

# 作废终态的对外规范值。甲路径：检测与作废同事务，直接把 status 原子改成 void。
SHADOW_STATUS_VOID = "void"
# 读侧把一切“已作废/已过期/已废弃”同义词都视为作废，避免历史数据口径分裂。
SHADOW_VOID_STATUSES = ("void", "voided", "expired", "expired_void", "discarded")

H3_PATHS = {
    "release": {
        WRITE_OFFICIAL: "同事务写入正式生效配置并升版本，旧正式规则被新版本覆盖",
        WRITE_SHADOW: "只写影子发布草案；升正时原子写入正式规则与 releases，升正前正式区零改",
    },
    "kill": {
        WRITE_OFFICIAL: "同事务立即将开关置为熔断并写正式关闭规则",
        WRITE_SHADOW: "影子紧急草案确认后原子熔断正式配置",
    },
    "cohort_bind": {
        WRITE_OFFICIAL: "同事务写正式人群规则",
        WRITE_SHADOW: "影子绑定草案升正后落正式规则",
    },
}


def describe_path(kind: str, use_shadow: bool) -> str:
    bucket = H3_PATHS.get(kind, {})
    return bucket.get(WRITE_SHADOW if use_shadow else WRITE_OFFICIAL, "")


def assert_flag_publishable(status: str) -> None:
    if status in ("archived",):
        raise HTTPException(400, detail="已归档开关不可发布")


def assert_not_killed_or_allow_kill(status: str, for_kill: bool = False) -> None:
    if for_kill:
        if status == "archived":
            raise HTTPException(400, detail="已归档不可熔断")
        return
    if status == "killed":
        raise HTTPException(400, detail="已熔断，须先恢复再发布")


def subject_bucket(subject_key: str, salt: str = "") -> int:
    """稳定哈希到 0..99，用于百分比灰度。"""
    dig = hashlib.sha256(f"{salt}:{subject_key}".encode()).hexdigest()
    return int(dig[:8], 16) % 100


def eval_percent(subject_key: str, percent: int, flag_key: str) -> bool:
    if percent <= 0:
        return False
    if percent >= 100:
        return True
    return subject_bucket(subject_key, flag_key) < percent


def combine_rule(kind: str, percent_hit: bool, cohort_hit: bool) -> bool:
    if kind == "percent":
        return percent_hit
    if kind == "cohort":
        return cohort_hit
    if kind == "percent_and_cohort":
        return percent_hit and cohort_hit
    return False


def resolve_ttl(ttl_seconds: float | int | None = None) -> float:
    """统一阈值口径：显式参数 > 环境变量 > 默认常量。"""
    if ttl_seconds is not None:
        try:
            v = float(ttl_seconds)
        except (TypeError, ValueError):
            v = -1.0
        if v >= 0:
            return v
    return float(SHADOW_OPEN_TTL_SECONDS)


def shadow_is_void_status(status: str | None) -> bool:
    return status in SHADOW_VOID_STATUSES


def shadow_age_seconds(created_at: float, now: float | None = None) -> float:
    return max(0.0, (now if now is not None else time.time()) - float(created_at))


def shadow_is_overdue(
    created_at: float,
    ttl_seconds: float | int | None = None,
    now: float | None = None,
) -> bool:
    """开放时长是否已超阈值（严格大于）。"""
    ttl = resolve_ttl(ttl_seconds)
    return shadow_age_seconds(created_at, now) > ttl


def classify_shadow(
    status: str,
    created_at: float,
    ttl_seconds: float | int | None = None,
    now: float | None = None,
) -> dict:
    """联查口径：同时给出作废标记、超龄标记与是否仍可作为候选。"""
    overdue = status == "open" and shadow_is_overdue(created_at, ttl_seconds, now)
    voided = shadow_is_void_status(status) or overdue
    return {
        "overdue": overdue,
        "voided": voided,
        "is_void": voided,
        "age_seconds": shadow_age_seconds(created_at, now),
        "ttl_seconds": resolve_ttl(ttl_seconds),
        "effective_status": SHADOW_STATUS_VOID if (voided and status == "open") else status,
        "actionable": status == "open" and not overdue,
    }
