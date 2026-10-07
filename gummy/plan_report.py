"""Build a local workbook only after a planned snapshot passes source verification."""

import os
import subprocess
import time
from datetime import date
from pathlib import Path

from gummy.verification import verify_snapshot


def report_path(output_dir: Path, plan_id: str, day: date, run_id: int) -> Path:
    return output_dir / f"{plan_id}-{day:%Y%m%d}-run{run_id}.xlsx"


def verify_portfolio_workbook(
    settings, source: str, run_id: int, workbook: Path, *, db=None, engine="python"
) -> None:
    """Check an edited XLSX against persisted rows without contacting the provider."""
    if settings.app_env != "local":
        raise ValueError("Workbook verification currently supports local mode only")
    if not workbook.is_file():
        raise ValueError("Previous daily report is missing")
    if engine == "python":
        from gummy.portable_workbook import verify_from_db

        if db is None:
            raise ValueError("Python report verification requires the existing database")
        try:
            verify_from_db(db, source, run_id, workbook)
        except (ValueError, KeyError, TypeError):
            raise ValueError("Previous daily report differs from stored snapshots") from None
        return
    if engine != "artifact":
        raise ValueError("Unknown report engine")
    project = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["DATABASE_URL"] = settings.database_url
    environment["RAPIDAPI_KEY"] = ""
    environment["RAPIDAPI_FREE_PLAN_CONFIRMED"] = "false"
    environment.setdefault(
        "GUMMY_ARTIFACT_NODE_DIR",
        str(Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node"),
    )
    try:
        check = subprocess.run(
            [
                "sh",
                str(project / "scripts/verify_portfolio_local.sh"),
                source,
                str(run_id),
                str(workbook.resolve()),
            ],
            cwd=project,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        raise ValueError("Previous daily report verification timed out") from None
    if check.returncode != 0:
        raise ValueError("Previous daily report differs from stored snapshots")


def verify_previous_plan_workbook(
    plan, db, settings, day: date, output_dir: Path, *, engine="python"
) -> None:
    """Fail before a new live collection if yesterday's report cannot be carried."""
    previous = [
        batch for batch in plan.batches if batch.date < day and batch.date not in plan.skipped_dates
    ]
    if not previous:
        return
    prior_day = previous[-1].date
    request = plan.request_for(prior_day)
    run = db.run_by_key(plan.source, request.run_key)
    if (
        run is None
        or run["status"] != "SUCCEEDED"
        or run["parameters"] != request.model_dump(exclude={"source", "run_key"})
    ):
        raise ValueError("Previous planned batch is missing or differs from the plan")
    workbook = report_path(Path(output_dir).resolve(), plan.plan_id, prior_day, run["id"])
    verify_portfolio_workbook(settings, plan.source, run["id"], workbook, db=db, engine=engine)


def wait_for_planned_run(plan, db, day: date, wait_seconds: int) -> dict | None:
    request = plan.request_for(day)
    if request is None:
        return None
    deadline = time.monotonic() + wait_seconds
    while True:
        run = db.run_by_key(plan.source, request.run_key)
        if run is not None and run["status"] != "RUNNING":
            return run
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return run
        time.sleep(min(2, remaining))


def export_plan_report(
    plan,
    db,
    settings,
    day: date,
    output_dir: Path,
    initial_review: Path | None = None,
    dry_run: bool = False,
    wait_seconds: int = 0,
    engine: str = "python",
) -> dict:
    if settings.app_env != "local":
        raise ValueError("Scheduled XLSX export currently supports local mode only")
    if not 0 <= wait_seconds <= 600:
        raise ValueError("Report wait must be between 0 and 600 seconds")
    request = plan.request_for(day)
    if request is None:
        return {"status": "NO_PLAN_FOR_DATE", "day": day.isoformat()}

    run = (
        db.run_by_key(plan.source, request.run_key)
        if dry_run
        else wait_for_planned_run(plan, db, day, wait_seconds)
    )
    if run is None:
        if dry_run:
            return {"status": "WAITING_FOR_BATCH", "day": day.isoformat()}
        raise ValueError("Today's planned batch has not succeeded")
    if run["status"] == "RUNNING":
        raise ValueError("Today's planned batch is still running")
    if run["status"] != "SUCCEEDED" or run["parameters"] != request.model_dump(
        exclude={"source", "run_key"}
    ):
        raise ValueError("Today's planned batch failed or differs from the plan")
    snapshot, rows = db.report(plan.source, run["id"])
    if verify_snapshot(snapshot, rows, db.evidence(run["id"]))["status"] != "PASS":
        raise ValueError("Today's stored source verification did not pass")

    output_dir = output_dir.resolve()
    destination = report_path(output_dir, plan.plan_id, day, run["id"])
    previous_review = initial_review
    previous_report_run_id = None
    for prior_batch in plan.batches:
        if prior_batch.date in plan.skipped_dates:
            continue
        if prior_batch.date >= day:
            break
        prior_request = plan.request_for(prior_batch.date)
        prior_run = db.run_by_key(plan.source, prior_request.run_key)
        if prior_run is None or prior_run["status"] != "SUCCEEDED":
            raise ValueError("An earlier planned batch is missing or failed")
        if prior_run["parameters"] != prior_request.model_dump(exclude={"source", "run_key"}):
            raise ValueError("An earlier planned batch differs from the plan")
        prior_snapshot, prior_rows = db.report(plan.source, prior_run["id"])
        if (
            verify_snapshot(prior_snapshot, prior_rows, db.evidence(prior_run["id"]))["status"]
            != "PASS"
        ):
            raise ValueError("An earlier planned batch failed source verification")
        prior_path = report_path(output_dir, plan.plan_id, prior_batch.date, prior_run["id"])
        if not prior_path.is_file():
            raise ValueError("An earlier planned report is missing; manual review is needed")
        previous_review = prior_path
        previous_report_run_id = prior_run["id"]

    result = {
        "status": "DRY_RUN" if dry_run else "EXPORTED",
        "day": day.isoformat(),
        "run_id": run["id"],
        "output": str(destination),
        "review_workbook": str(previous_review.resolve()) if previous_review else None,
    }
    if dry_run:
        return result
    if destination.exists():
        raise FileExistsError("Daily report already exists; inspect it before retrying")
    if previous_review is not None and not previous_review.is_file():
        raise ValueError("Initial review workbook is missing")

    output_dir.mkdir(parents=True, exist_ok=True)
    project = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["DATABASE_URL"] = settings.database_url
    environment["RAPIDAPI_KEY"] = ""
    environment["RAPIDAPI_FREE_PLAN_CONFIRMED"] = "false"
    environment.setdefault(
        "GUMMY_ARTIFACT_NODE_DIR",
        str(Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node"),
    )
    environment.pop("GUMMY_REVIEW_WORKBOOK", None)
    if previous_review is not None:
        environment["GUMMY_REVIEW_WORKBOOK"] = str(previous_review.resolve())
    if previous_report_run_id is not None:
        verify_portfolio_workbook(
            settings, plan.source, previous_report_run_id, previous_review, db=db, engine=engine
        )
    if engine == "python":
        from gummy.portable_workbook import export_book

        export_book(db, plan.source, run["id"], destination, previous_review)
        return result
    if engine != "artifact":
        raise ValueError("Unknown report engine")
    try:
        exported = subprocess.run(
            [
                "sh",
                str(project / "scripts/export_portfolio_local.sh"),
                plan.source,
                str(run["id"]),
                str(destination),
            ],
            cwd=project,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        raise ValueError("Daily report generation timed out; inspect the output file") from None
    if exported.returncode != 0:
        raise ValueError("Daily report generation or read-back verification failed")
    return result
