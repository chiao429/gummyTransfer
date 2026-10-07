"""Latest-per-product sample statistics over persisted, frozen catalog scopes."""

import csv
import io
import json
from collections import defaultdict
from decimal import Decimal
from typing import Literal

from gummy.catalog import DEFAULT_COLUMNS, CatalogQuery, catalog_data
from gummy.reports import safe_cell

PRICE_BANDS = {
    "under10": "0 ≤ 價格 < 10",
    "10_20": "10 ≤ 價格 < 20",
    "20_50": "20 ≤ 價格 < 50",
    "50plus": "價格 ≥ 50",
    "missing": "價格未提供",
}
GROUP_LABELS = {
    "category_price_band": "分類 × 價格帶",
    "category": "搜尋分類",
    "brand": "品牌",
}


class AnalysisQuery(CatalogQuery):
    # Historical observations must never be summed into sample rating totals.
    view: Literal["latest"] = "latest"
    group_by: Literal["category_price_band", "category", "brand"] = "category_price_band"
    price_band: Literal["", "under10", "10_20", "20_50", "50plus", "missing"] = ""


def price_band(price: Decimal | None) -> str:
    if price is None:
        return "missing"
    for boundary, label in ((10, "under10"), (20, "10_20"), (50, "20_50")):
        if price < boundary:
            return label
    return "50plus"


def sample_metrics(rows: list[dict]) -> dict:
    prices = [row["price"] for row in rows if row["price"] is not None]
    counts = [row["rating_count"] for row in rows if row["rating_count"] is not None]
    return {
        "product_count": len(rows),
        "priced_product_count": len(prices),
        "average_price": (sum(prices) / len(prices)).quantize(Decimal("0.01")) if prices else None,
        "rating_count_product_count": len(counts),
        "known_rating_count_sum": sum(counts) if counts else None,
        "retained_review_sample_count": sum(row["review_sample_count"] for row in rows),
        "evidence_product_count": sum(row["evidence_status"] == "captured" for row in rows),
        "currency": "USD",
    }


def analysis_data(db, query: AnalysisQuery) -> dict:
    base = CatalogQuery.model_validate(query.model_dump(exclude={"group_by", "price_band"}))
    # Select the latest ASIN across the entire period FIRST. Category drill-down
    # must not resurrect an older observation from a different search category.
    data = catalog_data(db, base.model_copy(update={"category": ""}))
    rows = [
        row
        for row in data["rows"]
        if (not query.category or row["category"] == query.category)
        and (not query.price_band or price_band(row["price"]) == query.price_band)
    ]
    buckets = defaultdict(list)
    for row in rows:
        key = (
            (row["category"], price_band(row["price"]))
            if query.group_by == "category_price_band"
            else (row[query.group_by], None)
        )
        buckets[key].append(row)
    groups = []
    for (value, band), members in sorted(
        buckets.items(),
        key=lambda item: (
            item[0][0] or "",
            list(PRICE_BANDS).index(item[0][1]) if item[0][1] else 0,
        ),
    ):
        groups.append(
            {
                "group_value": value,
                "price_band": band,
                "price_band_label": PRICE_BANDS.get(band),
                **sample_metrics(members),
                "asins": [row["asin"] for row in members],
            }
        )
    resolved = query.model_copy(
        update={
            "snapshot": data["query"].snapshot,
            "review_snapshot": data["query"].review_snapshot,
        }
    )
    pages = max(1, (len(rows) + query.page_size - 1) // query.page_size)
    page = min(query.page, pages)
    return {
        **data,
        "query": resolved,
        "rows": rows,
        "page_rows": rows[(page - 1) * query.page_size : page * query.page_size],
        "page": page,
        "pages": pages,
        "total": len(rows),
        "distinct_asins": len({row["asin"] for row in rows}),
        "summary": sample_metrics(rows),
        "groups": groups,
        "latest_selection_basis": "latest_per_country_asin_in_period_then_value_filters",
        "sales_metric_available": False,
    }


def analysis_csv(data: dict, kind: Literal["groups", "products"]) -> str:
    query = data["query"]
    scope = {
        "export_source": query.source,
        "export_snapshot": query.snapshot,
        "export_review_snapshot": query.review_snapshot,
        "export_date_from_taipei": query.date_from or "",
        "export_date_to_taipei": query.date_to or "",
        "export_date_basis": "batch_completed_at_Asia/Taipei",
        "export_latest_selection_basis": data["latest_selection_basis"],
        "export_scope": "sampled_products_only",
        "export_filters": json.dumps(
            query.model_dump(mode="json", exclude={"columns", "page", "page_size"}),
            ensure_ascii=False,
        ),
    }
    fields = (
        [
            "group_value",
            "price_band",
            "price_band_label",
            *data["summary"],
            "asins",
        ]
        if kind == "groups"
        else [*DEFAULT_COLUMNS, "review_sample_count", "evidence_status", "review_state"]
    )
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=[*scope, *fields])
    writer.writeheader()
    for row in data["groups"] if kind == "groups" else data["rows"]:
        values = {key: row.get(key) for key in fields}
        if "asins" in values:
            values["asins"] = ";".join(values["asins"])
        writer.writerow({key: safe_cell(value) for key, value in {**scope, **values}.items()})
    return "\ufeff" + output.getvalue()
