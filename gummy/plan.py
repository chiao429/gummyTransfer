"""Finite, reviewable multi-category collection plans for the local pilot."""

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gummy.models import CollectRequest, SourceName
from gummy.verification import verify_snapshot


class PlannedBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    date: date
    query: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=80)
    max_products: int = Field(ge=1, le=2)


class CollectionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    plan_id: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.-]+$")
    source: SourceName
    country: Literal["US"] = "US"
    batches: list[PlannedBatch] = Field(min_length=1, max_length=30)
    skipped_dates: list[date] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def finite_unique_days(self):
        dates = [item.date for item in self.batches]
        if dates != sorted(set(dates)):
            raise ValueError("Plan dates must be unique and ascending")
        if self.skipped_dates != sorted(set(self.skipped_dates)) or set(self.skipped_dates) - set(
            dates
        ):
            raise ValueError("Skipped dates must belong to the plan")
        if self.source == "rapidapi" and self.planned_requests > 30:
            raise ValueError("Live plan exceeds the 30-request pilot cap")
        return self

    @property
    def planned_requests(self) -> int:
        return (
            sum(
                1 + item.max_products
                for item in self.batches
                if item.date not in self.skipped_dates
            )
            if self.source == "rapidapi"
            else 0
        )

    def request_for(self, day: date) -> CollectRequest | None:
        if day in self.skipped_dates:
            return None
        item = next((batch for batch in self.batches if batch.date == day), None)
        if item is None:
            return None
        return CollectRequest(
            source=self.source,
            run_key=f"plan-{self.plan_id}-{day:%Y%m%d}",
            query=item.query,
            category=item.category,
            country=self.country,
            max_products=item.max_products,
        )


def load_plan(path: str | Path) -> tuple[CollectionPlan, str]:
    data = Path(path).read_bytes()
    plan = CollectionPlan.model_validate(json.loads(data))
    return plan, hashlib.sha256(data).hexdigest()


def plan_status(plan: CollectionPlan, db, settings, today: date) -> dict:
    quota = db.quota_status(settings.total_limit, settings.daily_limit)
    provider_quota = (
        db.provider_quota_status(settings.rapidapi_key) if plan.source == "rapidapi" else None
    )
    remaining = []
    conflicts = []
    missed = []
    batches = []
    for batch in plan.batches:
        if batch.date in plan.skipped_dates:
            batches.append(
                {
                    "date": batch.date.isoformat(),
                    "category": batch.category,
                    "query": batch.query,
                    "run_id": None,
                    "status": "SKIPPED_PAUSED",
                    "verification": None,
                    "started_at": None,
                }
            )
            continue
        request = plan.request_for(batch.date)
        previous = db.run_by_key(plan.source, request.run_key)
        verification_status = None
        if previous is None:
            if batch.date < today:
                missed.append(batch.date.isoformat())
                batch_status = "MISSED"
            else:
                remaining.append(batch)
                batch_status = "PENDING"
        elif previous["status"] != "SUCCEEDED" or previous["parameters"] != request.model_dump(
            exclude={"source", "run_key"}
        ):
            conflicts.append({"date": batch.date.isoformat(), "status": previous["status"]})
            batch_status = "CONFLICT"
        else:
            run, rows = db.report(plan.source, previous["id"])
            verification = verify_snapshot(run, rows, db.evidence(previous["id"]))
            verification_status = verification["status"]
            if verification["status"] != "PASS":
                conflicts.append({"date": batch.date.isoformat(), "status": verification["status"]})
                batch_status = "VERIFICATION_FAILED"
            else:
                batch_status = "SUCCEEDED"
        batches.append(
            {
                "date": batch.date.isoformat(),
                "category": batch.category,
                "query": batch.query,
                "run_id": previous["id"] if previous else None,
                "status": batch_status,
                "verification": verification_status,
                "started_at": previous.get("started_at") if previous else None,
            }
        )
    planned = sum(1 + item.max_products for item in remaining) if plan.source == "rapidapi" else 0
    if conflicts:
        status = "CONFLICTING_RUN_KEYS"
    elif missed:
        status = "MISSED_BATCH"
    elif not remaining:
        status = "FINISHED_OR_EXPIRED"
    elif (
        planned + quota["total_used"] > settings.total_limit
        or any(1 + item.max_products > settings.daily_limit for item in remaining)
        or any(
            item.date == today
            and quota["day"] == today.isoformat()
            and quota["daily_used"] + 1 + item.max_products > settings.daily_limit
            for item in remaining
        )
    ):
        status = "INSUFFICIENT_LOCAL_QUOTA"
    elif (
        provider_quota is not None
        and remaining
        and provider_quota["remaining_estimate"] <= 1 + remaining[0].max_products
    ):
        status = "PROVIDER_QUOTA_LOW"
    else:
        status = "READY"
    return {
        "status": status,
        "plan_id": plan.plan_id,
        "source": plan.source,
        "first_date": plan.batches[0].date.isoformat(),
        "last_date": plan.batches[-1].date.isoformat(),
        "remaining_days": len(remaining),
        "remaining_planned_requests": planned,
        "local_total_used": quota["total_used"],
        "local_total_limit": settings.total_limit,
        "conflicts": conflicts,
        "missed_dates": missed,
        "batches": batches,
        "provider_quota_checked": provider_quota is not None,
        "provider_remaining_estimate": provider_quota["remaining_estimate"]
        if provider_quota
        else None,
        "provider_quota_observed_at": provider_quota["last_header_at"] if provider_quota else None,
    }
