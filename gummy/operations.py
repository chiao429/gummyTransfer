"""Durable local tasks, conservative admission, source lineage and report registry."""

import hashlib
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, insert, select, update

from gummy.db import (
    attempts,
    operation_reports,
    products,
    runs,
    source_observations,
    task_events,
    tasks,
    utc_now,
    worker_state,
)
from gummy.errors import GummyError, ReportNotFound, RunConflict
from gummy.locking import process_lock
from gummy.models import CollectRequest, SourceName
from gummy.monitor import active_plan_overview, report_content_verified
from gummy.plan import load_plan, plan_status
from gummy.scheduled import local_now
from gummy.worker import collect

ACTIVE = ("QUEUED", "RUNNING")


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: str = Field(default="collect", pattern=r"^(collect|report)$")
    source: SourceName = "rapidapi"
    query: str = Field(default="socket wrench set", min_length=1, max_length=120)
    category: str = Field(default="套筒扳手", min_length=1, max_length=80)
    max_products: int = Field(default=2, ge=1, le=2)
    run_id: int | None = Field(default=None, gt=0, lt=2**63)
    report_scope: dict | None = None
    report_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    parent_report: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")

    @model_validator(mode="after")
    def independent_collection(self):
        if self.kind == "collect" and self.run_id is not None:
            raise ValueError("A new collection cannot use an existing run")
        if self.report_scope is not None:
            from gummy.analysis import AnalysisQuery

            scope = AnalysisQuery.model_validate(self.report_scope)
            if (
                self.kind != "report"
                or scope.source != self.source
                or scope.snapshot is None
                or scope.review_snapshot is None
                or not self.report_digest
            ):
                raise ValueError("Report must have a frozen source scope")
            self.report_scope = scope.model_dump(mode="json")
            if scope.snapshot == "0" or self.run_id != max(
                int(x) for x in scope.snapshot.split(",")
            ):
                raise ValueError("Report cutoff must match its frozen batches")
        elif self.report_digest or self.parent_report:
            raise ValueError("Report scope is required")
        return self


class TaskCancelled(GummyError):
    pass


def config_binding(settings):
    # A change of key, budget, or plan invalidates the confirmation. Never expose this digest.
    plan_digest = ""
    if settings.active_plan_path:
        try:
            plan_digest = hashlib.sha256(Path(settings.active_plan_path).read_bytes()).hexdigest()
        except OSError:
            plan_digest = "unavailable"
    if settings.scheduler_config_path:
        from gummy.db import Database
        from gummy.python_scheduler import load_scheduler
        from gummy.schedule_management import revisions

        binding_db = Database(settings.database_url)
        try:
            base = load_scheduler(settings.scheduler_config_path)[2]
            history = revisions(binding_db, base)
            plan_digest += str(history[-1]["id"] if history else 0)
        finally:
            binding_db.engine.dispose()
    values = [
        settings.rapidapi_key,
        settings.total_limit,
        settings.daily_limit,
        settings.free_plan_confirmed,
        settings.database_url,
        plan_digest,
    ]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def get_task(db, task_id):
    with db.engine.connect() as conn:
        row = conn.execute(select(tasks).where(tasks.c.id == task_id)).mappings().first()
    if row is None:
        raise ReportNotFound("No such task")
    return dict(row)


def worker_health(db):
    with db.engine.connect() as conn:
        row = conn.execute(select(worker_state)).mappings().first()
    if not row:
        return {"online": False, "phase": "unknown", "last_seen_at": None, "task_id": None}
    age = max(0, (datetime.now(UTC) - datetime.fromisoformat(row["last_seen_at"])).total_seconds())
    return {**dict(row), "online": age <= 30, "age_seconds": round(age)}


def heartbeat(db, phase="idle", task_id=None):
    with db.engine.begin() as conn:
        changed = conn.execute(
            update(worker_state)
            .where(worker_state.c.name == "local")
            .values(last_seen_at=utc_now(), phase=phase, task_id=task_id)
        )
        if not changed.rowcount:
            conn.execute(
                insert(worker_state).values(
                    name="local", last_seen_at=utc_now(), phase=phase, task_id=task_id
                )
            )


