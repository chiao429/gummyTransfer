"""Read-only, explicitly scoped browsing and exports over persisted snapshots."""

import csv
import io
import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select

from gummy.db import products, runs, source_observations
from gummy.models import SourceName, normalize_text
from gummy.reports import PRODUCT_EXPORT_FIELDS, product_export_values, safe_cell
from gummy.reviews import REVIEW_FIELDS, fingerprint, review_map, review_values

TAIPEI = ZoneInfo("Asia/Taipei")
DEFAULT_COLUMNS = [
    "run_id",
    "category",
    "asin",
    "title",
    "brand",
    "price",
    "currency",
    "rating",
    "rating_count",
    "observed_at",
]
EXPORT_FIELDS = [*PRODUCT_EXPORT_FIELDS, "evidence_status", *REVIEW_FIELDS]


class CatalogQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source: SourceName = "rapidapi"
    q: str = Field(default="", max_length=120)
    category: str = Field(default="", max_length=80)
    brand: str = Field(default="", max_length=120)
    material: str = Field(default="", max_length=120)
    measurement_system: str = Field(default="", max_length=120)
    date_from: date | None = None
    date_to: date | None = None
    run_id: int | None = Field(default=None, gt=0)
    min_price: Decimal | None = Field(default=None, ge=0, le=99999999.99, decimal_places=2)
    max_price: Decimal | None = Field(default=None, ge=0, le=99999999.99, decimal_places=2)
    view: Literal["latest", "observations"] = "latest"
    review_status: Literal["all", "pending", "entered", "stale"] = "all"
    sort: Literal["newest", "price_asc", "price_desc", "title"] = "newest"
    page: int = Field(default=1, ge=1, le=100000)
    page_size: Literal[10, 20, 50] = 20
    # The successful run manifest freezes membership, including late-finishing lower IDs.
    # This is a read scope, not an authorization token. Source is always enforced separately.
    snapshot: str | None = Field(default=None, max_length=4000)
    review_snapshot: int | None = Field(default=None, ge=0, lt=2**63)
    columns: list[str] = Field(default_factory=lambda: list(DEFAULT_COLUMNS), max_length=40)

    @field_validator("page_size", mode="before")
    @classmethod
    def query_page_size(cls, value):
        return int(value) if isinstance(value, str) and value in {"10", "20", "50"} else value

    @field_validator(
        "date_from", "date_to", "run_id", "min_price", "max_price", "snapshot", mode="before"
    )
    @classmethod
    def empty_is_none(cls, value):
        return None if value == "" else value

    @field_validator("snapshot")
    @classmethod
    def check_snapshot(cls, value):
        if value is None or value == "0":
            return value
        ids = value.split(",")
        if len(ids) > 500 or any(
            not x.isascii() or not x.isdigit() or not 0 < int(x) < 2**63 for x in ids
        ):
            raise ValueError("Invalid snapshot manifest")
        return ",".join(str(x) for x in sorted({int(x) for x in ids}))

    @field_validator("columns")
    @classmethod
    def check_columns(cls, value):
        value = [x for x in value if x]  # Empty hidden field allows rejecting zero selections.
        if not value or set(value) - set(EXPORT_FIELDS):
            raise ValueError("Select at least one supported export column")
        if set(value) & set(REVIEW_FIELDS):
            value += ["review_state", "review_revision", "review_updated_at"]
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def check_ranges(self):
        if any(day and not 1900 <= day.year <= 9998 for day in (self.date_from, self.date_to)):
            raise ValueError("Dates must be within 1900–9998")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("Start date must not exceed end date")
        if self.date_to == date.max:
            raise ValueError("End date is outside supported range")
        if (
            self.min_price is not None
            and self.max_price is not None
            and self.min_price > self.max_price
        ):
            raise ValueError("Minimum price must not exceed maximum price")
        return self


