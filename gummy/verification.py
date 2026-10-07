"""Offline checks for a persisted collection snapshot and its source evidence."""

from decimal import Decimal, InvalidOperation

from gummy.models import SPEC_KEYS, normalize_text, parse_price


def _attribute(fields: dict, name: str) -> str | None:
    value = None
    for section in ("product_information", "product_details"):
        for key, candidate in fields.get(section, {}).items():
            if normalize_text(key) == name:
                value = candidate.strip()
    return value


def _rating(value) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
        if parsed.is_finite() and 0 <= parsed <= 5:
            return parsed.quantize(Decimal("0.1"))
    except (InvalidOperation, TypeError):
        pass
    return None


def _rating_count(value) -> int | None:
    try:
        parsed = int(value)
        if str(parsed) == str(value) and 0 <= parsed <= 2147483647:
            return parsed
    except (ValueError, TypeError):
        pass
    return None


def verify_snapshot(run: dict, rows: list[dict], evidence: dict[str, dict]) -> dict:
    issues = []
    if not rows:
        issues.append({"asin": None, "field": "products", "code": "empty_snapshot"})
    for asin in evidence.keys() - {row["asin"] for row in rows}:
        issues.append({"asin": asin, "field": "evidence", "code": "unmatched_source_evidence"})
    for row in rows:
        asin = row["asin"]
        source = evidence.get(asin)
        if source is None:
            issues.append({"asin": asin, "field": "evidence", "code": "missing_source_evidence"})
            continue

        def check(field: str, actual, expected):
            if actual != expected:
                issues.append({"asin": asin, "field": field, "code": "source_storage_mismatch"})

        check("asin", source.get("asin"), asin)
        check("country", source.get("country"), run["parameters"]["country"])
        check("title", (source.get("product_title") or "").strip(), row["title"])
        check("currency", source.get("currency"), row["currency"])
        raw_price = source.get("product_price")
        check("price_raw", raw_price, row["price_raw"])
        check("price", parse_price(raw_price)[0], row["price"])
        check("brand", _attribute(source, "brand") or None, row["brand"])
        for key, dimension in SPEC_KEYS.items():
            raw_value = _attribute(source, key)
            check(
                dimension,
                normalize_text(raw_value) if raw_value else None,
                row["specs"].get(dimension),
            )
        check("rating", _rating(source.get("product_star_rating")), row["rating"])
        check("rating_count", _rating_count(source.get("product_num_ratings")), row["rating_count"])

    missing = any(item["code"] == "missing_source_evidence" for item in issues)
    other_issue = any(item["code"] != "missing_source_evidence" for item in issues)
    status = "FAIL" if other_issue else "INCOMPLETE" if missing else "PASS"
    return {
        "run_id": run["id"],
        "source": run["source"],
        "status": status,
        "product_count": len(rows),
        "evidence_count": len(evidence),
        "issues": issues,
    }
