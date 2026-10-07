"""A finite, persistent Python scheduler independent of Codex and calendar agents."""

import hashlib
import json
import os
import re
import signal
import tempfile
import threading
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gummy.errors import RunConflict
from gummy.locking import process_lock
from gummy.plan import load_plan, plan_status
from gummy.plan_report import export_plan_report, report_path, verify_previous_plan_workbook
from gummy.verification import verify_snapshot
from gummy.worker import collect


class SchedulerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    plan: str
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    timezone: Literal["Asia/Taipei"] = "Asia/Taipei"
    collect_at: str = Field(default="10:00", pattern=r"^\d{2}:\d{2}$")
    report_at: str = Field(default="10:10", pattern=r"^\d{2}:\d{2}$")
    grace_minutes: int = Field(default=15, ge=1, le=60)
    poll_seconds: int = Field(default=10, ge=1, le=30)
    output_dir: str = "exports/daily"
    initial_review_workbook: str | None = None
    state_file: str = "data/python-scheduler-state.json"

    @model_validator(mode="after")
    def valid_times(self):
        start, report = time.fromisoformat(self.collect_at), time.fromisoformat(self.report_at)
        if start < time(9) or report <= start:
            raise ValueError("Collection must be at/after 09:00; report must be later that day")
        return self


def load_scheduler(path):
    path = Path(path).resolve()
    raw = path.read_bytes()
    if len(raw) > 8192:
        raise ValueError("Scheduler configuration is too large")
    cfg = SchedulerConfig.model_validate_json(raw)
    for field in ("plan", "output_dir", "initial_review_workbook", "state_file"):
        value = getattr(cfg, field)
        if value is not None:
            if Path(value).is_absolute():
                raise ValueError("Scheduler paths must be relative to its configuration folder")
            resolved = (path.parent / value).resolve()
            if not resolved.is_relative_to(path.parent):
                raise ValueError("Scheduler paths must stay inside the project")
            setattr(cfg, field, str(resolved))
    plan, digest = load_plan(cfg.plan)
    if Path(cfg.output_dir).is_file():
        raise ValueError("Report output must be a folder")
    if cfg.initial_review_workbook and not Path(cfg.initial_review_workbook).is_file():
        raise ValueError("Initial review workbook is missing")
    if digest != cfg.expected_sha256:
        raise ValueError("Plan changed; validate and approve the new plan before scheduling")
    if plan.source == "rapidapi" and (plan.batches[-1].date - plan.batches[0].date).days > 30:
        raise ValueError("Live scheduler requires a finite pilot within 30 days")
    return cfg, plan, hashlib.sha256(raw).hexdigest()


def read_state(path):
    path = Path(path)
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError("Scheduler state is unsafe or too large")
    state = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(state, dict)
        or state.get("schema_version") != 1
        or not isinstance(state.get("jobs"), dict)
    ):
        raise ValueError("Scheduler state is invalid; inspect it before recovery")
    if not isinstance(state.get("plan_id"), str) or not isinstance(state.get("plan_sha256"), str):
        raise ValueError("Scheduler state has no valid plan identity")
    if len(state["jobs"]) > 60 or any(
        not isinstance(key, str)
        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}:(collect|report)", key)
        or not isinstance(job, dict)
        or not isinstance(job.get("status"), str)
        for key, job in state["jobs"].items()
    ):
        raise ValueError("Scheduler job ledger is invalid")
    return state