def catalog_data(db, query: CatalogQuery) -> dict:
    statement = select(runs).where(runs.c.source == query.source, runs.c.status == "SUCCEEDED")
    if query.run_id:
        statement = statement.where(runs.c.id == query.run_id)
    for day, lower in ((query.date_from, True), (query.date_to, False)):
        if day:
            boundary = datetime.combine(day if lower else day + timedelta(days=1), time(), TAIPEI)
            value = boundary.astimezone(UTC).isoformat(timespec="seconds")
            statement = statement.where(
                runs.c.completed_at >= value if lower else runs.c.completed_at < value
            )
    if query.snapshot is not None:
        statement = statement.where(runs.c.id.in_([int(x) for x in query.snapshot.split(",")]))
    with db.engine.connect() as conn:
        selected_runs = [
            dict(row) for row in conn.execute(statement.order_by(runs.c.id).limit(501)).mappings()
        ]
        if len(selected_runs) > 500:
            raise ValueError("Too many batches; choose a shorter date range")
        manifest = (
            query.snapshot
            if query.snapshot is not None
            else ",".join(str(row["id"]) for row in selected_runs) or "0"
        )
        selected = {row["id"]: row for row in selected_runs}
        statement = (
            select(
                products,
                source_observations.c.asin.label("evidence_asin"),
                source_observations.c.fields.label("evidence_fields"),
            )
            .outerjoin(
                source_observations,
                (source_observations.c.run_id == products.c.run_id)
                & (source_observations.c.asin == products.c.asin),
            )
            .where(products.c.run_id.in_(selected))
            .order_by(products.c.run_id, products.c.asin)
            .limit(10001)
        )
        records = [dict(row) for row in conn.execute(statement).mappings()]
    if len(records) > 10000:
        raise ValueError("Too many observations; choose a shorter date range")
    observations = []
    review_mark, reviews = review_map(db, selected, query.review_snapshot)
    for row in records:
        run = selected[row["run_id"]]
        digest = fingerprint(run, row, row["evidence_fields"])
        current_review = reviews.get((row["run_id"], row["asin"]))
        observations.append(
            {
                **product_export_values(run, row),
                "completed_at": run["completed_at"],
                "evidence_status": "captured" if row["evidence_asin"] else "missing",
                "review_sample_count": len(
                    (row["evidence_fields"] or {}).get("top_review_samples", [])
                ),
                **review_values(
                    current_review,
                    digest,
                ),
            }
        )
    facets = {
        key: sorted({row[key] for row in observations if row.get(key)})
        for key in ("category", "brand", "material", "measurement_system")
    }
    scoped = [
        row for row in observations if not query.category or row["category"] == query.category
    ]
    if query.view == "latest":
        latest = {}
        for row in sorted(scoped, key=lambda x: (x["observed_at"], x["run_id"])):
            latest[(row["country"], row["asin"])] = row
        scoped = list(latest.values())
    # Apply value filters AFTER choosing the latest observation, never resurrect an old price.
    matching = []
    for row in scoped:
        state = row["review_state"]
        if (
            (query.review_status == "pending" and state == "已填寫")
            or (query.review_status == "entered" and state != "已填寫")
            or (query.review_status == "stale" and state != "資料變更，需重核")
        ):
            continue
        if query.q and normalize_text(query.q) not in normalize_text(
            f"{row['title']} {row['asin']} {row['brand'] or ''}"
        ):
            continue
        if any(
            getattr(query, key)
            and normalize_text(row.get(key) or "") != normalize_text(getattr(query, key))
            for key in ("brand", "material", "measurement_system")
        ):
            continue
        if query.min_price is not None and (row["price"] is None or row["price"] < query.min_price):
            continue
        if query.max_price is not None and (row["price"] is None or row["price"] > query.max_price):
            continue
        matching.append(row)
    matching.sort(key=lambda x: (x["observed_at"], x["run_id"], x["asin"]), reverse=True)
    if query.sort.startswith("price"):
        matching.sort(
            key=lambda x: (
                x["price"] is None,
                (x["price"] or Decimal(0)) * (-1 if query.sort == "price_desc" else 1),
            )
        )
    elif query.sort == "title":
        matching.sort(key=lambda x: normalize_text(x["title"]))
    resolved = query.model_copy(update={"snapshot": manifest, "review_snapshot": review_mark})
    pages = max(1, (len(matching) + query.page_size - 1) // query.page_size)
    page = min(query.page, pages)
    return {
        "query": resolved,
        "runs": selected_runs,
        "facets": facets,
        "rows": matching,
        "page_rows": matching[(page - 1) * query.page_size : page * query.page_size],
        "page": page,
        "pages": pages,
        "total": len(matching),
        "distinct_asins": len({x["asin"] for x in matching}),
        "observation_count": len(observations),
    }


def catalog_csv(data: dict) -> str:
    query = data["query"]
    # Scope travels with each data row so a spreadsheet can be traced without its filename.
    scope = {
        "export_source": query.source,
        "export_snapshot": query.snapshot,
        "export_review_snapshot": query.review_snapshot,
        "export_date_from_taipei": query.date_from or "",
        "export_date_to_taipei": query.date_to or "",
        "export_view": query.view,
        "export_date_basis": "batch_completed_at_Asia/Taipei",
        "export_filters": json.dumps(
            query.model_dump(mode="json", exclude={"columns", "page", "page_size"}),
            ensure_ascii=False,
        ),
    }
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=[*scope, *query.columns])
    writer.writeheader()
    for row in data["rows"]:
        writer.writerow(
            {
                key: safe_cell(value)
                for key, value in {**scope, **{key: row.get(key) for key in query.columns}}.items()
            }
        )
    return "\ufeff" + output.getvalue()
