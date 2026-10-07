import hashlib
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    event,
    insert,
    select,
    update,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from gummy.errors import ConfigurationError, QuotaExceeded, ReportNotFound, RunConflict, SourceError
from gummy.verification import verify_snapshot

metadata = MetaData()
runs = Table(
    "collection_runs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("source", String(16), nullable=False),
    Column("run_key", String(80), nullable=False),
    Column("parameters", JSON, nullable=False),
    Column("status", String(16), nullable=False),
    Column("started_at", String(32), nullable=False),
    Column("completed_at", String(32)),
    Column("error_code", String(64)),
    UniqueConstraint("source", "run_key"),
)
products = Table(
    "product_snapshots",
    metadata,
    Column("run_id", ForeignKey("collection_runs.id"), primary_key=True),
    Column("asin", String(10), primary_key=True),
    Column("observed_at", String(32), nullable=False),
    Column("title", String(4000), nullable=False),
    Column("brand", String(120)),
    Column("price", Numeric(10, 2)),
    Column("price_raw", String(120)),
    Column("currency", String(3), nullable=False),
    Column("rating", Numeric(2, 1)),
    Column("rating_count", Integer),
    Column("specs", JSON, nullable=False),
    Column("warnings", JSON, nullable=False),
)
source_observations = Table(
    "source_observations",
    metadata,
    Column("run_id", ForeignKey("collection_runs.id"), primary_key=True),
    Column("asin", String(10), primary_key=True),
    Column("fields", JSON, nullable=False),
)
budgets = Table(
    "quota_budgets",
    metadata,
    Column("name", String(40), primary_key=True),
    Column("used", Integer, nullable=False),
)
attempts = Table(
    "api_attempts",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("run_id", ForeignKey("collection_runs.id"), nullable=False),
    Column("endpoint", String(40), nullable=False),
    Column("reserved_at", String(32), nullable=False),
    Column("outcome", String(32), nullable=False),
)
provider_quotas = Table(
    "provider_quota_state",
    metadata,
    Column("key_fingerprint", String(64), primary_key=True),
    Column("remaining_estimate", Integer, nullable=False),
    Column("last_header_at", String(32), nullable=False),
    Column("updated_at", String(32), nullable=False),
)