def save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Scheduler state cannot be a symbolic link")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", delete=False
        ) as handle:
            temporary = Path(handle.name)
            temporary.chmod(0o600)
            json.dump(state, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def scheduler_health(path, now=None, db=None):
    """Heartbeat proves a running Python process, not successful collection."""
    try:
        cfg, plan, digest = load_scheduler(path)
        schedule_meta = None
        if db is not None:
            from gummy.schedule_management import effective_schedule

            cfg, plan, schedule_meta = effective_schedule(
                db, path, day=(now or datetime.now(UTC)).astimezone(ZoneInfo(cfg.timezone)).date()
            )
        plan_sha = schedule_meta["plan_sha256"] if schedule_meta else cfg.expected_sha256
        state = read_state(cfg.state_file)
        if state is None:
            status = "NOT_RUNNING"
        elif (
            state.get("config_sha256") != digest
            or state.get("plan_sha256") != plan_sha
            or state.get("plan_id") != plan.plan_id
        ):
            status = "CONFIG_MISMATCH"
        else:
            age = (
                (now or datetime.now(UTC)) - datetime.fromisoformat(state["last_seen_at"])
            ).total_seconds()
            status = (
                "LOADED_AND_MATCHED"
                if (
                    state.get("phase") not in {"stopped", "failed"}
                    and 0 <= age <= 3 * cfg.poll_seconds + 10
                )
                else "NOT_RUNNING"
            )
        return {
            "status": status,
            "backend": "python",
            "online": status == "LOADED_AND_MATCHED",
            "plan_id": plan.plan_id,
            "plan_path": cfg.plan,
            "plan_sha256": plan_sha,
            "base_plan_sha256": cfg.expected_sha256,
            "enabled": schedule_meta["enabled"] if schedule_meta else True,
            "revision": schedule_meta["revision"] if schedule_meta else 0,
            "time_revision": schedule_meta["time_revision"] if schedule_meta else 0,
            "latest_revision": schedule_meta["latest_revision"] if schedule_meta else 0,
            "effective_from": schedule_meta["effective_from"] if schedule_meta else None,
            "collect_at": cfg.collect_at,
            "report_at": cfg.report_at,
            "timezone": cfg.timezone,
            "output_dir": cfg.output_dir,
            "initial_review_workbook": cfg.initial_review_workbook,
            "last_seen_at": state.get("last_seen_at") if state else None,
            "phase": state.get("phase") if state else "unknown",
            "jobs": state.get("jobs", {}) if state else {},
        }
    except (OSError, ValueError, TypeError, KeyError):
        return {"status": "CONFIG_MISMATCH", "backend": "python", "online": False}


class PythonScheduler:
    def __init__(self, config_path, db, settings):
        self.config_path = Path(config_path).resolve()
        self.cfg, self.plan, self.digest = load_scheduler(self.config_path)
        from gummy.schedule_management import effective_schedule

        self.cfg, self.plan, schedule_meta = effective_schedule(db, self.config_path)
        self.db, self.settings = db, settings
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.heartbeat_error = None
        self.state = read_state(self.cfg.state_file) or {
            "schema_version": 1,
            "jobs": {},
            "plan_id": self.plan.plan_id,
            "plan_sha256": schedule_meta["plan_sha256"],
            "schedule_revision": schedule_meta["revision"],
        }
        from gummy.schedule_management import digest as schedule_digest

        valid_previous = {0: self.cfg.expected_sha256}
        valid_previous.update(
            {r["id"]: schedule_digest(r["plan"]) for r in schedule_meta["history"]}
        )
        if self.state["plan_id"] != self.plan.plan_id or self.state[
            "plan_sha256"
        ] != valid_previous.get(self.state.get("schedule_revision", 0)):
            raise ValueError("Scheduler state belongs to a different plan")
        # RUNNING is uncertain after a crash; do not automatically retry billable jobs.
        for job in self.state["jobs"].values():
            if job["status"] == "RUNNING":
                job["status"] = "INTERRUPTED"
        self.state.update(config_sha256=self.digest, phase="starting", pid=os.getpid())
        self.state.update(
            plan_sha256=schedule_meta["plan_sha256"], schedule_revision=schedule_meta["revision"]
        )

    def persist(self, now=None):
        with self.lock:
            self.state["last_seen_at"] = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
            save_state(self.cfg.state_file, self.state)

    def _heartbeat(self):
        while not self.stop.wait(self.cfg.poll_seconds):
            try:
                self.persist()
            except Exception as exc:
                self.heartbeat_error = type(exc).__name__
                self.stop.set()

    def _collect(self, now):
        request = self.plan.request_for(now.date())
        previous = self.db.run_by_key(self.plan.source, request.run_key)
        if previous:
            if previous["status"] != "SUCCEEDED" or previous["parameters"] != request.model_dump(
                exclude={"source", "run_key"}
            ):
                raise ValueError("Existing batch is failed or conflicts with this plan")
            run, rows = self.db.report(self.plan.source, previous["id"])
            if verify_snapshot(run, rows, self.db.evidence(run["id"]))["status"] != "PASS":
                raise ValueError("Existing batch source evidence failed verification")
            return {"status": "ALREADY_COLLECTED", "run_id": run["id"]}
        due = datetime.combine(now.date(), time.fromisoformat(self.cfg.collect_at), now.tzinfo)
        if now >= due + timedelta(minutes=self.cfg.grace_minutes):
            return {"status": "MISSED", "reason": "collection_window_closed"}
        if plan_status(self.plan, self.db, self.settings, now.date())["status"] != "READY":
            raise ValueError("Plan needs attention before collection")
        if self.plan.source == "rapidapi":
            verify_previous_plan_workbook(
                self.plan,
                self.db,
                self.settings,
                now.date(),
                Path(self.cfg.output_dir),
                engine="python",
            )
            self.settings.validate_live()
            self.db.preflight_quota(
                1 + request.max_products, self.settings.total_limit, self.settings.daily_limit
            )
        return collect(self.db, self.settings, request)

    def _report(self, now):
        request = self.plan.request_for(now.date())
        run = self.db.run_by_key(self.plan.source, request.run_key)
        if run is None or run["status"] != "SUCCEEDED":
            return {"status": "BLOCKED", "reason": "no_successful_batch"}
        if run["parameters"] != request.model_dump(exclude={"source", "run_key"}):
            raise ValueError("Report batch differs from plan")
        snapshot, rows = self.db.report(self.plan.source, run["id"])
        if verify_snapshot(snapshot, rows, self.db.evidence(run["id"]))["status"] != "PASS":
            raise ValueError("Report batch source evidence failed verification")
        destination = report_path(
            Path(self.cfg.output_dir), self.plan.plan_id, now.date(), run["id"]
        )
        if destination.exists():
            from gummy.portable_workbook import verify_from_db

            verify_from_db(self.db, self.plan.source, run["id"], destination)
            return {"status": "REPORT_PRESENT", "run_id": run["id"], "output": str(destination)}
        return export_plan_report(
            self.plan,
            self.db,
            self.settings,
            now.date(),
            Path(self.cfg.output_dir),
            Path(self.cfg.initial_review_workbook) if self.cfg.initial_review_workbook else None,
            engine="python",
        )

    def tick(self, now=None):
        with process_lock(self.db, "schedule-control"):
            return self._tick(now)

    def _tick(self, now=None):
        now = (now or datetime.now(UTC)).astimezone(ZoneInfo(self.cfg.timezone))
        # Pin both files throughout the process; edits require a controlled restart.
        _, plan, digest = load_scheduler(self.config_path)
        if digest != self.digest or plan.plan_id != self.plan.plan_id:
            raise ValueError("Scheduler configuration changed; restart after validation")
        from gummy.schedule_management import effective_schedule

        self.cfg, self.plan, schedule_meta = effective_schedule(
            self.db, self.config_path, day=now.date()
        )
        with self.lock:
            self.state.update(
                plan_sha256=schedule_meta["plan_sha256"],
                schedule_revision=schedule_meta["revision"],
            )
        from gummy.recurrence import active, dispatch

        if active(self.db):
            try:
                emitted = dispatch(self.db, self.settings, now)
            except RunConflict:
                emitted = []  # A worker or form holds a lock; retry within the grace window.
            with self.lock:
                self.state["phase"] = "waiting"
            self.persist(now)
            return [{"task_id": tid, "status": "DISPATCHED"} for tid in emitted]
        if not schedule_meta["enabled"]:
            with self.lock:
                self.state["phase"] = "paused"
            self.persist(now)
            return []
        results = []
        if self.plan.request_for(now.date()) is None:
            with self.lock:
                self.state["phase"] = (
                    "finished" if now.date() > self.plan.batches[-1].date else "waiting"
                )
            self.persist(now)
            return results
        for kind, due in [("collect", self.cfg.collect_at), ("report", self.cfg.report_at)]:
            key = f"{now.date().isoformat()}:{kind}"
            with self.lock:
                if key in self.state["jobs"] or now.time().replace(
                    tzinfo=None
                ) < time.fromisoformat(due):
                    continue
                self.state["jobs"][key] = {"status": "RUNNING", "started_at": now.isoformat()}
                self.state["phase"] = kind
                self.persist(now)  # Save intent before a possibly billable operation.
            try:
                result = self._collect(now) if kind == "collect" else self._report(now)
            except Exception as exc:
                # Exceptions may contain URLs/headers; log only the error type.
                result = {"status": "FAILED", "error_code": type(exc).__name__}
            with self.lock:
                self.state["jobs"][key].update(result, completed_at=datetime.now(UTC).isoformat())
                self.state["phase"] = "waiting"
                self.persist()
            event = {"day": now.date().isoformat(), "job": kind, **result}
            print(json.dumps(event, ensure_ascii=False), flush=True)
            results.append(event)
        self.persist()
        with self.lock:
            self.state["phase"] = "waiting"
        self.persist()
        return results

    def run(self, once=False):
        if self.settings.app_env != "local" or self.db.engine.dialect.name != "sqlite":
            raise ValueError("Portable scheduler currently supports local SQLite deployments")
        if self.plan.source == "rapidapi":
            self.settings.validate_live()
        with process_lock(self.db, "scheduler"):
            self.persist()
            previous_handlers = {}
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGINT, signal.SIGTERM):
                    previous_handlers[signum] = signal.signal(signum, lambda *_: self.stop.set())
            watcher = threading.Thread(target=self._heartbeat, daemon=True)
            watcher.start()
            try:
                while not self.stop.is_set():
                    self.tick()
                    if once:
                        break
                    self.stop.wait(self.cfg.poll_seconds)
                if self.heartbeat_error:
                    raise ValueError("Scheduler heartbeat could not be saved")
            except Exception:
                with self.lock:
                    self.state["phase"] = "failed"
                raise
            finally:
                self.stop.set()
                watcher.join(timeout=self.cfg.poll_seconds + 1)
                with self.lock:
                    if self.state["phase"] != "failed":
                        self.state["phase"] = "stopped"
                    self.persist()
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
