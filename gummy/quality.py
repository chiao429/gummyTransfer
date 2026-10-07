"""Offline specification aliases and human labels, separate from immutable snapshots."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from gummy.models import normalize_text

MAPPING_VERSION = "spec-aliases-v1"
SPEC_LABELS = {
    "material": "材質",
    "measurement_system": "公制／英制",
    "size": "尺寸",
    "color": "顏色",
}
ALIASES = {
    "material": "material",
    "material type": "material",
    "measurement system": "measurement_system",
    "measurement standard": "measurement_system",
    "size": "size",
    "color": "color",
    "colour": "color",
}
THEMES = ("待標註", "品質與耐用", "尺寸與規格", "使用體驗", "價格與價值", "包裝與配送", "其他")
SENTIMENTS = ("待標註", "正向", "負向", "中性", "混合", "無法判斷")
Theme = Literal[
    "待標註", "品質與耐用", "尺寸與規格", "使用體驗", "價格與價值", "包裝與配送", "其他"
]
Sentiment = Literal["待標註", "正向", "負向", "中性", "混合", "無法判斷"]


class SampleLabel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    review_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,60}$")
    theme: Theme = "待標註"
    sentiment: Sentiment = "待標註"
    note: str = Field(default="", max_length=500)

    @field_validator("note")
    @classmethod
    def no_control_characters(cls, value):
        if any(ord(ch) < 32 and ch not in "\n\t" for ch in value):
            raise ValueError("Control characters are not allowed")
        return value


def canonical_value(dimension, raw):
    value = normalize_text(raw)
    if dimension == "measurement_system":
        return {
            "metric": "metric",
            "sae": "sae",
            "metric, sae": "metric;sae",
            "sae, metric": "metric;sae",
            "metric and sae": "metric;sae",
            "sae and metric": "metric;sae",
        }.get(value)
    if dimension == "material":
        return {
            "chrome-vanadium steel": "chrome vanadium steel",
            "cr-v steel": "chrome vanadium steel",
        }.get(value, value) or None
    return value or None


def specification_mapping(product, evidence):
    """Map exact source field names only; never infer from a title or overwrite DB."""
    rows = []
    for section in ("product_information", "product_details"):
        for key, raw in (evidence or {}).get(section, {}).items():
            dimension = ALIASES.get(normalize_text(key))
            if dimension is None:
                continue
            value = canonical_value(dimension, raw)
            stored = product["specs"].get(dimension)
            rows.append(
                {
                    "source_section": section,
                    "field_name": key,
                    "source_value": raw,
                    "dimension": dimension,
                    "candidate_value": value,
                    "stored_value": stored,
                    "mapping_version": MAPPING_VERSION,
                    "status": "未識別規格值"
                    if value is None
                    else "已入庫"
                    if value == stored
                    else "候選，待人工確認",
                }
            )
    for dimension in SPEC_LABELS:
        values = {
            r["candidate_value"]
            for r in rows
            if r["dimension"] == dimension and r["candidate_value"]
        }
        stored = product["specs"].get(dimension)
        if len(values) > 1 or (
            values and stored and canonical_value(dimension, stored) not in values
        ):
            for row in rows:
                if row["dimension"] == dimension:
                    row["status"] = "規格衝突，需核對"
    return rows


def sample_label_rows(samples, event, current_fingerprint):
    labels = {x["review_id"]: x for x in (event or {}).get("sample_labels", [])}
    result = []
    for sample in samples:
        label = labels.get(sample["review_id"], {})
        theme, sentiment = label.get("theme", "待標註"), label.get("sentiment", "待標註")
        entered = theme != "待標註" and sentiment != "待標註"
        stale = bool(label and event["fingerprint"] != current_fingerprint)
        state = (
            "資料變更，需重標"
            if stale
            else "已標註"
            if entered
            else "標註中"
            if label
            else "待標註"
        )
        result.append(
            {
                **sample,
                "theme": theme,
                "sentiment": sentiment,
                "annotation_note": label.get("note", ""),
                "annotation_state": state,
                "annotation_reviewer": event["reviewer"] if label else "",
                "annotation_revision": event["revision"] if label else 0,
                "annotation_updated_at": event["updated_at"] if label else "",
            }
        )
    return result