# Additive V8 tables. Existing snapshots, quota counters and schedules stay authoritative.
tasks = Table(
    "operation_tasks",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("kind", String(16), nullable=False),
    Column("source", String(16), nullable=False),
    Column("request", JSON, nullable=False),
    Column("binding", String(64), nullable=False),
    Column("status", String(24), nullable=False),
    Column("phase", String(32), nullable=False),
    Column("reserved_requests", Integer, nullable=False),
    Column("run_id", ForeignKey("collection_runs.id")),
    Column("cancel_requested", Boolean, nullable=False, default=False),
    Column("created_at", String(32), nullable=False),
    Column("updated_at", String(32), nullable=False),
    Column("started_at", String(32)),
    Column("completed_at", String(32)),
    Column("error_code", String(64)),
)
task_events = Table(
    "operation_task_events",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("task_id", ForeignKey("operation_tasks.id"), nullable=False),
    Column("phase", String(32), nullable=False),
    Column("occurred_at", String(32), nullable=False),
    Column("detail", String(120), nullable=False),
)
worker_state = Table(
    "operation_worker_state",
    metadata,
    Column("name", String(24), primary_key=True),
    Column("last_seen_at", String(32), nullable=False),
    Column("phase", String(32), nullable=False),
    Column("task_id", String(32)),
)
operation_reports = Table(
    "operation_reports",
    metadata,
    Column("task_id", ForeignKey("operation_tasks.id"), primary_key=True),
    Column("source", String(16), nullable=False),
    Column("through_run_id", ForeignKey("collection_runs.id"), nullable=False),
    Column("path", String(1000), nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("created_at", String(32), nullable=False),
    Column("workbook_version", Integer, nullable=False),
)

# V9/V10 are additive: applied schedules and fixed-scope report versions are immutable.
schedule_versions = Table(
    "schedule_versions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("confirmation_id", String(32), unique=True, nullable=False),
    Column("base_sha256", String(64), nullable=False),
    Column("plan", JSON, nullable=False),
    Column("config", JSON, nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("effective_from", String(10), nullable=False),
    Column("created_at", String(32), nullable=False),
)
report_versions = Table(
    "report_versions",
    metadata,
    Column("id", ForeignKey("operation_tasks.id"), primary_key=True),
    Column("source", String(16), nullable=False),
    Column("scope", JSON, nullable=False),
    Column("scope_hash", String(64), nullable=False),
    Column("data_digest", String(64), nullable=False),
    Column("parent_id", ForeignKey("report_versions.id")),
    Column("version", Integer, nullable=False),
    Column("created_at", String(32), nullable=False),
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, url: str):
        parsed = make_url(url)
        if parsed.drivername not in {"sqlite", "postgresql+psycopg"}:
            raise ConfigurationError("Use sqlite:///... or postgresql+psycopg://...")
        if parsed.drivername == "sqlite":
            if parsed.database in {None, "", ":memory:"}:
                raise ConfigurationError("Use a file-backed SQLite DB to persist quota state")
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
        args = {"check_same_thread": False, "timeout": 15} if parsed.drivername == "sqlite" else {}
        self.engine = create_engine(url, connect_args=args, pool_pre_ping=True)
        if parsed.drivername == "sqlite":

            @event.listens_for(self.engine, "connect")
            def sqlite_settings(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")

    def initialize(self):
        from gummy import recurrence  # noqa: F401 - register additive recurring tables
        from gummy.reviews import migrate_reviews, review_annotations, review_events, review_heads

        metadata.create_all(
            self.engine,
            tables=[
                t
                for t in metadata.sorted_tables
                if t not in (review_heads, review_events, review_annotations)
            ],
        )
        migrate_reviews(self.engine)

    def begin_run(self, request):
        parameters = request.model_dump(exclude={"run_key", "source"})
        with self.engine.begin() as conn:
            try:
                with conn.begin_nested():
                    result = conn.execute(
                        insert(runs).values(
                            source=request.source,
                            run_key=request.run_key,
                            parameters=parameters,
                            status="RUNNING",
                            started_at=utc_now(),
                        )
                    )
                    return result.inserted_primary_key[0], False
            except IntegrityError:
                previous = (
                    conn.execute(
                        select(runs).where(
                            runs.c.source == request.source,
                            runs.c.run_key == request.run_key,
                        )
                    )
                    .mappings()
                    .one()
                )
                if previous["parameters"] != parameters or previous["status"] != "SUCCEEDED":
                    raise RunConflict(
                        "Run key exists with different inputs or incomplete status"
                    ) from None
                return previous["id"], True

    def publish(self, run_id: int, items: list[dict], evidence: list[dict]):
        if len(items) != len(evidence) or any(
            item["asin"] != source["asin"] for item, source in zip(items, evidence)
        ):
            raise ValueError("Product and source evidence must have matching ASINs")
        with self.engine.begin() as conn:
            conn.execute(insert(products), [{"run_id": run_id, **item} for item in items])
            conn.execute(
                insert(source_observations),
                [
                    {"run_id": run_id, "asin": row["asin"], "fields": row["fields"]}
                    for row in evidence
                ],
            )
            run = dict(conn.execute(select(runs).where(runs.c.id == run_id)).mappings().one())
            stored_items = [
                dict(row)
                for row in conn.execute(
                    select(products).where(products.c.run_id == run_id)
                ).mappings()
            ]
            stored_evidence = {
                row["asin"]: row["fields"]
                for row in conn.execute(
                    select(source_observations).where(source_observations.c.run_id == run_id)
                ).mappings()
            }
            if verify_snapshot(run, stored_items, stored_evidence)["status"] != "PASS":
                raise SourceError("Stored snapshot did not match captured source fields")
            conn.execute(
                update(runs)
                .where(runs.c.id == run_id)
                .values(
                    status="SUCCEEDED",
                    completed_at=utc_now(),
                )
            )

    def fail(self, run_id: int, error_code: str):
        with self.engine.begin() as conn:
            conn.execute(
                update(runs)
                .where(runs.c.id == run_id)
                .values(
                    status="FAILED",
                    completed_at=utc_now(),
                    error_code=error_code,
                )
            )

    def report(self, source="demo", run_id: int | None = None):
        with self.engine.connect() as conn:
            query = select(runs).where(runs.c.status == "SUCCEEDED", runs.c.source == source)
            if run_id is not None:
                query = query.where(runs.c.id == run_id)
            run = conn.execute(query.order_by(runs.c.id.desc()).limit(1)).mappings().first()
            if not run:
                raise ReportNotFound("No successful report for the selected source/run")
            rows = (
                conn.execute(
                    select(products)
                    .where(
                        products.c.run_id == run["id"],
                    )
                    .order_by(products.c.asin)
                )
                .mappings()
                .all()
            )
            return dict(run), [dict(row) for row in rows]

    def evidence(self, run_id: int) -> dict[str, dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(source_observations).where(source_observations.c.run_id == run_id)
            ).mappings()
            return {row["asin"]: row["fields"] for row in rows}

    def recent_runs(self, limit: int = 5) -> list[dict]:
        with self.engine.connect() as conn:
            records = conn.execute(select(runs).order_by(runs.c.id.desc()).limit(limit)).mappings()
            return [dict(record) for record in records]

    def run_by_key(self, source: str, run_key: str) -> dict | None:
        with self.engine.connect() as conn:
            record = (
                conn.execute(select(runs).where(runs.c.source == source, runs.c.run_key == run_key))
                .mappings()
                .first()
            )
            return dict(record) if record else None

    def successful_run_ids(self, source: str, through_run_id: int) -> list[int]:
        """List one source's completed snapshots up to an inclusive export boundary."""
        with self.engine.connect() as conn:
            return list(
                conn.execute(
                    select(runs.c.id)
                    .where(
                        runs.c.source == source,
                        runs.c.status == "SUCCEEDED",
                        runs.c.id <= through_run_id,
                    )
                    .order_by(runs.c.id)
                ).scalars()
            )

    def category_stats(self, source: str, through_run_id: int | None = None) -> dict[str, dict]:
        query = (
            select(runs.c.id, runs.c.parameters, products.c.asin)
            .join(products, products.c.run_id == runs.c.id)
            .where(runs.c.source == source, runs.c.status == "SUCCEEDED")
        )
        if through_run_id is not None:
            query = query.where(runs.c.id <= through_run_id)
        with self.engine.connect() as conn:
            records = conn.execute(query).all()
        grouped = {}
        for run_id, parameters, asin in records:
            category = parameters["category"]
            group = grouped.setdefault(category, {"run_ids": set(), "asins": set()})
            group["run_ids"].add(run_id)
            group["asins"].add(asin)
        return {
            category: {
                "run_count": len(values["run_ids"]),
                "distinct_product_count": len(values["asins"]),
                "latest_run_id": max(values["run_ids"]),
            }
            for category, values in grouped.items()
        }

    def quota_status(self, total_limit: int, daily_limit: int):
        today = datetime.now(UTC).date().isoformat()
        with self.engine.connect() as conn:
            used = dict(conn.execute(select(budgets.c.name, budgets.c.used)).all())
        return {
            "scope": "this_database_only",
            "day_timezone": "UTC",
            "day": today,
            "total_used": used.get("lifetime", 0),
            "total_limit": total_limit,
            "daily_used": used.get(f"day:{today}", 0),
            "daily_limit": daily_limit,
        }

    def provider_quota_status(self, api_key: str | None) -> dict | None:
        """Last known quota for this key; other applications may have spent it since."""
        if not api_key:
            return None
        fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(provider_quotas).where(provider_quotas.c.key_fingerprint == fingerprint)
                )
                .mappings()
                .first()
            )
        return (
            {
                "remaining_estimate": row["remaining_estimate"],
                "last_header_at": row["last_header_at"],
                "updated_at": row["updated_at"],
            }
            if row
            else None
        )

    def record_provider_quota(self, api_key: str, remaining: int, header_seen: bool):
        """Persist a conservative estimate without storing the API key or headers."""
        if remaining < 0:
            raise ValueError("Provider quota estimate cannot be negative")
        fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        observed_at = utc_now()
        with self.engine.begin() as conn:
            previous = (
                conn.execute(
                    select(provider_quotas).where(provider_quotas.c.key_fingerprint == fingerprint)
                )
                .mappings()
                .first()
            )
            if previous is None:
                if not header_seen:
                    return
                conn.execute(
                    insert(provider_quotas).values(
                        key_fingerprint=fingerprint,
                        remaining_estimate=remaining,
                        last_header_at=observed_at,
                        updated_at=observed_at,
                    )
                )
            else:
                conn.execute(
                    update(provider_quotas)
                    .where(provider_quotas.c.key_fingerprint == fingerprint)
                    .values(
                        remaining_estimate=min(previous["remaining_estimate"], remaining),
                        last_header_at=observed_at if header_seen else previous["last_header_at"],
                        updated_at=observed_at,
                    )
                )

    def preflight_quota(self, planned: int, total_limit: int, daily_limit: int, run_id=None):
        state = self.quota_status(total_limit, daily_limit)
        with self.engine.connect() as conn:
            held = sum(
                row["reserved_requests"]
                for row in conn.execute(
                    select(tasks).where(
                        tasks.c.status.in_(["QUEUED", "RUNNING"]), tasks.c.source == "rapidapi"
                    )
                ).mappings()
                if row["run_id"] != run_id or run_id is None
            )
        if (
            state["total_used"] + planned + held > total_limit
            or state["daily_used"] + planned + held > daily_limit
        ):
            raise QuotaExceeded("Insufficient local quota for the entire planned batch")

    def reserve_attempt(self, run_id: int, endpoint: str, total_limit: int, daily_limit: int):
        today = datetime.now(UTC).date().isoformat()
        # Conditional UPDATE + one transaction serialize writers on SQLite and PostgreSQL.
        # Reservation is committed BEFORE I/O. Crashes/timeouts keep it consumed, conservatively.
        with self.engine.begin() as conn:
            active = (
                conn.execute(
                    select(tasks).where(
                        tasks.c.source == "rapidapi", tasks.c.status.in_(["QUEUED", "RUNNING"])
                    )
                )
                .mappings()
                .all()
            )
            mine = next((row for row in active if row["run_id"] == run_id), None)
            held = sum(row["reserved_requests"] for row in active if row["run_id"] != run_id)
            for name, limit in (("lifetime", total_limit), (f"day:{today}", daily_limit)):
                try:
                    with conn.begin_nested():
                        conn.execute(insert(budgets).values(name=name, used=0))
                except IntegrityError:
                    pass
                result = conn.execute(
                    update(budgets)
                    .where(
                        budgets.c.name == name,
                        budgets.c.used < limit - held,
                    )
                    .values(used=budgets.c.used + 1)
                )
                if result.rowcount != 1:
                    raise QuotaExceeded("Local request budget exhausted; no request was sent")
            if mine is not None:
                result = conn.execute(
                    update(tasks)
                    .where(tasks.c.id == mine["id"], tasks.c.reserved_requests > 0)
                    .values(reserved_requests=tasks.c.reserved_requests - 1, updated_at=utc_now())
                )
                if result.rowcount != 1:
                    raise QuotaExceeded("Task request reservation exhausted")
            result = conn.execute(
                insert(attempts).values(
                    run_id=run_id,
                    endpoint=endpoint,
                    reserved_at=utc_now(),
                    outcome="reserved",
                )
            )
            return result.inserted_primary_key[0]

    def finish_attempt(self, attempt_id: int, outcome: str):
        with self.engine.begin() as conn:
            conn.execute(
                update(attempts).where(attempts.c.id == attempt_id).values(outcome=outcome)
            )
