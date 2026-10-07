"""A portable, source-labelled payload for the local multi-sheet workbook."""

import csv
import io
from collections import defaultdict
from decimal import Decimal

from gummy.classification import title_measurement_candidates
from gummy.models import SPEC_KEYS, CombinationQuery, normalize_text
from gummy.quality import MAPPING_VERSION, sample_label_rows, specification_mapping
from gummy.reports import (
    export_combinations_csv,
    product_export_values,
    report_metadata,
    source_audit_rows,
)
from gummy.verification import verify_snapshot


def attach_web_reviews(db, payload: dict, watermark: int | None = None) -> dict:
    """Optional independent web-review sheet; legacy Excel manual cells stay intact."""
    from gummy.reviews import fingerprint, review_map, review_values

    run_ids = sorted({item["run_id"] for item in payload["observations"]})
    mark, events = review_map(db, run_ids, watermark)
    payload["web_review_snapshot"] = mark
    if not events:
        return payload
    subjects = {}
    for run_id in run_ids:
        run, rows = db.report(payload["source"], run_id)
        evidence = db.evidence(run_id)
        for row in rows:
            subjects[(run_id, row["asin"])] = fingerprint(run, row, evidence.get(row["asin"]))
    payload["web_reviews"] = [
        {
            "run_id": item["run_id"],
            "asin": item["asin"],
            "category": item["category"],
            **review_values(
                events.get((item["run_id"], item["asin"])), subjects[(item["run_id"], item["asin"])]
            ),
        }
        for item in payload["observations"]
    ]
    if payload.get("workbook_schema_version") == 2:
        # Only reconstruct historical v2 reports. Current exports use source reviews unchanged.
        payload["review_samples"] = [
            sample_label_rows(
                [sample],
                events.get((sample["run_id"], sample["asin"])),
                subjects[(sample["run_id"], sample["asin"])],
            )[0]
            for sample in payload.get("review_samples", [])
        ]
    return payload


def _csv_rows(content: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(content.lstrip("\ufeff"))))


def _plain_values(values: dict) -> dict[str, str]:
    """Keep raw text for XLSX; the workbook writer escapes formula prefixes."""
    return {key: "" if value is None else str(value) for key, value in values.items()}


def portfolio_combinations(observations: list[dict], coverage: dict) -> list[dict]:
    """Group sampled observations by available specs, keeping title guesses visible."""
    grouped = defaultdict(list)
    for item in observations:
        structured = item["measurement_system"]
        candidates = [value for value in item["title_measurement_candidates"].split(";") if value]
        measurements = (
            [(structured, False)]
            if structured
            else [(value, True) for value in candidates]
            if candidates
            else [("", False)]
        )
        for measurement, is_candidate in measurements:
            key = (
                item["category"],
                item["material"],
                measurement,
                item["size"],
                item["color"],
            )
            grouped[key].append((item, is_candidate))

    categories = coverage["categories"] + coverage["outside_reference"]
    category_order = {item["category"]: index for index, item in enumerate(categories)}
    result = []
    for key, members in sorted(
        grouped.items(), key=lambda pair: (category_order[pair[0][0]], pair[0][1:])
    ):
        prices = [Decimal(item["price"]) for item, _ in members if item["price"]]
        result.append(
            {
                "category": key[0],
                "material": key[1],
                "measurement_system": key[2],
                "size": key[3],
                "color": key[4],
                "observation_count": len(members),
                "distinct_asin_count": len({item["asin"] for item, _ in members}),
                "priced_observation_count": len(prices),
                "average_price_usd": str((sum(prices) / len(prices)).quantize(Decimal("0.01")))
                if prices
                else None,
                "title_candidate_observation_count": sum(guessed for _, guessed in members),
                "source_evidence_observation_count": sum(
                    item["evidence_status"] == "captured_selected_fields" for item, _ in members
                ),
                "source_pass_observation_count": sum(
                    item["run_verification"] == "PASS" for item, _ in members
                ),
                "asins": sorted({item["asin"] for item, _ in members}),
                "run_asins": [f"{item['run_id']}:{item['asin']}" for item, _ in members],
            }
        )
    return result


def workbook_payload(
    run: dict, rows: list[dict], evidence: dict[str, dict], coverage: dict
) -> dict:
    query = CombinationQuery(source=run["source"], run_id=run["id"], include_title_candidates=True)
    details = [_plain_values(product_export_values(run, row)) for row in rows]
    groups = _csv_rows(export_combinations_csv(run, rows, query))
    audit = [_plain_values(item) for item in source_audit_rows(run, rows, evidence)]
    specs = []
    for row in rows:
        for dimension in ("material", "measurement_system", "size", "color"):
            structured = row["specs"].get(dimension)
            if structured:
                specs.append(
                    {
                        "category": run["parameters"]["category"],
                        "asin": row["asin"],
                        "dimension": dimension,
                        "value": structured,
                        "evidence": "structured_product_detail",
                    }
                )
            elif dimension == "measurement_system":
                for candidate in title_measurement_candidates(row["title"]):
                    specs.append(
                        {
                            "category": run["parameters"]["category"],
                            "asin": row["asin"],
                            "dimension": dimension,
                            "value": candidate,
                            "evidence": "unverified_title_keyword",
                        }
                    )
    return {
        "report": report_metadata(run, rows),
        "details": details,
        "groups": groups,
        "specifications": specs,
        "audit": audit,
        "coverage": coverage,
        "limits": {
            "scope": "search_first_page_sample_only",
            "sales_metric_available": False,
            "group_counts_additive": False,
            "original_reference_tabs_rebuilt": False,
        },
    }


