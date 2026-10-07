"""Bounded, read-only inspection of manual fields in a local portfolio XLSX."""

import posixpath
import re
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
MAX_WORKBOOK_BYTES = 10 * 1024 * 1024
MAX_MEMBER_BYTES = 2 * 1024 * 1024
MAX_ZIP_ENTRIES = 200
MAX_REVIEW_ROWS = 1000


def _xml_member(archive: ZipFile, name: str) -> ET.Element:
    entry = archive.getinfo(name)
    if entry.file_size > MAX_MEMBER_BYTES:
        raise ValueError("Workbook XML is too large")
    content = archive.read(name)
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise ValueError("Workbook XML declarations are not allowed")
    return ET.fromstring(content)


def _index_sheet_path(archive: ZipFile) -> str:
    workbook = _xml_member(archive, "xl/workbook.xml")
    sheet = next(
        (
            item
            for item in workbook.findall(f".//{{{MAIN_NS}}}sheet")
            if item.get("name") == "對帳索引"
        ),
        None,
    )
    if sheet is None:
        raise ValueError("Review worksheet is missing")
    relationship_id = sheet.get(f"{{{OFFICE_REL_NS}}}id")
    relationships = _xml_member(archive, "xl/_rels/workbook.xml.rels")
    relationship = next(
        (
            item
            for item in relationships.findall(f"{{{PACKAGE_REL_NS}}}Relationship")
            if item.get("Id") == relationship_id
        ),
        None,
    )
    if relationship is None or not relationship.get("Type", "").endswith("/worksheet"):
        raise ValueError("Review worksheet relationship is invalid")
    target = relationship.get("Target", "")
    name = (
        target.lstrip("/")
        if target.startswith("/")
        else posixpath.normpath(posixpath.join("xl", target))
    )
    if not re.fullmatch(r"xl/worksheets/[^/]+\.xml", name):
        raise ValueError("Review worksheet path is invalid")
    return name


def _shared_strings(archive: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _xml_member(archive, "xl/sharedStrings.xml")
    return [
        "".join(node.text or "" for node in item.findall(f".//{{{MAIN_NS}}}t"))
        for item in root.findall(f"{{{MAIN_NS}}}si")
    ]


def _cell_value(cell: ET.Element, shared: list[str]) -> str:
    if cell.find(f"{{{MAIN_NS}}}f") is not None:
        raise ValueError("Review worksheet contains a formula")
    kind = cell.get("t")
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(f".//{{{MAIN_NS}}}t"))
    value = cell.find(f"{{{MAIN_NS}}}v")
    raw = value.text if value is not None and value.text is not None else ""
    if kind == "s":
        index = int(raw)
        if not 0 <= index < len(shared):
            raise ValueError("Shared-string index is invalid")
        return shared[index]
    return raw


def read_review_progress(path: Path, expected_keys: set[tuple[int, str]]) -> dict:
    """Count entered K/L/M values; this never validates the human conclusions."""
    try:
        if not path.is_file() or path.stat().st_size > MAX_WORKBOOK_BYTES:
            raise ValueError("Workbook is missing or too large")
        with ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) > MAX_ZIP_ENTRIES or len(names) != len(set(names)):
                raise ValueError("Workbook contains too many files")
            root = _xml_member(archive, _index_sheet_path(archive))
            shared = _shared_strings(archive)
        if root.find(f".//{{{MAIN_NS}}}f") is not None:
            raise ValueError("Review worksheet contains a formula")
        found = set()
        classification_entered = source_entered = both_entered = notes_entered = 0
        for row in root.findall(f".//{{{MAIN_NS}}}sheetData/{{{MAIN_NS}}}row"):
            row_number = int(row.get("r", "0"))
            if row_number <= 3:
                continue
            values = {}
            for cell in row.findall(f"{{{MAIN_NS}}}c"):
                ref = cell.get("r", "")
                match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", ref)
                if match is None or int(match.group(2)) != row_number:
                    raise ValueError("Review worksheet cell reference is invalid")
                value = _cell_value(cell, shared)
                column = match.group(1)
                if column in {"A", "C", "K", "L", "M"}:
                    if column in values:
                        raise ValueError("Duplicate review cell")
                    values[column] = value.strip()
            if not any(values.values()):
                continue
            if len(found) >= MAX_REVIEW_ROWS:
                raise ValueError("Too many review rows")
            run_text, asin = values.get("A", ""), values.get("C", "")
            if not re.fullmatch(r"[1-9][0-9]*", run_text) or not re.fullmatch(
                r"[A-Z0-9]{10}", asin
            ):
                raise ValueError("Review row identity is invalid")
            key = (int(run_text), asin)
            if key not in expected_keys or key in found:
                raise ValueError("Review row does not match the database")
            found.add(key)
            classified = values.get("K", "") not in {"", "待確認"}
            source_checked = values.get("L", "") not in {"", "待確認"}
            classification_entered += classified
            source_entered += source_checked
            both_entered += classified and source_checked
            notes_entered += bool(values.get("M", ""))
        if found != expected_keys:
            raise ValueError("Review worksheet is missing database products")
        return {
            "status": "READ",
            "total": len(expected_keys),
            "classification_entered": classification_entered,
            "source_entered": source_entered,
            "both_entered": both_entered,
            "notes_entered": notes_entered,
            "pending": len(expected_keys) - both_entered,
        }
    except (
        OSError,
        BadZipFile,
        ET.ParseError,
        KeyError,
        IndexError,
        ValueError,
        OverflowError,
        RuntimeError,
        EOFError,
        zlib.error,
    ):
        return {"status": "UNAVAILABLE"}
