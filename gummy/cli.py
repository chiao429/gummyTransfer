import argparse
import json
import os
import plistlib
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from gummy.config import Settings
from gummy.db import Database
from gummy.errors import GummyError
from gummy.models import DIMENSIONS, CollectRequest, CombinationQuery
from gummy.plan import load_plan, plan_status
from gummy.plan_report import export_plan_report, verify_previous_plan_workbook
from gummy.reference import reference_coverage
from gummy.reports import export_combinations_csv, export_csv, export_source_audit_csv
from gummy.scheduled import in_trial_window, local_now, scheduled_request, search_parameters
from gummy.verification import verify_snapshot
from gummy.workbook_data import portfolio_payload, workbook_payload
from gummy.worker import collect


def parser():
    root = argparse.ArgumentParser(description="gummy local collection worker and read API")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "schedule-migrate", help="Convert remaining legacy dates to one-time rules; no collection"
    )
    sub.add_parser("init-db", help="Initialize local schema and additive review migration")
    sub.add_parser("quota", help="Show local accounting without calling the provider")
    scheduler = sub.add_parser("scheduler", help="Run the independent Python daily scheduler")
    scheduler.add_argument("--config", default="python-scheduler.json")
    scheduler.add_argument("--check", action="store_true", help="Read-only check; never collect")
    scheduler.add_argument("--once", action="store_true", help="Execute one current due-job check")
    portable_export = sub.add_parser("export-xlsx", help="Export verified XLSX with Python only")
    portable_export.add_argument("--source", choices=["demo", "rapidapi"], default="demo")
    portable_export.add_argument("--run-id", type=int, required=True)
    portable_export.add_argument("--output", required=True)
    portable_export.add_argument("--review-workbook")
    worker = sub.add_parser("task-worker", help="Run durable local tasks confirmed in the UI")
    worker.add_argument("--once", action="store_true")
    sub.add_parser("configure-rapidapi", help="Secure interactive local key setup; no API calls")
    collect_cmd = sub.add_parser("collect", help="Collect one bounded batch (demo by default)")
    collect_cmd.add_argument("--source", choices=["demo", "rapidapi"], default="demo")
    collect_cmd.add_argument(
        "--run-key", default=datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat()
    )
    collect_cmd.add_argument("--query", default="socket wrench set")
    collect_cmd.add_argument("--category", default="套筒扳手")
    collect_cmd.add_argument("--max-products", type=int, default=2)
    scheduled = sub.add_parser(
        "scheduled-collect", help="Run one idempotent batch for the current Taipei date"
    )
    scheduled.add_argument("--source", choices=["demo", "rapidapi"], default="demo")
    scheduled.add_argument("--query")
    scheduled.add_argument("--category")
    scheduled.add_argument("--max-products", type=int, default=2)
    scheduled.add_argument("--start-date", type=date.fromisoformat)
    scheduled.add_argument("--end-date", type=date.fromisoformat)
    scheduled.add_argument("--dry-run", action="store_true")
    plist = sub.add_parser("schedule-plist", help="Generate an inactive macOS LaunchAgent plist")
    plist.add_argument("--source", choices=["demo", "rapidapi"], default="demo")
    plist.add_argument("--query")
    plist.add_argument("--category")
    plist.add_argument("--max-products", type=int, default=2)
    plist.add_argument("--hour", type=int, default=9)
    plist.add_argument("--minute", type=int, default=0)
    plist.add_argument("--start-date", type=date.fromisoformat)
    plist.add_argument("--end-date", type=date.fromisoformat)
    plist.add_argument("--output", default="data/com.woobe.gummy.daily.plist")
    plan_check = sub.add_parser(
        "plan-validate", help="Check a finite category plan without collection"
    )
    plan_check.add_argument("--plan", required=True)
    plan_run = sub.add_parser("run-plan", help="Run today's category from a finite plan")
    plan_run.add_argument("--plan", required=True)
    plan_run.add_argument("--expected-sha256")
    plan_run.add_argument("--report-output-dir", default="exports/daily")
    plan_run.add_argument("--dry-run", action="store_true")
    plan_plist = sub.add_parser("plan-plist", help="Generate an inactive plan LaunchAgent")
    plan_plist.add_argument("--plan", required=True)
    plan_plist.add_argument("--hour", type=int, default=9)
    plan_plist.add_argument("--minute", type=int, default=10)
    plan_plist.add_argument("--output", default="data/com.woobe.gummy.plan.plist")
    plan_report = sub.add_parser(
        "plan-report", help="Export today's verified planned batch to a local XLSX"
    )
    plan_report.add_argument("--plan", required=True)
    plan_report.add_argument("--expected-sha256")
    plan_report.add_argument("--output-dir", default="exports/daily")
    plan_report.add_argument("--initial-review-workbook")
    plan_report.add_argument("--wait-seconds", type=int, default=0)
    plan_report.add_argument("--dry-run", action="store_true")
    report_plist = sub.add_parser(
        "plan-report-plist", help="Generate an inactive daily report LaunchAgent"
    )
    report_plist.add_argument("--plan", required=True)
    report_plist.add_argument("--hour", type=int, default=9)
    report_plist.add_argument("--minute", type=int, default=20)
    report_plist.add_argument("--output-dir", default="exports/daily")
    report_plist.add_argument("--initial-review-workbook")
    report_plist.add_argument("--wait-seconds", type=int, default=300)
    report_plist.add_argument("--output", default="data/com.woobe.gummy.report.plist")
    export = sub.add_parser("export", help="Write the latest successful snapshot as CSV")
    export.add_argument("--source", choices=["demo", "rapidapi"], default="demo")
    export.add_argument("--run-id", type=int)
    export.add_argument("--output", default="exports/products.csv")
    combined = sub.add_parser(
        "export-combinations", help="Export same-ASIN specification groups as CSV"
    )
    combined.add_argument("--source", choices=["demo", "rapidapi"], default="demo")
    combined.add_argument("--run-id", type=int)
    combined.add_argument(
        "--group-by",
        nargs="+",
        choices=sorted(DIMENSIONS),
        default=["material", "measurement_system"],
    )
    combined.add_argument("--include-title-candidates", action="store_true")
    combined.add_argument("--output", default="exports/combinations.csv")
    audit = sub.add_parser(
        "export-audit", help="Export selected source fields beside stored values"
    )
    audit.add_argument("--source", choices=["demo", "rapidapi"], default="demo")
    audit.add_argument("--run-id", type=int)
    audit.add_argument("--output", default="exports/source-audit.csv")
    workbook = sub.add_parser(
        "export-workbook-data", help="Write source-labelled data for a multi-sheet workbook"
    )
    workbook.add_argument("--source", choices=["demo", "rapidapi"], default="demo")
    workbook.add_argument("--run-id", type=int)
    workbook.add_argument("--output", default="exports/workbook-data.json")
    portfolio = sub.add_parser(
        "export-portfolio-data", help="Write all successful category snapshots through one run"
    )
    portfolio.add_argument("--source", choices=["demo", "rapidapi"], default="demo")
    portfolio.add_argument("--through-run-id", type=int)
    portfolio.add_argument("--output", default="exports/portfolio-data.json")
    portfolio.add_argument("--include-web-reviews", action="store_true")
    portfolio.add_argument("--review-snapshot", type=int)
    portfolio.add_argument("--workbook-version", type=int, choices=[1, 2, 3], default=3)
    verify = sub.add_parser(
        "verify-run", help="Check stored products against captured source fields"
    )
    verify.add_argument("--source", choices=["demo", "rapidapi"], default="demo")
    verify.add_argument("--run-id", type=int)
    serve = sub.add_parser("serve", help="Start the gummy read API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=int(os.getenv("PORT", "8010")))
    return root


def main():
    args = parser().parse_args()
    db = None
    try:
        if args.command == "scheduler":
            # The config's folder is the project root, independent of caller's working directory.
            args.config = str(Path(args.config).resolve())
            os.chdir(Path(args.config).parent)
        if args.command == "configure-rapidapi":
            from gummy.local_settings import configure_rapidapi

            print(json.dumps(configure_rapidapi(), ensure_ascii=False))
            return
        settings = Settings.from_env()
        if args.command == "serve":
            settings.validate_bind(args.host)
            uvicorn.run("gummy.api:create_app", factory=True, host=args.host, port=args.port)
            return
        request = None
        scheduled_at = None
        if args.command == "schedule-plist":
            # A finite live trial prevents a forgotten local agent from running indefinitely.
            if args.source == "rapidapi" and not args.end_date:
                raise ValueError("Live schedules require --end-date")
            if not 0 <= args.hour <= 23 or not 0 <= args.minute <= 59:
                raise ValueError("Schedule hour/minute is out of range")
            if args.source == "rapidapi" and args.hour < 9:
                raise ValueError("Live local schedules must run at or after 09:00")
            if args.start_date and args.end_date and args.end_date < args.start_date:
                raise ValueError("End date is before start date")
            query, category = search_parameters(args.source, args.query, args.category)
            scheduled_request(args.source, query, category, args.max_products)
            arguments = [
                sys.executable,
                "-m",
                "gummy",
                "scheduled-collect",
                "--source",
                args.source,
                "--query",
                query,
                "--category",
                category,
                "--max-products",
                str(args.max_products),
            ]
            for option in ("start_date", "end_date"):
                value = getattr(args, option)
                if value:
                    arguments.extend(["--" + option.replace("_", "-"), value.isoformat()])
            path = Path(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            content = {
                "Label": "com.woobe.gummy.amazon.daily",
                "ProgramArguments": arguments,
                "WorkingDirectory": str(Path.cwd().resolve()),
                "StartCalendarInterval": {"Hour": args.hour, "Minute": args.minute},
                "RunAtLoad": False,
                "StandardOutPath": str((Path.cwd() / "data" / "schedule.stdout.log").resolve()),
                "StandardErrorPath": str((Path.cwd() / "data" / "schedule.stderr.log").resolve()),
            }
            with path.open("xb") as handle:
                plistlib.dump(content, handle)
            print(json.dumps({"output": str(path.resolve()), "status": "GENERATED_NOT_INSTALLED"}))
            return
        if args.command in {"collect", "scheduled-collect"}:
            if args.command == "scheduled-collect":
                scheduled_at = local_now()
                query, category = search_parameters(args.source, args.query, args.category)
                request = scheduled_request(
                    args.source, query, category, args.max_products, scheduled_at
                )
            else:
                request = CollectRequest(
                    source=args.source,
                    run_key=args.run_key,
                    query=args.query,
                    category=args.category,
                    max_products=args.max_products,
                )
        db = Database(settings.database_url)
        if (settings.app_env == "local" or args.command == "init-db") and not (
            args.command == "scheduler" and args.check
        ):
            db.initialize()
        if args.command in {"run-plan", "scheduled-collect"}:
            from gummy.recurrence import active

            if active(db):
                raise ValueError(
                    "Recurring rules are active; legacy scheduled collection is disabled"
                )
        if args.command == "init-db":
            result = {"status": "initialized"}
        elif args.command == "schedule-migrate":
            from gummy.recurrence import migrate, overview

            migrate(db, settings)
            result = {"status": "MIGRATED", "rules": len(overview(db, settings)["rules"])}
        elif args.command == "task-worker":
            from gummy.operations import run_task_worker

            run_task_worker(db, settings, once=args.once)
            return
        elif args.command == "quota":
            result = db.quota_status(settings.total_limit, settings.daily_limit)
        elif args.command == "scheduler":
            from gummy.python_scheduler import PythonScheduler, load_scheduler, scheduler_health

            config, plan, _ = load_scheduler(args.config)
            if args.check:
                result = {
                    "scheduler": scheduler_health(args.config, db=db),
                    "plan": plan_status(plan, db, settings, local_now().date()),
                    "collect_at": config.collect_at,
                    "report_at": config.report_at,
                    "timezone": config.timezone,
                    "provider_requests_sent": 0,
                }
            else:
                PythonScheduler(args.config, db, settings).run(once=args.once)
                return
        elif args.command == "export-xlsx":
            from gummy.portable_workbook import export_book

            result = export_book(
                db,
                args.source,
                args.run_id,
                Path(args.output),
                Path(args.review_workbook) if args.review_workbook else None,
            )
        elif args.command in {
            "plan-validate",
            "run-plan",
            "plan-plist",
            "plan-report",
            "plan-report-plist",
        }:
            plan, digest = load_plan(args.plan)
            now = local_now()
            if args.command == "plan-validate":
                result = {**plan_status(plan, db, settings, now.date()), "sha256": digest}
            elif args.command == "plan-report":
                if args.expected_sha256 and args.expected_sha256 != digest:
                    raise ValueError("Plan file changed since the report schedule was generated")
                result = export_plan_report(
                    plan,
                    db,
                    settings,
                    now.date(),
                    Path(args.output_dir),
                    Path(args.initial_review_workbook) if args.initial_review_workbook else None,
                    args.dry_run,
                    args.wait_seconds,
                )
            elif args.command == "plan-report-plist":
                if not 0 <= args.hour <= 23 or not 0 <= args.minute <= 59:
                    raise ValueError("Schedule hour/minute is out of range")
                if not 0 <= args.wait_seconds <= 600:
                    raise ValueError("Report wait must be between 0 and 600 seconds")
                if plan.source == "rapidapi" and (args.hour, args.minute) <= (9, 10):
                    raise ValueError("Live reports must run after the 09:10 collection window")
                if plan_status(plan, db, settings, now.date())["status"] != "READY":
                    raise ValueError("Plan is not ready for a report schedule")
                path = Path(args.output)
                path.parent.mkdir(parents=True, exist_ok=True)
                arguments = [
                    sys.executable,
                    "-m",
                    "gummy",
                    "plan-report",
                    "--plan",
                    str(Path(args.plan).resolve()),
                    "--expected-sha256",
                    digest,
                    "--output-dir",
                    str(Path(args.output_dir).resolve()),
                    "--wait-seconds",
                    str(args.wait_seconds),
                ]
                if args.initial_review_workbook:
                    arguments.extend(
                        [
                            "--initial-review-workbook",
                            str(Path(args.initial_review_workbook).resolve()),
                        ]
                    )
                content = {
                    "Label": f"com.woobe.gummy.report.{plan.plan_id}",
                    "ProgramArguments": arguments,
                    "WorkingDirectory": str(Path.cwd().resolve()),
                    "StartCalendarInterval": {"Hour": args.hour, "Minute": args.minute},
                    "RunAtLoad": False,
                    "StandardOutPath": str(
                        (Path.cwd() / "data" / f"report-{plan.plan_id}.stdout.log").resolve()
                    ),
                    "StandardErrorPath": str(
                        (Path.cwd() / "data" / f"report-{plan.plan_id}.stderr.log").resolve()
                    ),
                }
                with path.open("xb") as handle:
                    plistlib.dump(content, handle)
                result = {
                    "output": str(path.resolve()),
                    "status": "GENERATED_NOT_INSTALLED",
                    "sha256": digest,
                }
            elif args.command == "plan-plist":
                if not 0 <= args.hour <= 23 or not 0 <= args.minute <= 59:
                    raise ValueError("Schedule hour/minute is out of range")
                if plan.source == "rapidapi" and args.hour < 9:
                    raise ValueError("Live local schedules must run at or after 09:00")
                state = plan_status(plan, db, settings, now.date())
                if state["status"] != "READY":
                    raise ValueError("Plan is not ready: " + state["status"])
                if plan.source == "rapidapi":
                    settings.validate_live()
                path = Path(args.output)
                path.parent.mkdir(parents=True, exist_ok=True)
                content = {
                    "Label": f"com.woobe.gummy.plan.{plan.plan_id}",
                    "ProgramArguments": [
                        sys.executable,
                        "-m",
                        "gummy",
                        "run-plan",
                        "--plan",
                        str(Path(args.plan).resolve()),
                        "--expected-sha256",
                        digest,
                    ],
                    "WorkingDirectory": str(Path.cwd().resolve()),
                    "StartCalendarInterval": {"Hour": args.hour, "Minute": args.minute},
                    "RunAtLoad": False,
                    "StandardOutPath": str(
                        (Path.cwd() / "data" / f"plan-{plan.plan_id}.stdout.log").resolve()
                    ),
                    "StandardErrorPath": str(
                        (Path.cwd() / "data" / f"plan-{plan.plan_id}.stderr.log").resolve()
                    ),
                }
                with path.open("xb") as handle:
                    plistlib.dump(content, handle)
                result = {
                    "output": str(path.resolve()),
                    "status": "GENERATED_NOT_INSTALLED",
                    "sha256": digest,
                }
            else:
                if args.expected_sha256 and args.expected_sha256 != digest:
                    raise ValueError("Plan file changed since the schedule was generated")
                request = plan.request_for(now.date())
                if request is None:
                    result = {"status": "NO_PLAN_FOR_DATE", "day": now.date().isoformat()}
                elif plan.source == "rapidapi" and now.hour < 9:
                    result = {"status": "BEFORE_DAILY_WINDOW", "day": now.date().isoformat()}
                else:
                    previous = db.run_by_key(plan.source, request.run_key)
                    if previous is not None:
                        if previous["status"] != "SUCCEEDED" or previous[
                            "parameters"
                        ] != request.model_dump(exclude={"source", "run_key"}):
                            raise ValueError("Today's plan run key has conflicting or failed state")
                        result = {
                            "status": "ALREADY_COLLECTED",
                            "run_id": previous["id"],
                            "run_key": request.run_key,
                        }
                    else:
                        state = plan_status(plan, db, settings, now.date())
                        if state["status"] != "READY":
                            raise ValueError("Plan is not ready: " + state["status"])
                        if plan.source == "rapidapi":
                            verify_previous_plan_workbook(
                                plan, db, settings, now.date(), Path(args.report_output_dir)
                            )
                            settings.validate_live()
                            db.preflight_quota(
                                1 + request.max_products,
                                settings.total_limit,
                                settings.daily_limit,
                            )
                        if args.dry_run:
                            result = {
                                "status": "DRY_RUN",
                                "run_key": request.run_key,
                                "category": request.category,
                                "planned_external_requests": 1 + request.max_products
                                if plan.source == "rapidapi"
                                else 0,
                            }
                        else:
                            result = collect(db, settings, request)
        elif args.command == "collect":
            result = collect(db, settings, request)
        elif args.command == "scheduled-collect":
            start = args.start_date
            end = args.end_date
            if start and end and end < start:
                raise ValueError("End date is before start date")
            day = scheduled_at.date()
            if not in_trial_window(day, start, end):
                result = {"status": "OUTSIDE_TRIAL_WINDOW", "run_key": request.run_key}
            elif request.source == "rapidapi" and scheduled_at.hour < 9:
                result = {"status": "BEFORE_DAILY_WINDOW", "run_key": request.run_key}
            elif args.dry_run:
                if request.source == "rapidapi":
                    settings.validate_live()
                    db.preflight_quota(
                        1 + request.max_products, settings.total_limit, settings.daily_limit
                    )
                result = {
                    "status": "DRY_RUN",
                    "source": request.source,
                    "run_key": request.run_key,
                    "planned_external_requests": 1 + request.max_products
                    if request.source == "rapidapi"
                    else 0,
                }
            else:
                result = collect(db, settings, request)
        elif args.command == "verify-run":
            run, rows = db.report(args.source, args.run_id)
            result = verify_snapshot(run, rows, db.evidence(run["id"]))
        else:
            if args.command == "export-portfolio-data":
                run, _ = db.report(args.source, args.through_run_id)
                snapshots = []
                for selected_id in db.successful_run_ids(args.source, run["id"]):
                    selected_run, selected_rows = db.report(args.source, selected_id)
                    snapshots.append((selected_run, selected_rows, db.evidence(selected_id)))
                payload = portfolio_payload(
                    snapshots,
                    reference_coverage(
                        db.category_stats(args.source, through_run_id=run["id"]),
                        args.source,
                    ),
                )
                payload["workbook_schema_version"] = args.workbook_version
                if args.workbook_version == 2:
                    from gummy.quality import sample_label_rows

                    payload["review_samples"] = sample_label_rows(
                        payload["review_samples"], None, ""
                    )
                if args.include_web_reviews:
                    from gummy.workbook_data import attach_web_reviews

                    if args.review_snapshot is not None and not 0 <= args.review_snapshot < 2**63:
                        raise ValueError("Invalid review snapshot")
                    attach_web_reviews(db, payload, args.review_snapshot)
                content = json.dumps(payload, ensure_ascii=False)
            else:
                run, rows = db.report(args.source, args.run_id)
                if args.command == "export-workbook-data":
                    content = json.dumps(
                        workbook_payload(
                            run,
                            rows,
                            db.evidence(run["id"]),
                            reference_coverage(
                                db.category_stats(run["source"], through_run_id=run["id"]),
                                run["source"],
                            ),
                        ),
                        ensure_ascii=False,
                    )
                elif args.command == "export-combinations":
                    query = CombinationQuery(
                        source=args.source,
                        run_id=run["id"],
                        group_by=args.group_by,
                        include_title_candidates=args.include_title_candidates,
                    )
                    content = export_combinations_csv(run, rows, query)
                elif args.command == "export-audit":
                    content = export_source_audit_csv(run, rows, db.evidence(run["id"]))
                else:
                    content = export_csv(run, rows)
            path = Path(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation avoids overwriting an existing export accidentally.
            with path.open("x", encoding="utf-8", newline="") as handle:
                handle.write(content)
            result = {"output": str(path.resolve()), "run_id": run["id"], "source": args.source}
        print(json.dumps(result, ensure_ascii=False))
        if args.command == "verify-run" and result["status"] != "PASS":
            raise SystemExit(2)
    except (GummyError, ValidationError, FileExistsError, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}, ensure_ascii=False))
        raise SystemExit(1) from None
    except (SQLAlchemyError, OSError):
        # Database URLs and upstream bodies may contain secrets; do not print exception strings.
        print(
            json.dumps(
                {
                    "error": "storage_error",
                    "detail": "Check database connectivity and file permissions",
                }
            )
        )
        raise SystemExit(1) from None
    finally:
        if db:
            db.engine.dispose()
