"""Versioned local human judgments. Collection snapshots are never edited here."""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import (
    JSON,
    Column,
    ForeignKeyConstraint,
    Integer,
    String,
    Table,
    UniqueConstraint,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError, OperationalError

from gummy.db import metadata, products, runs, source_observations, utc_now
from gummy.errors import ReportNotFound
from gummy.quality import SampleLabel, sample_label_rows, specification_mapping
from gummy.verification import verify_snapshot

Judgment = Literal["待確認", "符合", "不符合", "無法確認"]
JUDGMENTS = ("待確認", "符合", "不符合", "無法確認")
REVIEW_FIELDS = [
    "review_state",
    "review_classification",
    "review_source",
    "review_note",
    "reviewer",
    "review_revision",
    "review_updated_at",
]

review_heads = Table(
    "human_review_heads",
    metadata,
    Column("run_id", Integer, primary_key=True),
    Column("asin", String(10), primary_key=True),
    Column("revision", Integer, nullable=False),
    ForeignKeyConstraint(["run_id", "asin"], [products.c.run_id, products.c.asin]),
)
review_events = Table(
    "human_review_events",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("run_id", Integer, nullable=False),
    Column("asin", String(10), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("classification", String(16), nullable=False),
    Column("source_check", String(16), nullable=False),
    Column("note", String(2000), nullable=False),
    Column("reviewer", String(80), nullable=False),
    Column("updated_at", String(32), nullable=False),
    ForeignKeyConstraint(["run_id", "asin"], [products.c.run_id, products.c.asin]),
    # IDs are export watermarks; SQLite must never recycle an ID after deletion.
    UniqueConstraint("run_id", "asin", "revision"),
    sqlite_autoincrement=True,
)

review_annotations = Table(
    "human_review_annotations",
    metadata,
    Column("run_id", Integer, primary_key=True),
    Column("asin", String(10), primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("sample_labels", JSON, nullable=False),
    ForeignKeyConstraint(
        ["run_id", "asin", "revision"],
        [review_events.c.run_id, review_events.c.asin, review_events.c.revision],
    ),
)


def migrate_reviews(engine):
    """Additive migration 001: create review storage; never alter collection tables."""
    metadata.create_all(engine, tables=[review_heads, review_events, review_annotations])


class ReviewEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    revision: int = Field(ge=0, le=2147483647)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    classification: Judgment
    source_check: Judgment
    reviewer: str = Field(min_length=1, max_length=80)
    note: str = Field(default="", max_length=2000)
    sample_labels: list[SampleLabel] | None = Field(default=None, max_length=8)

    @field_validator("sample_labels")
    @classmethod
    def unique_review_ids(cls, value):
        if value and len({item.review_id for item in value}) != len(value):
            raise ValueError("Duplicate review label")
        return value

    @field_validator("reviewer", "note")
    @classmethod
    def no_control_characters(cls, value):
        if any(ord(ch) < 32 and ch not in "\n\t" for ch in value):
            raise ValueError("Control characters are not allowed")
        return value


class ReviewConflict(Exception):
    pass


def fingerprint(run, product, evidence):
    # Only stable persisted fields; a catalog join's helper columns are excluded.
    value = {
        "source": run["source"],
        "parameters": run["parameters"],
        "product": {column.name: product[column.name] for column in products.columns},
        "evidence": evidence,
    }
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _subject(conn, source, run_id, asin):
    run = (
        conn.execute(
            select(runs).where(
                runs.c.id == run_id, runs.c.source == source, runs.c.status == "SUCCEEDED"
            )
        )
        .mappings()
        .first()
    )
    product = (
        conn.execute(select(products).where(products.c.run_id == run_id, products.c.asin == asin))
        .mappings()
        .first()
        if run
        else None
    )
    if product is None:
        raise ReportNotFound("找不到這個來源的成功批次商品")
    evidence = conn.execute(
        select(source_observations.c.fields).where(
            source_observations.c.run_id == run_id, source_observations.c.asin == asin
        )
    ).scalar_one_or_none()
    return dict(run), dict(product), evidence


def review_values(event, current_fingerprint):
    if event is None:
        return dict(zip(REVIEW_FIELDS, ["待核對", "待確認", "待確認", "", "", 0, ""]))
    stale = event["fingerprint"] != current_fingerprint
    done = event["classification"] != "待確認" and event["source_check"] != "待確認"
    return dict(
        zip(
            REVIEW_FIELDS,
            [
                "資料變更，需重核" if stale else "已填寫" if done else "核對中",
                event["classification"],
                event["source_check"],
                event["note"],
                event["reviewer"],
                event["revision"],
                event["updated_at"],
            ],
        )
    )


def review_map(db, run_ids, watermark=None):
    """Freeze the visible review versions as well as the successful run manifest."""
    with db.engine.connect() as conn:
        maximum = conn.execute(select(func.max(review_events.c.id))).scalar() or 0
        mark = min(maximum, watermark) if watermark is not None else maximum
        records = conn.execute(
            _events_statement()
            .where(review_events.c.run_id.in_(run_ids), review_events.c.id <= mark)
            .order_by(review_events.c.id)
        ).mappings()
        result = {(row["run_id"], row["asin"]): _event_values(row) for row in records}
    return mark, result


def review_subject(db, source, run_id, asin):
    with db.engine.connect() as conn:
        run, product, evidence = _subject(conn, source, run_id, asin)
        history = [
            _event_values(x)
            for x in conn.execute(
                _events_statement()
                .where(review_events.c.run_id == run_id, review_events.c.asin == asin)
                .order_by(review_events.c.revision.desc())
            ).mappings()
        ]
    digest = fingerprint(run, product, evidence)
    latest = history[0] if history else None
    verification = verify_snapshot(run, [product], {asin: evidence} if evidence else {})
    return {
        "run": run,
        "product": product,
        "evidence": evidence,
        "fingerprint": digest,
        "history": history,
        "latest": latest,
        "values": review_values(latest, digest),
        "verification": verification,
        "specification_mapping": specification_mapping(product, evidence),
        "review_samples": sample_label_rows(
            (evidence or {}).get("top_review_samples", []), latest, digest
        ),
    }


def _events_statement():
    return select(review_events, review_annotations.c.sample_labels).outerjoin(
        review_annotations,
        (review_events.c.run_id == review_annotations.c.run_id)
        & (review_events.c.asin == review_annotations.c.asin)
        & (review_events.c.revision == review_annotations.c.revision),
    )


def _event_values(row):
    return {**dict(row), "sample_labels": row["sample_labels"] or []}


def save_review(db, source, run_id, asin, edit: ReviewEdit):
    try:
        with db.engine.begin() as conn:
            run, product, evidence = _subject(conn, source, run_id, asin)
            digest = fingerprint(run, product, evidence)
            if digest != edit.fingerprint:
                raise ReviewConflict("來源或商品資料已變更，請重新核對後再存檔。")
            labels = edit.sample_labels
            if labels is None:
                previous = (
                    conn.execute(
                        _events_statement().where(
                            review_events.c.run_id == run_id,
                            review_events.c.asin == asin,
                            review_events.c.revision == edit.revision,
                        )
                    )
                    .mappings()
                    .first()
                )
                labels = (
                    [SampleLabel(**item) for item in (previous["sample_labels"] or [])]
                    if previous and previous["fingerprint"] == digest
                    else []
                )
            sample_ids = {x["review_id"] for x in (evidence or {}).get("top_review_samples", [])}
            if any(label.review_id not in sample_ids for label in labels):
                raise ValueError("評論標註不屬於此批次的留存樣本，請重新開啟核對頁。")
            label_values = [
                label.model_dump()
                for label in labels
                if label.theme != "待標註" or label.sentiment != "待標註" or label.note
            ]
            check = verify_snapshot(run, [product], {asin: evidence} if evidence else {})
            if edit.source_check == "符合" and check["status"] != "PASS":
                raise ValueError("留存來源缺失或自動對帳不一致，來源核對不能選擇「符合」。")
            try:
                with conn.begin_nested():
                    conn.execute(insert(review_heads).values(run_id=run_id, asin=asin, revision=0))
            except IntegrityError:
                pass
            changed = conn.execute(
                update(review_heads)
                .where(
                    review_heads.c.run_id == run_id,
                    review_heads.c.asin == asin,
                    review_heads.c.revision == edit.revision,
                )
                .values(revision=edit.revision + 1)
            )
            if changed.rowcount != 1:
                raise ReviewConflict("另一份表單已先存檔。你的輸入已保留，請比較最新結果再儲存。")
            conn.execute(
                insert(review_events).values(
                    run_id=run_id,
                    asin=asin,
                    **edit.model_dump(exclude={"revision", "sample_labels"}),
                    revision=edit.revision + 1,
                    updated_at=utc_now(),
                )
            )
            if label_values:
                conn.execute(
                    insert(review_annotations).values(
                        run_id=run_id,
                        asin=asin,
                        revision=edit.revision + 1,
                        sample_labels=label_values,
                    )
                )
    except OperationalError as exc:
        if db.engine.dialect.name == "sqlite" and "locked" in str(exc).lower():
            raise ReviewConflict("資料正在更新。你的輸入已保留，請確認最新結果後再存檔。") from None
        raise
    return edit.revision + 1
