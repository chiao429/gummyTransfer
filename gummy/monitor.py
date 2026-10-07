"""Read-only local status for a finite collection plan and its LaunchAgent."""

import hashlib
import json
import os
import plistlib
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

from gummy.errors import ReportNotFound
from gummy.plan import load_plan, plan_status
from gummy.plan_report import report_path
from gummy.review_status import read_review_progress
from gummy.scheduled import local_now


def launch_agent_loaded(plan_id: str) -> bool | None:
    if sys.platform != "darwin":
        return None
    label = f"com.woobe.gummy.plan.{plan_id}"
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.returncode == 0


def installed_schedule(
    plan_id: str, plan_path: Path, digest: str, agent_dir: Path | None = None
) -> dict:
    agent_dir = agent_dir or Path.home() / "Library" / "LaunchAgents"
    path = agent_dir / f"com.woobe.gummy.plan.{plan_id}.plist"
    if not path.is_file():
        return {"installed": False, "matches_plan": False, "time_local": None}
    try:
        with path.open("rb") as handle:
            manifest = plistlib.load(handle)
        args = manifest["ProgramArguments"]
        planned_path = Path(args[args.index("--plan") + 1]).resolve()
        planned_hash = args[args.index("--expected-sha256") + 1]
        interval = manifest["StartCalendarInterval"]
        time_local = f"{interval['Hour']:02d}:{interval['Minute']:02d}"
        matches = (
            manifest["Label"] == f"com.woobe.gummy.plan.{plan_id}"
            and len(args) == 8
            and args[1:5] == ["-m", "gummy", "run-plan", "--plan"]
            and args[6] == "--expected-sha256"
            and planned_path == plan_path.resolve()
            and planned_hash == digest
            and 0 <= interval["Hour"] <= 23
            and 0 <= interval["Minute"] <= 59
            and manifest["RunAtLoad"] is False
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
        return {"installed": True, "matches_plan": False, "time_local": None}
    return {"installed": True, "matches_plan": matches, "time_local": time_local}


def report_launch_agent_loaded(plan_id: str) -> bool | None:
    if sys.platform != "darwin":
        return None
    label = f"com.woobe.gummy.report.{plan_id}"
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.returncode == 0


def installed_report_schedule(
    plan_id: str, plan_path: Path, digest: str, agent_dir: Path | None = None
) -> dict:
    agent_dir = agent_dir or Path.home() / "Library" / "LaunchAgents"
    path = agent_dir / f"com.woobe.gummy.report.{plan_id}.plist"
    missing = {
        "installed": False,
        "matches_plan": False,
        "time_local": None,
        "output_dir": None,
        "initial_review_workbook": None,
    }
    if not path.is_file():
        return missing
    try:
        with path.open("rb") as handle:
            manifest = plistlib.load(handle)
        args = manifest["ProgramArguments"]
        planned_path = Path(args[args.index("--plan") + 1]).resolve()
        planned_hash = args[args.index("--expected-sha256") + 1]
        output_dir = Path(args[args.index("--output-dir") + 1]).resolve()
        interval = manifest["StartCalendarInterval"]
        time_local = f"{interval['Hour']:02d}:{interval['Minute']:02d}"
        has_review = len(args) == 14
        matches = (
            manifest["Label"] == f"com.woobe.gummy.report.{plan_id}"
            and len(args) in (12, 14)
            and args[1:5] == ["-m", "gummy", "plan-report", "--plan"]
            and args[6] == "--expected-sha256"
            and args[8] == "--output-dir"
            and Path(args[9]).is_absolute()
            and args[10] == "--wait-seconds"
            and 0 <= int(args[11]) <= 600
            and (
                not has_review
                or (
                    args[12] == "--initial-review-workbook"
                    and Path(args[13]).is_absolute()
                    and Path(args[13]).is_file()
                )
            )
            and planned_path == plan_path.resolve()
            and planned_hash == digest
            and 0 <= interval["Hour"] <= 23
            and 0 <= interval["Minute"] <= 59
            and manifest["RunAtLoad"] is False
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
        return {**missing, "installed": True}
    return {
        "installed": True,
        "matches_plan": matches,
        "time_local": time_local,
        "output_dir": str(output_dir),
        "initial_review_workbook": str(Path(args[13]).resolve()) if has_review else None,
    }


def schedule_status(loaded: bool | None, installed: dict) -> str:
    if loaded is True and installed["matches_plan"]:
        return "LOADED_AND_MATCHED"
    if loaded is None:
        return "UNKNOWN"
    if loaded is True or installed["installed"]:
        return "CONFIG_MISMATCH" if not installed["matches_plan"] else "NOT_LOADED"
    return "NOT_LOADED"


def report_content_verified(path: Path, source: str, run_id: int) -> bool:
    """Check the export's read-back record and detect changes since verification."""
    record_path = Path(str(path) + ".verified.json")
    try:
        if not path.is_file() or record_path.stat().st_size > 4096:
            return False
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if (
            record.get("schema_version") != 1
            or record.get("source") != source
            or record.get("through_run_id") != run_id
            or record.get("verification_status") != "PASS"
        ):
            return False
        with path.open("rb") as workbook:
            digest = hashlib.file_digest(workbook, "sha256").hexdigest()
        return digest == record.get("sha256")
    except (OSError, ValueError, UnicodeError, AttributeError):
        return False


def active_plan_overview(settings, db, now: datetime | None = None) -> dict:
    if not settings.active_plan_path:
        return {"status": "NOT_CONFIGURED"}
    try:
        path = Path(settings.active_plan_path)
        plan, digest = load_plan(path)
    except (OSError, ValueError):
        return {"status": "PLAN_UNAVAILABLE"}
    today = (now or local_now()).date()
    if settings.scheduler_config_path:
        from gummy.schedule_management import effective_schedule

        try:
            upcoming_cfg, plan, _ = effective_schedule(
                db, settings.scheduler_config_path, upcoming=True
            )
        except (OSError, ValueError):
            return {"status": "PLAN_UNAVAILABLE"}
    state = plan_status(plan, db, settings, today)
    loaded = launch_agent_loaded(plan.plan_id)
    installed = installed_schedule(plan.plan_id, path, digest)
    report_loaded = report_launch_agent_loaded(plan.plan_id)
    report_installed = installed_report_schedule(plan.plan_id, path, digest)
    python_state = None
    if settings.scheduler_config_path:
        from gummy.python_scheduler import scheduler_health

        python_state = scheduler_health(settings.scheduler_config_path, db=db)
        matching = (
            python_state.get("plan_path") == str(path.resolve())
            and python_state.get("base_plan_sha256") == digest
        )
        if not matching:
            python_state["status"] = "CONFIG_MISMATCH"
            python_state["online"] = False
        installed = {
            "installed": True,
            "matches_plan": matching,
            "time_local": upcoming_cfg.collect_at,
        }
        report_installed = {
            "installed": True,
            "matches_plan": matching,
            "time_local": upcoming_cfg.report_at,
            "output_dir": python_state.get("output_dir"),
            "initial_review_workbook": python_state.get("initial_review_workbook"),
        }
        loaded = report_loaded = python_state.get("online", False)
    latest_report = None
    monitored_batches = []
    if report_installed["output_dir"]:
        for batch in state["batches"]:
            report = None
            if batch["status"] == "SUCCEEDED":
                candidate = report_path(
                    Path(report_installed["output_dir"]),
                    plan.plan_id,
                    date.fromisoformat(batch["date"]),
                    batch["run_id"],
                )
                report = {
                    "path": str(candidate),
                    "exists": candidate.is_file(),
                    "content_verified": report_content_verified(
                        candidate, plan.source, batch["run_id"]
                    ),
                }
                latest_report = report
            monitored_batches.append({**batch, "report": report})
    else:
        monitored_batches = [{**batch, "report": None} for batch in state["batches"]]
    latest_review_progress = None
    review_progress_source = None
    if latest_report is not None and latest_report["exists"]:
        latest_run_id = next(
            batch["run_id"] for batch in reversed(monitored_batches) if batch["report"] is not None
        )
        review_path = Path(latest_report["path"])
        review_progress_source = "latest_daily_report"
    elif latest_report is None and report_installed.get("initial_review_workbook"):
        try:
            latest_run_id = db.report(plan.source)[0]["id"]
        except ReportNotFound:
            latest_run_id = None
        review_path = Path(report_installed["initial_review_workbook"])
        review_progress_source = "initial_review_workbook"
    else:
        latest_run_id = None
    if latest_run_id is not None:
        expected_keys = {
            (run_id, row["asin"])
            for run_id in db.successful_run_ids(plan.source, latest_run_id)
            for row in db.report(plan.source, run_id)[1]
        }
        latest_review_progress = read_review_progress(review_path, expected_keys)
    result = {
        **state,
        "batches": monitored_batches,
        "schedule_status": python_state["status"]
        if python_state
        else schedule_status(loaded, installed),
        "launch_agent_loaded": None if python_state else loaded,
        "installed_plist_matches_plan": None if python_state else installed["matches_plan"],
        "scheduled_time_local": installed["time_local"],
        "report_schedule_status": python_state["status"]
        if python_state
        else schedule_status(report_loaded, report_installed),
        "report_scheduled_time_local": report_installed["time_local"],
        "latest_report": latest_report,
        "latest_review_progress": latest_review_progress,
        "review_progress_source": review_progress_source,
        "sha256": digest,
        "scheduler_backend": "python" if python_state else "launchagent",
        "scheduler": python_state,
    }
    from gummy.recurrence import active, overview

    if active(db):
        result["recurrence"] = overview(db, settings, now)
        result["legacy_batches"] = monitored_batches
        result["batches"] = [b for b in monitored_batches if b["status"] != "PENDING"]
        result["status"] = "READY"
        result["scheduled_time_local"] = None
        result["report_scheduled_time_local"] = None
        result["scheduler"]["enabled"] = any(
            r["spec"]["state"] == "enabled" for r in result["recurrence"]["rules"]
        )
    return result
