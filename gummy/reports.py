import csv
import io
from collections import defaultdict
from decimal import Decimal
from itertools import product

from gummy.classification import title_measurement_candidates
from gummy.models import normalize_text


def report_metadata(run: dict, rows: list[dict]) -> dict:
    return {
        "run_id": run["id"],
        "source": run["source"],
        "is_demo": run["source"] == "demo",
        "status": run["status"],
        "started_at": run["started_at"],
        "completed_at": run["completed_at"],
        "parameters": run["parameters"],
        "product_count": len(rows),
        "scope": "sampled_products_only",
        "currency": "USD",
        "sales_metric_available": False,
    }


def serialize_product(row: dict) -> dict:
    result = {
        key: str(value) if isinstance(value, Decimal) else value for key, value in row.items()
    }
    result["title_measurement_candidates"] = title_measurement_candidates(row["title"])
    result["title_measurement_evidence"] = (
        "unverified_title_keyword" if result["title_measurement_candidates"] else None
    )
    return result


def dimension_values(
    row: dict, key: str, include_title_candidates: bool
) -> tuple[list[str | None], bool]:
    structured = row["specs"].get(key)
    if structured:
        return [structured], False
    if key == "measurement_system" and include_title_candidates:
        candidates = title_measurement_candidates(row["title"])
        if candidates:
            return candidates, True
    return [None], False


def combinations(run: dict, rows: list[dict], query) -> dict:
    # Match dimensions against the SAME product snapshot. OR within a dimension, AND across them.
    matching = [
        row
        for row in rows
        if all(
            any(
                candidate in values
                for candidate in dimension_values(row, key, query.include_title_candidates)[0]
            )
            for key, values in query.filters.items()
        )
    ]
    groups = defaultdict(list)
    for row in matching:
        dimensions = [
            dimension_values(row, key, query.include_title_candidates) for key in query.group_by
        ]
        used_title = any(is_candidate for _, is_candidate in dimensions)
        for group_values in product(*(values for values, _ in dimensions)):
            groups[group_values].append((row, used_title))
    result = []
    for values, items in sorted(groups.items(), key=lambda pair: tuple(v or "" for v in pair[0])):
        prices = [row["price"] for row, _ in items if row["price"] is not None]
        result.append(
            {
                "specifications": dict(zip(query.group_by, values)),
                "product_count": len(items),
                "priced_product_count": len(prices),
                "average_price": str((sum(prices) / len(prices)).quantize(Decimal("0.01")))
                if prices
                else None,
                "currency": "USD",
                "asins": [row["asin"] for row, _ in items],
                "title_candidate_asins": [row["asin"] for row, inferred in items if inferred],
            }
        )
    return {
        "report": report_metadata(run, rows),
        "filters": query.filters,
        "include_title_candidates": query.include_title_candidates,
        "classification_note": (
            "Title candidates are unverified; one ASIN can appear in multiple groups."
            if query.include_title_candidates
            else "Structured product details only."
        ),
        "matched_product_count": len(matching),
        "groups": result,
    }


def safe_cell(value):
    if value is None:
        return ""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


PRODUCT_EXPORT_FIELDS = [
    "run_id",
    "source",
    "is_demo",
    "category",
    "query",
    "country",
    "asin",
    "title",
    "brand",
    "price",
    "currency",
    "rating",
    "rating_count",
    "material",
    "measurement_system",
    "title_measurement_candidates",
    "title_measurement_evidence",
    "size",
    "color",
    "observed_at",
    "warnings",
]


def product_export_values(run: dict, row: dict) -> dict:
    """Flatten one snapshot before format-specific spreadsheet escaping."""
    title_candidates = title_measurement_candidates(row["title"])
    values = {
        **row,
        **row["specs"],
        "source": run["source"],
        "is_demo": run["source"] == "demo",
        "category": run["parameters"]["category"],
        "query": run["parameters"]["query"],
        "country": run["parameters"]["country"],
        "warnings": ";".join(row["warnings"]),
        "title_measurement_candidates": ";".join(title_candidates),
        "title_measurement_evidence": ("unverified_title_keyword" if title_candidates else None),
    }
    return {key: values.get(key) for key in PRODUCT_EXPORT_FIELDS}


def export_csv(run: dict, rows: list[dict]) -> str:
    output = io.StringIO(newline="")
    fields = PRODUCT_EXPORT_FIELDS
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        values = product_export_values(run, row)
        writer.writerow({key: safe_cell(values.get(key)) for key in fields})
    # UTF-8 BOM lets common Excel installations recognize Traditional Chinese correctly.
    return "\ufeff" + output.getvalue()