def portfolio_payload(snapshots: list[tuple[dict, list[dict], dict]], coverage: dict) -> dict:
    """Combine immutable successful snapshots without merging repeated observations."""
    if not snapshots:
        raise ValueError("Portfolio requires at least one successful snapshot")
    observations = []
    additional_attributes = []
    mappings = []
    review_samples = []
    for run, rows, evidence in snapshots:
        details = [_plain_values(product_export_values(run, row)) for row in rows]
        audits = {
            item["asin"]: _plain_values(item) for item in source_audit_rows(run, rows, evidence)
        }
        verification = verify_snapshot(run, rows, evidence)
        issue_count = {}
        for issue in verification["issues"]:
            if issue["asin"]:
                issue_count[issue["asin"]] = issue_count.get(issue["asin"], 0) + 1
        for detail in details:
            audit = audits[detail["asin"]]
            source_fields = evidence.get(detail["asin"], {})
            identity = {
                "run_id": run["id"],
                "category": run["parameters"]["category"],
                "asin": detail["asin"],
                "observed_at": detail["observed_at"],
            }
            product = next(row for row in rows if row["asin"] == detail["asin"])
            mappings.extend(
                {**identity, **item} for item in specification_mapping(product, source_fields)
            )
            review_samples.extend(
                {**identity, **item} for item in source_fields.get("top_review_samples", [])
            )
            for field, section in (
                ("search_sales_volume", "search_listing"),
                ("detail_sales_volume", "product_summary"),
            ):
                if source_fields.get(field):
                    additional_attributes.append(
                        {
                            "run_id": run["id"],
                            "category": run["parameters"]["category"],
                            "asin": detail["asin"],
                            "observed_at": detail["observed_at"],
                            "source_section": section,
                            "field_name": "sales_volume",
                            "source_value": source_fields[field],
                        }
                    )
            for section in ("product_information", "product_details"):
                for key, value in source_fields.get(section, {}).items():
                    if normalize_text(key) in {*SPEC_KEYS, "brand"}:
                        continue
                    additional_attributes.append(
                        {
                            "run_id": run["id"],
                            "category": run["parameters"]["category"],
                            "asin": detail["asin"],
                            "observed_at": detail["observed_at"],
                            "source_section": section,
                            "field_name": key,
                            "source_value": value,
                        }
                    )
            for star, percent in source_fields.get("rating_distribution_percent", {}).items():
                additional_attributes.append(
                    {
                        "run_id": run["id"],
                        "category": run["parameters"]["category"],
                        "asin": detail["asin"],
                        "observed_at": detail["observed_at"],
                        "source_section": "product_summary",
                        "field_name": f"rating_distribution_{star}_star_percent",
                        "source_value": str(percent),
                    }
                )
            for index, review in enumerate(source_fields.get("top_review_samples", []), start=1):
                for field in (
                    "review_id",
                    "review_title",
                    "review_comment",
                    "review_star_rating",
                    "review_date",
                ):
                    additional_attributes.append(
                        {
                            "run_id": run["id"],
                            "category": run["parameters"]["category"],
                            "asin": detail["asin"],
                            "observed_at": detail["observed_at"],
                            "source_section": "top_review_sample",
                            "field_name": f"{index:02d}_{field}",
                            "source_value": review[field],
                        }
                    )
            observations.append(
                {
                    **detail,
                    "run_id": run["id"],
                    "source_title": audit["source_title"],
                    "source_price": audit["source_price"],
                    "evidence_status": audit["evidence_status"],
                    "run_verification": verification["status"],
                    "product_issue_count": issue_count.get(detail["asin"], 0),
                    "source_audit": audit,
                }
            )
    return {
        "source": coverage["source"],
        "workbook_schema_version": 3,
        "through_run_id": snapshots[-1][0]["id"],
        "run_count": len(snapshots),
        "observation_count": len(observations),
        "distinct_asin_count": len({item["asin"] for item in observations}),
        "coverage": coverage,
        "combinations": portfolio_combinations(observations, coverage),
        "observations": observations,
        "additional_attributes": additional_attributes,
        "specification_mapping_version": MAPPING_VERSION,
        "specification_mappings": mappings,
        "review_samples": review_samples,
        "limits": {
            "scope": "successful_snapshots_through_run_only",
            "repeated_asin_is_separate_observation": True,
            "sales_metric_available": False,
            "title_measurement_candidates_verified": False,
        },
    }