def admission(db, settings, spec, *, ignore_task=None, require_worker=True):
    reasons = []
    local = db.quota_status(settings.total_limit, settings.daily_limit)
    provider = (
        db.provider_quota_status(settings.rapidapi_key) if spec.source == "rapidapi" else None
    )
    planned = 1 + spec.max_products if spec.kind == "collect" and spec.source == "rapidapi" else 0
    with db.engine.connect() as conn:
        pending = [
            dict(row)
            for row in conn.execute(select(tasks).where(tasks.c.status.in_(ACTIVE))).mappings()
            if row["id"] != ignore_task
        ]
    held = sum(row["reserved_requests"] for row in pending if row["source"] == "rapidapi")
    if len(pending) >= 20:
        reasons.append("待執行任務已達 20 筆，請等待完成或取消部分任務。")
    future = 0
    today_reserved = 0
    plan_state = None
    from gummy.recurrence import active as recurring_active

    if planned and settings.active_plan_path and not recurring_active(db):
        try:
            from gummy.schedule_management import managed_plan

            plan = (
                managed_plan(db, settings)
                if settings.scheduler_config_path
                else load_plan(settings.active_plan_path)[0]
            )
            plan_state = plan_status(plan, db, settings, local_now().date())
            if plan.source == "rapidapi":
                future = plan_state["remaining_planned_requests"]
                today = local_now().date().isoformat()
                if any(
                    b["date"] == today and b["status"] == "PENDING"
                    for b in plan_state.get("batches", [])
                ):
                    batch = plan.request_for(local_now().date())
                    if batch is not None:
                        today_reserved = 1 + batch.max_products
                if plan_state["status"] not in {"READY", "FINISHED_OR_EXPIRED"}:
                    reasons.append("既有採集計畫需要處理，請先查看排程狀態。")
        except (OSError, ValueError):
            reasons.append("無法確認既有採集計畫，暫停新增真實採集。")
    if require_worker and not worker_health(db)["online"]:
        reasons.append("背景執行器尚未連線，請先啟動任務服務。")
    if spec.kind == "collect":
        if any(row["kind"] == "collect" and row["source"] == spec.source for row in pending):
            reasons.append("此來源已有未完成採集，請等待完成或取消排隊任務。")
        if planned:
            if not settings.rapidapi_key or not settings.free_plan_confirmed:
                reasons.append("請先設定 API 金鑰並確認免費硬上限方案。")
            if local["daily_used"] + held + today_reserved + planned > local["daily_limit"]:
                reasons.append("今天可用次數不足或需保留給當天排程，請等待排程完成或重置。")
            if local["total_used"] + held + future + planned > local["total_limit"]:
                reasons.append("試點剩餘次數須保留給既有排程，無法新增這次採集。")
            if provider and provider["remaining_estimate"] <= planned + held + future:
                reasons.append("供應商上次回報的剩餘估計不足以涵蓋任務及排程。")
            if provider is None:
                reasons.append("尚無目前金鑰的供應商餘額觀測，請先核對供應商控制台。")
    else:
        if spec.run_id is None:
            reasons.append("請選擇要匯出的成功批次。")
        else:
            try:
                run, _ = db.report(spec.source, spec.run_id)
                if run["id"] != spec.run_id:
                    reasons.append("報表批次不符。")
            except ReportNotFound:
                reasons.append("找不到此來源的成功批次。")
        if spec.report_scope:
            from gummy.report_center import fixed_report_data

            try:
                _, digest = fixed_report_data(db, spec.report_scope)
                if digest != spec.report_digest:
                    reasons.append("固定資料已變更，請重新查詢與預覽。")
            except ValueError:
                reasons.append("固定資料範圍無法核對。")
    reset = datetime.combine(datetime.now(UTC).date() + timedelta(days=1), datetime.min.time(), UTC)
    payload = {
        "allowed": not reasons,
        "reasons": reasons,
        "planned_requests": planned,
        "queued_reserved": held,
        "future_reserved": future,
        "today_reserved": today_reserved,
        "local_quota": local,
        "provider_quota": provider,
        "daily_reset_at": reset.isoformat(),
        "plan_state": plan_state["status"] if plan_state else None,
    }
    # Bind the confirmation to budget/run/plan state as well as the submitted fields.
    state = {
        "input": spec.model_dump(),
        "policy": config_binding(settings),
        "day": local["day"],
        "used": [local["total_used"], local["daily_used"]],
        "held": held,
        "future": future,
        "today_reserved": today_reserved,
        "provider": provider,
        "pending": [row["id"] for row in pending],
    }
    payload["fingerprint"] = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
    return payload


