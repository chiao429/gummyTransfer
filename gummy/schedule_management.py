"""Immutable, quota-reviewed schedule revisions; no supplier requests or file mutation."""

import hashlib
import json
from datetime import datetime, time, timedelta

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import insert, select

from gummy.db import runs, schedule_versions, tasks, utc_now
from gummy.locking import process_lock
from gummy.plan import CollectionPlan, PlannedBatch, load_plan, plan_status
from gummy.scheduled import local_now


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class ScheduleEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    collect_at: str = Field(pattern=r"^\d{2}:\d{2}$")
    report_at: str = Field(pattern=r"^\d{2}:\d{2}$")
    batches: list[PlannedBatch] = Field(max_length=30)

    @model_validator(mode="after")
    def times(self):
        from datetime import time

        start, end = time.fromisoformat(self.collect_at), time.fromisoformat(self.report_at)
        if start < time(9) or end <= start:
            raise ValueError("採集不得早於台灣 09:00，報表必須晚於採集。")
        return self


def revisions(db, base_sha):
    with db.engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                select(schedule_versions)
                .where(schedule_versions.c.base_sha256 == base_sha)
                .order_by(schedule_versions.c.id)
            ).mappings()
        ]


def effective_schedule(db, path, *, day=None, upcoming=False):
    from gummy.python_scheduler import load_scheduler

    cfg, plan, base = load_scheduler(path)
    history = revisions(db, base)
    latest = history[-1] if history else None
    selected = (
        latest
        if upcoming
        else next(
            (
                row
                for row in reversed(history)
                if row["effective_from"] <= (day or local_now().date()).isoformat()
            ),
            None,
        )
    )
    if latest:
        plan = CollectionPlan.model_validate(latest["plan"])
        plan_sha = digest(latest["plan"])
    else:
        plan_sha = cfg.expected_sha256
    if selected:
        if set(selected["config"]) != {"collect_at", "report_at"}:
            raise ValueError("排程版本含不支援的設定。")
        cfg = type(cfg).model_validate({**cfg.model_dump(), **selected["config"]})
    return (
        cfg,
        plan,
        {
            "base_sha256": base,
            "plan_sha256": plan_sha,
            "revision": latest["id"] if latest else 0,
            "time_revision": selected["id"] if selected else 0,
            "latest_revision": latest["id"] if latest else 0,
            "enabled": latest["enabled"] if latest else True,
            "effective_from": latest["effective_from"] if latest else None,
            "history": history,
        },
    )


def managed_plan(db, settings):
    if settings.scheduler_config_path:
        return effective_schedule(db, settings.scheduler_config_path, upcoming=True)[1]
    return load_plan(settings.active_plan_path)[0]


