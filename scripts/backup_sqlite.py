"""Create a verified local SQLite snapshot without copying a live WAL file."""

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import stat
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from gummy.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    url = make_url(Settings.from_env().database_url)
    if url.drivername != "sqlite" or url.database in {None, "", ":memory:"}:
        parser.error("This backup command requires a file-backed local SQLite database")
    source = Path(url.database).resolve(strict=True)
    if not source.is_file():
        parser.error("SQLite database file is missing")

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = (args.output or Path(f"data/backups/gummy-{timestamp}.sqlite3")).resolve()
    if output == source:
        parser.error("Backup output must differ from the live database")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat.S_IMODE(output.parent.stat().st_mode) & 0o077:
        parser.error("Backup directory must be private (mode 700)")
    if output.exists():
        parser.error("Backup output already exists")

    temporary = output.with_name(f".{output.name}.{secrets.token_hex(6)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        with (
            closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as live,
            closing(sqlite3.connect(temporary)) as snapshot,
        ):
            live.backup(snapshot)
            if snapshot.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("SQLite backup integrity check failed")
            if snapshot.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("SQLite backup foreign key check failed")
            counts = {
                table: snapshot.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "collection_runs",
                    "product_snapshots",
                    "source_observations",
                    "api_attempts",
                )
            }
        os.link(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)

    with output.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    result = {"output": str(output), "sha256": digest, "integrity": "ok", "counts": counts}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