def export_combinations_csv(run: dict, rows: list[dict], query) -> str:
    """Flatten actual same-ASIN groups for Excel without implying sales estimates."""
    grouped = combinations(run, rows, query)
    output = io.StringIO(newline="")
    fields = [
        "run_id",
        "source",
        "category",
        "query",
        "country",
        "observed_batch_completed_at_utc",
        "classification_mode",
        "matched_distinct_product_count",
        "material",
        "measurement_system",
        "size",
        "color",
        "group_product_count",
        "priced_product_count",
        "average_price_usd",
        "currency",
        "asins",
        "title_candidate_asins",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for group in grouped["groups"]:
        values = {
            "run_id": run["id"],
            "source": run["source"],
            "category": run["parameters"]["category"],
            "query": run["parameters"]["query"],
            "country": run["parameters"]["country"],
            "observed_batch_completed_at_utc": run["completed_at"],
            "classification_mode": (
                "title_candidates_opted_in" if query.include_title_candidates else "structured_only"
            ),
            "matched_distinct_product_count": grouped["matched_product_count"],
            **group["specifications"],
            "group_product_count": group["product_count"],
            "priced_product_count": group["priced_product_count"],
            "average_price_usd": group["average_price"],
            "currency": group["currency"],
            "asins": ";".join(group["asins"]),
            "title_candidate_asins": ";".join(group["title_candidate_asins"]),
        }
        writer.writerow({field: safe_cell(values.get(field)) for field in fields})
    return "\ufeff" + output.getvalue()


def _source_attribute(fields: dict, name: str) -> str | None:
    result = None
    for section in ("product_information", "product_details"):
        for key, value in fields.get(section, {}).items():
            if normalize_text(key) == name:
                result = value
    return result


def source_audit_rows(run: dict, rows: list[dict], evidence: dict[str, dict]) -> list[dict]:
    """Pair retained provider fields with the matching persisted product snapshot."""
    result = []
    for row in rows:
        source = evidence.get(row["asin"], {})
        values = {
            "run_id": run["id"],
            "source": run["source"],
            "asin": row["asin"],
            "observed_at": row["observed_at"],
            "evidence_status": "captured_selected_fields"
            if source
            else "not_recorded_for_this_run",
            "source_title": source.get("product_title"),
            "stored_title": row["title"],
            "source_price": source.get("product_price"),
            "stored_price_raw": row["price_raw"],
            "stored_price_usd": row["price"],
            "source_brand": _source_attribute(source, "brand"),
            "stored_brand": row["brand"],
            "source_material": _source_attribute(source, "material"),
            "stored_material": row["specs"].get("material"),
            "source_measurement_system": _source_attribute(source, "measurement system"),
            "stored_measurement_system": row["specs"].get("measurement_system"),
            "title_measurement_candidates_unverified": ";".join(
                title_measurement_candidates(row["title"])
            ),
            "source_size": _source_attribute(source, "size"),
            "stored_size": row["specs"].get("size"),
            "source_color": _source_attribute(source, "color"),
            "stored_color": row["specs"].get("color"),
            "source_rating": source.get("product_star_rating"),
            "stored_rating": row["rating"],
            "source_rating_count": source.get("product_num_ratings"),
            "stored_rating_count": row["rating_count"],
        }
        result.append(values)
    return result


def export_source_audit_csv(run: dict, rows: list[dict], evidence: dict[str, dict]) -> str:
    """Show provider-selected fields beside stored values for human review."""
    output = io.StringIO(newline="")
    fields = [
        "run_id",
        "source",
        "asin",
        "observed_at",
        "evidence_status",
        "source_title",
        "stored_title",
        "source_price",
        "stored_price_raw",
        "stored_price_usd",
        "source_brand",
        "stored_brand",
        "source_material",
        "stored_material",
        "source_measurement_system",
        "stored_measurement_system",
        "title_measurement_candidates_unverified",
        "source_size",
        "stored_size",
        "source_color",
        "stored_color",
        "source_rating",
        "stored_rating",
        "source_rating_count",
        "stored_rating_count",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for values in source_audit_rows(run, rows, evidence):
        writer.writerow({field: safe_cell(values.get(field)) for field in fields})
    return "\ufeff" + output.getvalue()
