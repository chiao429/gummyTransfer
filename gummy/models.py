import re
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from gummy.errors import SourceError

SourceName = Literal["demo", "rapidapi"]
DIMENSIONS = {"material", "measurement_system", "size", "color"}
SPEC_KEYS = {
    "material": "material",
    "measurement system": "measurement_system",
    "size": "size",
    "color": "color",
}
EVIDENCE_FIELDS = (
    "asin",
    "country",
    "currency",
    "product_title",
    "product_price",
    "product_star_rating",
    "product_num_ratings",
)


class CollectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source: SourceName = "demo"
    run_key: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    query: str = Field(default="socket wrench set", min_length=1, max_length=120)
    category: str = Field(default="套筒扳手", min_length=1, max_length=80)
    country: Literal["US"] = "US"
    max_products: int = Field(default=2, ge=1, le=2)


class CombinationQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source: SourceName = "demo"
    run_id: int | None = Field(default=None, gt=0)
    filters: dict[str, list[str]] = Field(default_factory=dict)
    group_by: list[str] = Field(default_factory=lambda: ["material", "measurement_system"])
    include_title_candidates: bool = Field(
        default=False,
        description="Use unverified Metric/SAE title keywords if structured data is absent",
    )

    @field_validator("filters")
    @classmethod
    def validate_filters(cls, value):
        if len(value) > len(DIMENSIONS) or set(value) - DIMENSIONS:
            raise ValueError("Unknown specification dimension")
        cleaned = {}
        for key, values in value.items():
            if not 1 <= len(values) <= 10:
                raise ValueError("Each filter needs 1 to 10 values")
            if any(not item.strip() or len(item) > 120 for item in values):
                raise ValueError("Invalid specification value")
            cleaned[key] = list(dict.fromkeys(normalize_text(item) for item in values))
        return cleaned

    @field_validator("group_by")
    @classmethod
    def validate_groups(cls, value):
        if not 1 <= len(value) <= 3 or len(set(value)) != len(value) or set(value) - DIMENSIONS:
            raise ValueError("group_by needs 1 to 3 distinct specification dimensions")
        return value


def normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


def source_evidence(raw: dict) -> dict:
    """Keep bounded product evidence, excluding reviewer identities and raw responses."""
    result = {}
    for field in EVIDENCE_FIELDS:
        value = raw.get(field)
        if value is not None and isinstance(value, (str, int, float, bool)):
            limit = 4000 if field == "product_title" else 120
            if len(str(value)) <= limit:
                result[field] = str(value)
    hint = raw.get("sales_volume")
    if isinstance(hint, str) and 0 < len(hint.strip()) <= 120:
        result["detail_sales_volume"] = hint.strip()
    distribution = raw.get("rating_distribution")
    if (
        isinstance(distribution, dict)
        and set(distribution) == {"1", "2", "3", "4", "5"}
        and all(type(value) is int and 0 <= value <= 100 for value in distribution.values())
        and 98 <= sum(distribution.values()) <= 102
    ):
        result["rating_distribution_percent"] = distribution
    reviews = raw.get("top_reviews")
    if isinstance(reviews, list):
        selected_reviews = []
        seen_ids = set()
        for review in reviews[:8]:
            if not isinstance(review, dict):
                continue
            reviewed_asin = review.get("reviewed_product_asin")
            if reviewed_asin is not None and reviewed_asin != raw.get("asin"):
                continue
            review_id = review.get("review_id")
            title = review.get("review_title")
            comment = review.get("review_comment")
            star = review.get("review_star_rating")
            date = review.get("review_date")
            if (
                not isinstance(review_id, str)
                or re.fullmatch(r"[A-Za-z0-9-]{1,60}", review_id) is None
                or review_id in seen_ids
                or not isinstance(title, str)
                or not 0 < len(title.strip()) <= 240
                or not isinstance(comment, str)
                or not 0 < len(comment.strip()) <= 2000
                or str(star) not in {"1", "2", "3", "4", "5"}
                or not isinstance(date, str)
                or not 0 < len(date.strip()) <= 120
            ):
                continue
            seen_ids.add(review_id)
            selected_reviews.append(
                {
                    "review_id": review_id,
                    "review_title": title.strip(),
                    "review_comment": comment.strip(),
                    "review_star_rating": str(star),
                    "review_date": date.strip(),
                }
            )
        if selected_reviews:
            result["top_review_samples"] = selected_reviews
    for section in ("product_information", "product_details"):
        values = raw.get(section)
        if not isinstance(values, dict):
            continue
        eligible = [
            (key, value)
            for key, value in values.items()
            if isinstance(key, str)
            and isinstance(value, str)
            and key.strip()
            and len(key) <= 120
            and len(value) <= 120
        ]
        mapped = {*SPEC_KEYS, "brand"}
        selected = [item for item in eligible if normalize_text(item[0]) in mapped]
        additional = [item for item in eligible if normalize_text(item[0]) not in mapped]
        result[section] = dict((selected + additional)[:64])
    return result


def valid_asin(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Z0-9]{10}", value) is not None


def parse_price(value) -> tuple[Decimal | None, str | None]:
    if value is None or value == "":
        return None, "missing_price"
    # MVP supports US/USD only. Other locale formats must not be silently misread.
    raw = str(value).strip()
    if not re.fullmatch(r"\$?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?", raw):
        return None, "unparsed_price"
    try:
        amount = Decimal(raw.removeprefix("$").replace(",", ""))
        if amount > Decimal("99999999.99"):
            return None, "unparsed_price"
        return amount.quantize(Decimal("0.01")), None
    except InvalidOperation:
        return None, "unparsed_price"


def normalize_product(raw: dict, expected_asin: str, country: str) -> dict:
    if not valid_asin(raw.get("asin")) or raw["asin"] != expected_asin:
        raise SourceError("Product detail ASIN does not match the requested product")
    title = raw.get("product_title")
    if not isinstance(title, str) or not title.strip() or len(title) > 4000:
        raise SourceError("Product detail has no valid title")
    if raw.get("country", country) != country or raw.get("currency") != "USD":
        raise SourceError("MVP expects US marketplace and explicit USD currency")
    attrs = {}
    for field in ("product_information", "product_details"):
        values = raw.get(field) or {}
        if not isinstance(values, dict):
            raise SourceError("Product attributes must be objects")
        for key, value in values.items():
            if isinstance(key, str) and isinstance(value, str) and len(value) <= 120:
                attrs[normalize_text(key)] = value.strip()
    specs = {
        dimension: normalize_text(attrs[key])
        for key, dimension in SPEC_KEYS.items()
        if attrs.get(key)
    }
    price, warning = parse_price(raw.get("product_price"))
    rating = None
    raw_rating = raw.get("product_star_rating")
    try:
        parsed = Decimal(str(raw_rating))
        if parsed.is_finite() and 0 <= parsed <= 5:
            rating = parsed.quantize(Decimal("0.1"))
    except InvalidOperation:
        pass
    count = raw.get("product_num_ratings")
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 2147483647:
        count = None
    warnings = [warning] if warning else []
    if not specs:
        warnings.append("missing_specs")
    return {
        "asin": expected_asin,
        "title": title.strip(),
        "brand": attrs.get("brand") or None,
        "price": price,
        "price_raw": str(raw.get("product_price"))[:120]
        if raw.get("product_price") is not None
        else None,
        "currency": "USD",
        "rating": rating,
        "rating_count": count,
        "specs": specs,
        "warnings": warnings,
    }
