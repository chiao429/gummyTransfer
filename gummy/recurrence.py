"""Durable recurring rules and all-category admission; no provider I/O in the scheduler."""

import hashlib
import json
import secrets
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import JSON, Column, ForeignKey, Integer, String, Table, insert, select, update

from gummy.db import metadata, runs, tasks, utc_now
from gummy.errors import RunConflict
from gummy.locking import process_lock
from gummy.models import CollectRequest, SourceName

TZ = ZoneInfo("Asia/Taipei")
control = Table(
    "recurring_control",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("cutover_at", String(40), nullable=False),
    Column("base_sha256", String(64), nullable=False),
)
rules = Table(
    "recurring_rules",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("spec", JSON, nullable=False),
    Column("cursor", String(40), nullable=False),
    Column("created_at", String(40), nullable=False),
)
versions = Table(
    "recurring_versions",
    metadata,
    Column("confirmation_id", String(32), primary_key=True),
    Column("rule_id", ForeignKey("recurring_rules.id"), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("spec", JSON, nullable=False),
    Column("created_at", String(40), nullable=False),
)
occurrences = Table(
    "recurring_occurrences",
    metadata,
    Column("id", ForeignKey("operation_tasks.id"), primary_key=True),
    Column("rule_id", ForeignKey("recurring_rules.id"), nullable=False),
    Column("scheduled_at", String(40), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("spec", JSON, nullable=False),
    Column("trigger", String(16), nullable=False),
    Column("results", JSON, nullable=False),
)


class Category(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    category: str = Field(min_length=1, max_length=80)
    query: str = Field(min_length=1, max_length=120)
    max_products: int = Field(default=2, ge=1, le=2)


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=80)
    source: SourceName = "rapidapi"
    frequency: str = Field(pattern="^(daily|weekly|once)$")
    weekdays: list[int] = Field(default_factory=list, max_length=7)
    at: str = Field(default="10:00", pattern=r"^\d{2}:\d{2}$")
    start: date
    end: date | None = None
    state: str = Field(default="enabled", pattern="^(enabled|paused|archived)$")
    categories: list[Category] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def valid(self):
        time.fromisoformat(self.at)
        if self.frequency == "once":
            self.end = self.start
        if self.end and self.end < self.start:
            raise ValueError("結束日期不可早於開始日期")
        if self.frequency == "weekly":
            if not self.weekdays or any(d not in range(7) for d in self.weekdays):
                raise ValueError("請至少選一個星期")
            self.weekdays = sorted(set(self.weekdays))
        else:
            self.weekdays = []
        if self.frequency == "once":
            self.end = self.start
        if len({(c.category, c.query) for c in self.categories}) != len(self.categories):
            raise ValueError("同一排程不可重複相同分類與關鍵字")
        return self

    @property
    def requests(self):
        return sum(1 + c.max_products for c in self.categories) if self.source == "rapidapi" else 0


def stamp(now=None):
    return (now or datetime.now(UTC)).astimezone(UTC).isoformat()


def active(db):
    with db.engine.connect() as conn:
        return conn.execute(select(control.c.id)).first() is not None


def all_rules(db):
    with db.engine.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(select(rules).order_by(rules.c.created_at, rules.c.id)).mappings()
        ]


def get_rule(db, rule_id):
    with db.engine.connect() as conn:
        row = conn.execute(select(rules).where(rules.c.id == rule_id)).mappings().first()
    if not row:
        raise ValueError("找不到排程")
    return dict(row)


def times(spec, after, through):
    """Bounded caller-supplied range, open on the left; wall clock interpreted in Taiwan."""
    day = max(spec.start, after.astimezone(TZ).date())
    last = min(spec.end or through.astimezone(TZ).date(), through.astimezone(TZ).date())
    while day <= last:
        due = datetime.combine(day, time.fromisoformat(spec.at), TZ).astimezone(UTC)
        if after < due <= through and (
            spec.frequency != "weekly" or day.weekday() in spec.weekdays
        ):
            yield due
        day += timedelta(days=1)


def forecast(spec, now=None, days=30):
    now = now or datetime.now(UTC)
    if spec.state != "enabled":
        return []
    return list(times(spec, now, now + timedelta(days=days)))


def next_times(spec, now=None, count=5):
    now = now or datetime.now(UTC)
    if spec.state != "enabled":
        return []
    beginning = max(
        now, datetime.combine(spec.start, time.min, TZ).astimezone(UTC) - timedelta(microseconds=1)
    )
    return list(times(spec, beginning, beginning + timedelta(days=40)))[:count]


def budget(db, settings, spec, ignore_task=None):
    q = db.quota_status(settings.total_limit, settings.daily_limit)
    with db.engine.connect() as conn:
        held = sum(
            r["reserved_requests"]
            for r in conn.execute(
                select(tasks).where(
                    tasks.c.status.in_(["QUEUED", "RUNNING"]), tasks.c.source == "rapidapi"
                )
            ).mappings()
            if r["id"] != ignore_task
        )
    provider = (
        db.provider_quota_status(settings.rapidapi_key) if spec.source == "rapidapi" else None
    )
    reasons = []
    if spec.requests:
        if not settings.rapidapi_key or not settings.free_plan_confirmed:
            reasons.append("尚未設定金鑰或確認免費硬上限方案")
        if q["total_used"] + held + spec.requests > q["total_limit"]:
            reasons.append("本機累計剩餘次數不足以完成全部分類")
        if q["daily_used"] + held + spec.requests > q["daily_limit"]:
            reasons.append("本機今天剩餘次數不足以完成全部分類")
        if provider is None:
            reasons.append("供應商剩餘次數未知，請核對控制台")
        elif provider["remaining_estimate"] <= held + spec.requests:
            reasons.append("供應商最近觀測的剩餘估計不足（保留安全餘量）")
    return {
        "allowed": not reasons,
        "reasons": reasons,
        "local": q,
        "provider": provider,
        "held": held,
        "requests": spec.requests,
    }


def fingerprint(db, settings, spec, rule_id, now=None):
    from gummy.operations import config_binding

    row = get_rule(db, rule_id) if rule_id else None
    payload = {
        "spec": spec.model_dump(mode="json"),
        "rule_id": rule_id,
        "revision": row["revision"] if row else 0,
        "budget": budget(db, settings, spec),
        "binding": config_binding(settings),
        "day": stamp(now)[:10],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _task(conn, settings, row, due, status, reason="", trigger="schedule", identity=None):
    from gummy.operations import config_binding

    spec = Rule.model_validate(row["spec"])
    tid = identity or hashlib.sha256((row["id"] + ":" + stamp(due)).encode()).hexdigest()[:32]
    if conn.execute(select(tasks.c.id).where(tasks.c.id == tid)).first():
        return tid
    first = spec.categories[0]
    conn.execute(
        insert(tasks).values(
            id=tid,
            kind="schedule",
            source=spec.source,
            request={
                "category": spec.name,
                "query": first.query,
                "max_products": first.max_products,
                "occurrence_id": tid,
            },
            binding=config_binding(settings),
            status=status,
            phase="queued" if status == "QUEUED" else "stopped",
            reserved_requests=spec.requests if status == "QUEUED" else 0,
            cancel_requested=False,
            created_at=stamp(due),
            updated_at=utc_now(),
            completed_at=None if status == "QUEUED" else utc_now(),
            error_code=reason or None,
        )
    )
    conn.execute(
        insert(occurrences).values(
            id=tid,
            rule_id=row["id"],
            scheduled_at=stamp(due),
            revision=row["revision"],
            spec=row["spec"],
            trigger=trigger,
            results=[],
        )
    )
    return tid


def migrate(db, settings, now=None):
    """Explicit cutover under the legacy scheduler's lock; never contacts the provider."""
    from gummy.python_scheduler import load_scheduler
    from gummy.schedule_management import effective_schedule

    now = now or datetime.now(UTC)
    with process_lock(db, "admission"), process_lock(db, "schedule-control"), process_lock(db):
        if active(db):
            return
        cfg, plan, meta = effective_schedule(db, settings.scheduler_config_path, upcoming=True)
        with db.engine.connect() as conn:
            if conn.execute(
                select(tasks.c.id).where(tasks.c.status.in_(["QUEUED", "RUNNING"]))
            ).first():
                raise RunConflict("請等現有任務完成再切換")
            if conn.execute(select(runs.c.id).where(runs.c.status == "RUNNING")).first():
                raise RunConflict("仍有執行中或中斷未確認的採集，請先檢查")
        pending = []
        for batch in plan.batches:
            request = plan.request_for(batch.date)
            due = datetime.combine(batch.date, time.fromisoformat(cfg.collect_at), TZ)
            if request and due > now and db.run_by_key(plan.source, request.run_key) is None:
                spec = Rule(
                    name=batch.category,
                    source=plan.source,
                    frequency="once",
                    start=batch.date,
                    at=cfg.collect_at,
                    state="enabled" if meta["enabled"] else "paused",
                    categories=[
                        Category(
                            category=batch.category,
                            query=batch.query,
                            max_products=batch.max_products,
                        )
                    ],
                )
                pending.append(spec)
        with db.engine.begin() as conn:
            conn.execute(
                insert(control).values(
                    id=1,
                    cutover_at=stamp(now),
                    base_sha256=load_scheduler(settings.scheduler_config_path)[2],
                )
            )
            for spec in pending:
                rid = secrets.token_hex(16)
                conn.execute(
                    insert(rules).values(
                        id=rid,
                        revision=1,
                        spec=spec.model_dump(mode="json"),
                        cursor=stamp(now),
                        created_at=stamp(now),
                    )
                )
                conn.execute(
                    insert(versions).values(
                        confirmation_id=secrets.token_hex(16),
                        rule_id=rid,
                        revision=1,
                        spec=spec.model_dump(mode="json"),
                        created_at=stamp(now),
                    )
                )


def save_rule(db, settings, spec, rule_id, expected, confirmation_id, now=None):
    now = now or datetime.now(UTC)
    with process_lock(db, "admission"), process_lock(db, "schedule-control"):
        if not active(db):
            raise ValueError("循環排程尚未切換")
        with db.engine.connect() as conn:
            prior = (
                conn.execute(select(versions).where(versions.c.confirmation_id == confirmation_id))
                .mappings()
                .first()
            )
        if prior:
            if prior["spec"] != spec.model_dump(mode="json"):
                raise ValueError("確認內容不符")
            return prior["rule_id"]
        if fingerprint(db, settings, spec, rule_id, now) != expected:
            raise ValueError("設定或用量已變更，請重新預覽")
        if spec.state == "enabled" and not next_times(spec, now):
            raise ValueError("沒有未來執行時間，請調整日期")
        old = get_rule(db, rule_id) if rule_id else None
        if old and old["spec"]["state"] == "archived":
            raise ValueError("封存排程不可重新啟用；請新增排程")
        rid, rev = rule_id or secrets.token_hex(16), (old["revision"] + 1 if old else 1)
        with db.engine.begin() as conn:
            if old:
                # Record elapsed but not dispatched slots before replacing their rule snapshot.
                for due in times(
                    Rule.model_validate(old["spec"]), datetime.fromisoformat(old["cursor"]), now
                ):
                    status = (
                        "MISSED"
                        if old["spec"]["state"] == "enabled" and now >= due + timedelta(minutes=15)
                        else "SKIPPED"
                    )
                    _task(conn, settings, old, due, status, "rule_changed")
                ids = select(occurrences.c.id).where(occurrences.c.rule_id == rid)
                conn.execute(
                    update(tasks)
                    .where(tasks.c.id.in_(ids), tasks.c.status == "QUEUED")
                    .values(
                        status="CANCELLED",
                        phase="stopped",
                        reserved_requests=0,
                        completed_at=stamp(now),
                        updated_at=stamp(now),
                        error_code="rule_changed",
                    )
                )
                conn.execute(
                    update(rules)
                    .where(rules.c.id == rid)
                    .values(revision=rev, spec=spec.model_dump(mode="json"), cursor=stamp(now))
                )
            else:
                conn.execute(
                    insert(rules).values(
                        id=rid,
                        revision=rev,
                        spec=spec.model_dump(mode="json"),
                        cursor=stamp(now),
                        created_at=stamp(now),
                    )
                )
            conn.execute(
                insert(versions).values(
                    confirmation_id=confirmation_id,
                    rule_id=rid,
                    revision=rev,
                    spec=spec.model_dump(mode="json"),
                    created_at=stamp(now),
                )
            )
        return rid


def dispatch(db, settings, now=None):
    """Caller holds schedule-control; durable unique tasks precede cursor advancement."""
    now = now or datetime.now(UTC)
    due_rows = []
    for row in all_rules(db):
        spec = Rule.model_validate(row["spec"])
        if spec.state == "archived":
            continue
        for due in times(spec, datetime.fromisoformat(row["cursor"]), now):
            due_rows.append((due, row["id"], row, spec))
    emitted = []
    with process_lock(db, "admission"), process_lock(db):
        for due, _, row, spec in sorted(due_rows, key=lambda r: (r[0], r[1])):
            status, reason = "QUEUED", ""
            if spec.state != "enabled":
                status, reason = "SKIPPED", "paused"
            elif now >= due + timedelta(minutes=15):
                status, reason = "MISSED", "collection_window_closed"
            else:
                check = budget(db, settings, spec)
                if not check["allowed"]:
                    status, reason = "BLOCKED", "budget_or_provider"
            with db.engine.begin() as conn:
                tid = _task(conn, settings, row, due, status, reason)
            emitted.append(tid)
        with db.engine.begin() as conn:
            conn.execute(update(rules).where(rules.c.cursor < stamp(now)).values(cursor=stamp(now)))
    return emitted


def run_now(db, settings, rule_id, expected, confirmation_id, now=None):
    now = now or datetime.now(UTC)
    with process_lock(db, "admission"), process_lock(db, "schedule-control"), process_lock(db):
        with db.engine.connect() as conn:
            if conn.execute(select(tasks.c.id).where(tasks.c.id == confirmation_id)).first():
                return confirmation_id
        row = get_rule(db, rule_id)
        spec = Rule.model_validate(row["spec"])
        if spec.state == "archived" or fingerprint(db, settings, spec, rule_id, now) != expected:
            raise ValueError("排程已變更，請重新預覽")
        check = budget(db, settings, spec)
        if not check["allowed"]:
            raise ValueError("；".join(check["reasons"]))
        with db.engine.begin() as conn:
            return _task(
                conn, settings, row, now, "QUEUED", trigger="manual", identity=confirmation_id
            )


def occurrence(db, tid):
    with db.engine.connect() as conn:
        row = (
            conn.execute(
                select(occurrences, tasks.c.status, tasks.c.error_code)
                .join(tasks, tasks.c.id == occurrences.c.id)
                .where(occurrences.c.id == tid)
            )
            .mappings()
            .first()
        )
    if row is None:
        raise ValueError("找不到執行紀錄")
    return dict(row)


def history(db, rule_id=None, limit=100):
    with db.engine.connect() as conn:
        query = select(occurrences, tasks.c.status, tasks.c.error_code).join(
            tasks, tasks.c.id == occurrences.c.id
        )
        if rule_id:
            query = query.where(occurrences.c.rule_id == rule_id)
        return [
            dict(r)
            for r in conn.execute(
                query.order_by(occurrences.c.scheduled_at.desc()).limit(limit)
            ).mappings()
        ]


def process_schedule(db, settings, task, transport=None):
    from gummy.operations import config_binding, get_task, set_phase
    from gummy.worker import _collect

    row = occurrence(db, task["id"])
    spec = Rule.model_validate(row["spec"])
    results = list(row["results"])
    outcome = "SUCCEEDED"
    try:
        # Hold collection for ALL categories. CLI and reviews cannot steal the reserved budget.
        with process_lock(db):
            if task["binding"] != config_binding(settings):
                raise ValueError("configuration_changed")
            if not budget(db, settings, spec, ignore_task=task["id"])["allowed"]:
                set_phase(
                    db,
                    task["id"],
                    "stopped",
                    status="BLOCKED",
                    reserved_requests=0,
                    completed_at=utc_now(),
                    error_code="budget_or_provider",
                )
                return get_task(db, task["id"])
            for index, category in enumerate(spec.categories):
                request = CollectRequest(
                    source=spec.source, run_key=f"rec-{task['id']}-{index}", **category.model_dump()
                )

                def progress(phase, **info):
                    set_phase(
                        db,
                        task["id"],
                        phase,
                        run_id=info["run_id"],
                        detail=f"分類 {index + 1} / {len(spec.categories)}",
                    )

                result = _collect(db, settings, request, transport, progress)
                results.append({"index": index, "run_id": result["run_id"], "status": "SUCCEEDED"})
                with db.engine.begin() as conn:
                    conn.execute(
                        update(occurrences)
                        .where(occurrences.c.id == task["id"])
                        .values(results=results)
                    )
    except Exception as exc:
        outcome = "PARTIAL" if results else "FAILED"
        set_phase(db, task["id"], "stopped", error_code=type(exc).__name__)
    set_phase(db, task["id"], "report", reserved_requests=0)
    if results:
        try:
            from gummy.recurrence_report import build_report

            build_report(db, settings, task["id"], task["id"])
        except Exception as exc:
            outcome = "DATA_READY"
            set_phase(db, task["id"], "stopped", error_code=type(exc).__name__)
    set_phase(db, task["id"], "done", status=outcome, reserved_requests=0, completed_at=utc_now())
    return get_task(db, task["id"])


def overview(db, settings, now=None):
    now = now or datetime.now(UTC)
    previews, rows = [], []
    for row in all_rules(db):
        spec = Rule.model_validate(row["spec"])
        upcoming = forecast(spec, now)
        rows.append(
            {
                **row,
                "next_at": stamp(next_times(spec, now, 1)[0]) if next_times(spec, now, 1) else None,
                "requests_per_run": spec.requests,
                "forecast_requests": len(upcoming) * spec.requests,
            }
        )
        previews.extend(
            {
                "rule_id": row["id"],
                "name": spec.name,
                "source": spec.source,
                "scheduled_at": stamp(due),
                "requests": spec.requests,
                "category_count": len(spec.categories),
            }
            for due in upcoming
        )
    previews.sort(key=lambda r: (r["scheduled_at"], r["rule_id"]))
    return {
        "rules": rows,
        "upcoming": previews,
        "next_at": min((r["next_at"] for r in rows if r["next_at"]), default=None),
        "history": history(db),
        "timezone": "Asia/Taipei",
        "report_trigger": "after_collection",
    }


def recover_results(db, tid):
    """Recover committed snapshots after a worker crash; never replay provider requests."""
    row = occurrence(db, tid)
    results = []
    for index, _ in enumerate(row["spec"]["categories"]):
        run = db.run_by_key(row["spec"]["source"], f"rec-{tid}-{index}")
        if run and run["status"] == "SUCCEEDED":
            results.append({"index": index, "run_id": run["id"], "status": "SUCCEEDED"})
    with db.engine.begin() as conn:
        conn.execute(update(occurrences).where(occurrences.c.id == tid).values(results=results))
    return results