def enqueue(db, settings, spec, task_id, fingerprint):
    with process_lock(db, "admission"), process_lock(db):
        with db.engine.connect() as conn:
            prior = conn.execute(select(tasks).where(tasks.c.id == task_id)).mappings().first()
        if prior:
            if prior["request"] != spec.model_dump():
                raise RunConflict("Task inputs changed")
            return task_id
        check = admission(db, settings, spec)
        if not check["allowed"]:
            raise ValueError("；".join(check["reasons"]))
        if check["fingerprint"] != fingerprint:
            raise RunConflict("預覽後額度或任務狀態已改變，請重新預覽。")
        with db.engine.begin() as conn:
            conn.execute(
                insert(tasks).values(
                    id=task_id,
                    kind=spec.kind,
                    source=spec.source,
                    request=spec.model_dump(),
                    binding=config_binding(settings),
                    status="QUEUED",
                    phase="queued",
                    reserved_requests=check["planned_requests"],
                    run_id=spec.run_id,
                    cancel_requested=False,
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
            )
            conn.execute(
                insert(task_events).values(
                    task_id=task_id, phase="queued", occurred_at=utc_now(), detail="已確認並排隊"
                )
            )
            if spec.report_scope:
                from gummy.report_center import register_version

                register_version(conn, spec, task_id)
        return task_id


def cancel_task(db, task_id):
    with process_lock(db, "admission"), db.engine.begin() as conn:
        row = conn.execute(select(tasks).where(tasks.c.id == task_id)).mappings().first()
        if row is None:
            raise ReportNotFound("No such task")
        if row["status"] not in ACTIVE:
            return
        queued = row["status"] == "QUEUED"
        conn.execute(
            update(tasks)
            .where(tasks.c.id == task_id)
            .values(
                cancel_requested=True,
                updated_at=utc_now(),
                **(
                    {
                        "status": "CANCELLED",
                        "phase": "cancelled",
                        "reserved_requests": 0,
                        "completed_at": utc_now(),
                    }
                    if queued
                    else {}
                ),
            )
        )
        conn.execute(
            insert(task_events).values(
                task_id=task_id,
                phase="cancel_requested",
                occurred_at=utc_now(),
                detail="使用者要求停止後續工作",
            )
        )


def set_phase(db, task_id, phase, **fields):
    detail = fields.pop("detail", "")
    with db.engine.begin() as conn:
        conn.execute(
            update(tasks)
            .where(tasks.c.id == task_id)
            .values(phase=phase, updated_at=utc_now(), **fields)
        )
        conn.execute(
            insert(task_events).values(
                task_id=task_id, phase=phase, occurred_at=utc_now(), detail=detail[:120]
            )
        )


def build_task_report(db, settings, task):
    from gummy.portable_workbook import export_book

    project = Path(__file__).resolve().parents[1]
    destination = project / "exports" / "operations" / (task["id"] + ".xlsx")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Destination is chosen by the server; callers cannot provide paths or shell commands.
    spec = TaskInput.model_validate(task["request"])
    if spec.report_scope:
        from gummy.report_center import export_fixed_report

        export_fixed_report(db, spec, destination)
    else:
        export_book(db, task["source"], task["run_id"], destination)
    if not report_content_verified(destination, task["source"], task["run_id"]):
        raise ValueError("report_verification_failed")
    record = json.loads(Path(str(destination) + ".verified.json").read_text())
    with db.engine.begin() as conn:
        conn.execute(
            insert(operation_reports).values(
                task_id=task["id"],
                source=task["source"],
                through_run_id=task["run_id"],
                path=str(destination),
                sha256=record["sha256"],
                created_at=utc_now(),
                workbook_version=record["workbook_schema_version"],
            )
        )


def process_next(db, settings, *, transport=None, report_builder=build_task_report):
    from gummy.recurrence import occurrences

    with process_lock(db, "admission"):
        with db.engine.begin() as conn:
            task = (
                conn.execute(
                    select(tasks)
                    .outerjoin(occurrences, occurrences.c.id == tasks.c.id)
                    .where(tasks.c.status == "QUEUED")
                    .order_by(tasks.c.created_at, occurrences.c.rule_id, tasks.c.id)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if task is None:
                return None
            task = dict(task)
            conn.execute(
                update(tasks)
                .where(tasks.c.id == task["id"])
                .values(status="RUNNING", started_at=utc_now(), updated_at=utc_now())
            )
    task_id = task["id"]
    heartbeat(db, "running", task_id)
    try:
        if task["kind"] == "schedule":
            from gummy.recurrence import process_schedule

            return process_schedule(db, settings, task, transport)
        if task["kind"] == "schedule-report":
            from gummy.recurrence_report import build_report

            build_report(db, settings, task["request"]["occurrence_id"], task_id)
            set_phase(
                db,
                task_id,
                "done",
                status="SUCCEEDED",
                completed_at=utc_now(),
                reserved_requests=0,
                detail="已存資料重產與讀回核對通過",
            )
            return get_task(db, task_id)
        spec = TaskInput.model_validate(task["request"])
        if task["kind"] == "collect":
            if task["binding"] != config_binding(settings):
                raise ValueError("configuration_changed")
            check = admission(db, settings, spec, ignore_task=task_id, require_worker=False)
            if not check["allowed"]:
                raise ValueError("preflight_changed")

            def progress(phase, **info):
                if get_task(db, task_id)["cancel_requested"]:
                    raise TaskCancelled("cancelled")
                set_phase(
                    db,
                    task_id,
                    phase,
                    run_id=info["run_id"],
                    detail=f"已處理 {info.get('completed', 0)} / {info.get('total', 0)} 件",
                )
                heartbeat(db, phase, task_id)

            result = collect(
                db,
                settings,
                CollectRequest(
                    source=spec.source,
                    run_key="ui-" + task_id,
                    query=spec.query,
                    category=spec.category,
                    max_products=spec.max_products,
                ),
                transport=transport,
                progress=progress,
            )
            set_phase(db, task_id, "collected", run_id=result["run_id"], reserved_requests=0)
        task = get_task(db, task_id)
        if task["cancel_requested"]:
            raise TaskCancelled("cancelled")
        set_phase(db, task_id, "report", reserved_requests=0)
        report_builder(db, settings, task)
        set_phase(
            db,
            task_id,
            "done",
            status="SUCCEEDED",
            completed_at=utc_now(),
            reserved_requests=0,
            detail="報表產生與讀回驗證通過",
        )
    except Exception as exc:
        current = get_task(db, task_id)
        status = "CANCELLED" if isinstance(exc, TaskCancelled) else "FAILED"
        if current["run_id"] is not None and status != "CANCELLED":
            try:
                db.report(current["source"], current["run_id"])
                status = "DATA_READY"
            except ReportNotFound:
                pass
        code = (
            str(exc)
            if str(exc)
            in {"configuration_changed", "preflight_changed", "report_verification_failed"}
            else type(exc).__name__
        )
        set_phase(
            db,
            task_id,
            "stopped",
            status=status,
            error_code=code,
            completed_at=utc_now(),
            reserved_requests=0,
            detail="已停止，未自動重試",
        )
    finally:
        heartbeat(db)
    return get_task(db, task_id)


def run_task_worker(db, settings, *, once=False):
    if settings.app_env != "local":
        raise ValueError("Task UI worker is local-only")
    with process_lock(db, "task-worker"):
        # On restart, never replay a task that may already have made a billable request.
        with db.engine.connect() as conn:
            interrupted = [
                dict(row)
                for row in conn.execute(select(tasks).where(tasks.c.status == "RUNNING")).mappings()
            ]
        for task in interrupted:
            status = "INTERRUPTED"
            if task["kind"] == "schedule":
                from gummy.recurrence import recover_results

                if recover_results(db, task["id"]):
                    status = "DATA_READY"
            if task["run_id"] is not None:
                try:
                    db.report(task["source"], task["run_id"])
                    status = "DATA_READY"
                except ReportNotFound:
                    pass
            set_phase(
                db,
                task["id"],
                "stopped",
                status=status,
                error_code="worker_restarted",
                completed_at=utc_now(),
                reserved_requests=0,
            )
        heartbeat(db)
        stop = threading.Event()

        def pulse():
            while not stop.wait(5):
                with db.engine.begin() as conn:
                    conn.execute(
                        update(worker_state)
                        .where(worker_state.c.name == "local")
                        .values(last_seen_at=utc_now())
                    )

        thread = threading.Thread(target=pulse, daemon=True)
        thread.start()
        try:
            while True:
                try:
                    process_next(db, settings)
                except RunConflict:
                    # The UI may briefly hold admission. No task has started I/O here.
                    pass
                if once:
                    return
                time.sleep(2)
        finally:
            stop.set()
            thread.join(timeout=6)


def task_view(db, task_id):
    row = get_task(db, task_id)
    public = {
        k: row[k]
        for k in (
            "id",
            "kind",
            "source",
            "request",
            "status",
            "phase",
            "reserved_requests",
            "run_id",
            "cancel_requested",
            "created_at",
            "updated_at",
            "started_at",
            "completed_at",
            "error_code",
        )
    }
    with db.engine.connect() as conn:
        public["events"] = [
            dict(item)
            for item in conn.execute(
                select(task_events)
                .where(task_events.c.task_id == task_id)
                .order_by(task_events.c.id)
            ).mappings()
        ]
        public["actual_requests"] = (
            conn.execute(
                select(func.count()).select_from(attempts).where(attempts.c.run_id == row["run_id"])
            ).scalar()
            if row["kind"] == "collect"
            else 0
        )
        if row["kind"] == "schedule":
            batch_ids = select(runs.c.id).where(runs.c.run_key.like("rec-" + task_id + "-%"))
            public["actual_requests"] = conn.execute(
                select(func.count()).select_from(attempts).where(attempts.c.run_id.in_(batch_ids))
            ).scalar()
        report = (
            conn.execute(select(operation_reports).where(operation_reports.c.task_id == task_id))
            .mappings()
            .first()
        )
        public["report"] = (
            {
                "verified": report_content_verified(
                    Path(report["path"]), row["source"], row["run_id"]
                ),
                "workbook_version": report["workbook_version"],
            }
            if report
            else None
        )
    return public


def schedule_alerts(plan, now):
    """Calendar dates alone do not prove execution. Allow a 15 minute startup grace."""
    alerts = []

    def overdue(batch, field):
        if batch["date"] < now.date().isoformat():
            return True
        if batch["date"] != now.date().isoformat() or not plan.get(field):
            return False
        try:
            hour, minute = map(int, plan[field].split(":"))
            return now > now.replace(hour=hour, minute=minute, second=0, microsecond=0) + (
                timedelta(minutes=15)
            )
        except ValueError:
            return False

    for batch in plan.get("batches", []):
        if not (plan.get("scheduler") or {}).get("enabled", True) and batch["status"] == "PENDING":
            continue
        report = batch.get("report")
        if batch["status"] == "MISSED":
            alerts.append(f"{batch['date']} 的排程未執行；不會自動補抓歷史日期。")
        elif batch["status"] == "PENDING" and overdue(batch, "scheduled_time_local"):
            alerts.append(f"{batch['date']} 已超過預定採集時間 15 分鐘，仍無當日批次。")
        elif batch["status"] == "SUCCEEDED" and (not report or not report["exists"]):
            if overdue(batch, "report_scheduled_time_local"):
                alerts.append(f"批次 #{batch['run_id']} 已取得資料，但每日報表尚未產出。")
        elif report and report["exists"] and not report["content_verified"]:
            alerts.append(f"批次 #{batch['run_id']} 的報表尚未通過內容驗證。")
    return alerts


def operations_overview(db, settings, source="rapidapi"):
    now = datetime.now(UTC)
    quota = db.quota_status(settings.total_limit, settings.daily_limit)
    plan = active_plan_overview(settings, db)
    health = worker_health(db)
    with db.engine.connect() as conn:
        recent = [
            dict(row)
            for row in conn.execute(
                select(runs).where(runs.c.source == source).order_by(runs.c.id.desc()).limit(15)
            ).mappings()
        ]
        ids = [
            row[0]
            for row in conn.execute(
                select(tasks.c.id)
                .where(tasks.c.source == source)
                .order_by(tasks.c.created_at.desc())
                .limit(20)
            )
        ]
        totals = conn.execute(
            select(func.count(), func.count(func.distinct(products.c.asin)))
            .select_from(products.join(runs))
            .where(runs.c.source == source, runs.c.status == "SUCCEEDED")
        )
        observations, distinct = totals.one()
        evidence = conn.execute(
            select(func.count())
            .select_from(source_observations.join(runs))
            .where(runs.c.source == source, runs.c.status == "SUCCEEDED")
        ).scalar()
    task_list = [task_view(db, tid) for tid in ids]
    today = local_now().date().isoformat()
    today_runs = [
        row
        for row in recent
        if row["completed_at"]
        and datetime.fromisoformat(row["completed_at"])
        .astimezone(local_now().tzinfo)
        .date()
        .isoformat()
        == today
    ]
    alerts = []
    if not health["online"]:
        alerts.append("背景任務執行器無近期回報，立即採集暫不可用。")
    if source == "rapidapi" and quota["daily_used"] >= quota["daily_limit"]:
        alerts.append("目前 UTC 帳本日次數已用完，下一次台灣時間 08:00 重置。")
    alerts.extend(schedule_alerts(plan, local_now()))
    for key, job in (plan.get("scheduler") or {}).get("jobs", {}).items():
        if job.get("status") in {"FAILED", "INTERRUPTED", "BLOCKED", "MISSED"}:
            date, kind = key.split(":", 1)
            label = "採集" if kind == "collect" else "報表"
            alerts.append(f"{date} 的 Python {label}排程需處理；不會自動重試採集。")
    if plan["status"] not in {"READY", "FINISHED_OR_EXPIRED", "NOT_CONFIGURED"}:
        alerts.append("採集計畫需要處理，請查看下方排程紀錄。")
    for name in ("schedule_status", "report_schedule_status"):
        if name in plan and plan[name] != "LOADED_AND_MATCHED":
            alerts.append("採集或報表排程尚未載入、設定不符，或無法確認。")
            break
    if any(t["status"] in {"FAILED", "DATA_READY", "INTERRUPTED"} for t in task_list[:5]):
        alerts.append("最近任務有待處理結果，可從任務明細查看原因。")
    return {
        "source": source,
        "checked_at": now.isoformat(),
        "worker": health,
        "quota": quota,
        "provider": db.provider_quota_status(settings.rapidapi_key),
        "plan": plan,
        "tasks": task_list,
        "runs": recent,
        "alerts": alerts,
        "today_runs": today_runs,
        "observations": observations,
        "distinct_asins": distinct,
        "evidence_count": evidence,
    }