def preview_schedule(db, settings, edit, now=None):
    now = now or local_now()
    if not settings.scheduler_config_path or settings.app_env != "local":
        raise ValueError("尚未設定本機 Python 排程器。")
    cfg, current, meta = effective_schedule(db, settings.scheduler_config_path, upcoming=True)
    if any(batch.date <= now.date() for batch in edit.batches):
        raise ValueError("只能修改明天起的安排，今天與過去資料保留。")
    fixed = [b for b in current.batches if b.date <= now.date()]
    skipped = set(current.skipped_dates)
    for batch in fixed:
        if batch.date >= now.date() or batch.date in skipped:
            continue
        historical = [r for r in meta["history"] if r["effective_from"] <= batch.date.isoformat()]
        collect_at = historical[-1]["config"]["collect_at"] if historical else "10:00"
        # The original file remains authoritative for the initial time.
        if not historical:
            from gummy.python_scheduler import load_scheduler

            collect_at = load_scheduler(settings.scheduler_config_path)[0].collect_at
        due = datetime.combine(batch.date, time.fromisoformat(collect_at), now.tzinfo)
        prior_controls = [
            r for r in meta["history"] if datetime.fromisoformat(r["created_at"]) <= due
        ]
        original_request = current.request_for(batch.date)
        if (
            prior_controls
            and not prior_controls[-1]["enabled"]
            and original_request
            and db.run_by_key(current.source, original_request.run_key) is None
        ):
            skipped.add(batch.date)
    proposed = CollectionPlan(
        plan_id=current.plan_id,
        source=current.source,
        country=current.country,
        batches=fixed + edit.batches,
        skipped_dates=sorted(skipped),
    )
    if (proposed.batches[-1].date - proposed.batches[0].date).days > 30:
        raise ValueError("本試點計畫須限制在 30 天內。")
    with db.engine.connect() as conn:
        pending = [
            dict(r)
            for r in conn.execute(
                select(tasks).where(tasks.c.status.in_(["QUEUED", "RUNNING"]))
            ).mappings()
        ]
        running = conn.execute(select(runs.c.id).where(runs.c.status == "RUNNING")).first()
    held = sum(r["reserved_requests"] for r in pending if r["source"] == "rapidapi")
    old = plan_status(current, db, settings, now.date())
    new = plan_status(proposed, db, settings, now.date())
    quota = db.quota_status(settings.total_limit, settings.daily_limit)
    provider = (
        db.provider_quota_status(settings.rapidapi_key) if proposed.source == "rapidapi" else None
    )
    reasons = []
    if running or any(t["status"] == "RUNNING" for t in pending):
        reasons.append("有工作正在執行，請等完成後再套用。")
    if edit.enabled:
        if new["status"] not in {"READY", "FINISHED_OR_EXPIRED"}:
            reasons.append("計畫有漏跑、來源核對、批次衝突或額度問題；請先處理。")
        if proposed.source == "rapidapi":
            if not settings.rapidapi_key or not settings.free_plan_confirmed:
                reasons.append("需設定金鑰並確認免費硬上限方案。")
            if (
                quota["total_used"] + held + new["remaining_planned_requests"]
                > settings.total_limit
            ):
                reasons.append("試點剩餘不足以涵蓋排隊工作及新計畫。")
            if (
                provider is None
                or provider["remaining_estimate"] <= held + new["remaining_planned_requests"]
            ):
                reasons.append("供應商最近觀測不足或未知，請先查供應商控制台。")
    state = {
        "edit": edit.model_dump(mode="json"),
        "base": meta["base_sha256"],
        "revision": meta["latest_revision"],
        "day": now.date().isoformat(),
        "quota": quota,
        "provider": provider,
        "pending": [(t["id"], t["status"], t["reserved_requests"]) for t in pending],
        "runs": new["batches"],
    }
    return {
        "allowed": not reasons,
        "reasons": reasons,
        "fingerprint": digest(state),
        "before": current.model_dump(mode="json"),
        "after": proposed.model_dump(mode="json"),
        "old_requests": old["remaining_planned_requests"],
        "new_requests": new["remaining_planned_requests"],
        "queued_reserved": held,
        "quota": quota,
        "provider": provider,
        "before_times": [cfg.collect_at, cfg.report_at],
        "effective_from": (now.date() + timedelta(days=1)).isoformat(),
        "revision": meta["latest_revision"],
        "enabled_before": meta["enabled"],
        "skipped_dates": [day.isoformat() for day in sorted(skipped)],
    }


def apply_schedule(db, settings, edit, fingerprint, confirmation_id, now=None):
    with process_lock(db, "admission"), process_lock(db, "schedule-control"), process_lock(db):
        with db.engine.connect() as conn:
            previous = (
                conn.execute(
                    select(schedule_versions).where(
                        schedule_versions.c.confirmation_id == confirmation_id
                    )
                )
                .mappings()
                .first()
            )
        if previous:
            return previous["id"]
        check = preview_schedule(db, settings, edit, now)
        if not check["allowed"] or check["fingerprint"] != fingerprint:
            raise ValueError("預覽後設定、工作或額度已變更，請重新預覽。")
        _, _, meta = effective_schedule(db, settings.scheduler_config_path, upcoming=True)
        with db.engine.begin() as conn:
            result = conn.execute(
                insert(schedule_versions).values(
                    confirmation_id=confirmation_id,
                    base_sha256=meta["base_sha256"],
                    plan=check["after"],
                    config={"collect_at": edit.collect_at, "report_at": edit.report_at},
                    enabled=edit.enabled,
                    effective_from=check["effective_from"],
                    created_at=utc_now(),
                )
            )
            return result.inserted_primary_key[0]
