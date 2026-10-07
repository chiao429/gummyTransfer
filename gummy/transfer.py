"""Offline, verified project handover. Never invokes a supplier or starts a worker."""

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import zipfile
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from sqlalchemy.engine import make_url

from gummy.locking import process_lock

MANIFEST = "transfer-manifest.json"
PREPARED = "data/transfer-prepared.json"
DATABASE = "data/gummy.db"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def publish_new(path, data):
    """Publish a private complete file without overwriting an existing destination."""
    fd, temporary = tempfile.mkstemp(prefix=".gummy-transfer-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def inside(root, name):
    """Reject traversal and symlinked ancestors, including on the receiving machine."""
    rel = PurePosixPath(name)
    if (
        not name
        or rel.is_absolute()
        or ".." in rel.parts
        or "\\" in name
        or ":" in name
        or rel.as_posix() != name
    ):
        raise ValueError("交接路徑不安全")
    path = root
    for part in rel.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("交接檔案不可使用符號連結")
    return path


def local_database(settings):
    url = make_url(settings.database_url)
    if url.drivername != "sqlite" or url.database in {None, "", ":memory:"}:
        raise ValueError("目前交接工具僅支援本機 SQLite；PostgreSQL 需另做資料庫備份")
    path = Path(url.database).absolute()
    if path.is_symlink() or not path.is_file():
        raise ValueError("找不到安全的 SQLite 資料庫檔案")
    return path.resolve()


def lock_database(path):
    # process_lock only needs this URL for SQLite; do not initialize/mutate the source DB.
    return SimpleNamespace(engine=SimpleNamespace(url=make_url(f"sqlite:///{path}")))


def idle(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, statuses in (
        ("operation_tasks", ("QUEUED", "RUNNING")),
        ("collection_runs", ("RUNNING",)),
    ):
        if table in tables:
            placeholders = ",".join("?" for _ in statuses)
            if conn.execute(
                f"SELECT 1 FROM {table} WHERE status IN ({placeholders}) LIMIT 1", statuses
            ).fetchone():
                raise ValueError("仍有待執行或進行中的工作；請先完成或取消，再打包／準備")


def inspect_database(conn):
    if conn.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise ValueError("資料庫完整性檢查失敗")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("資料庫關聯檢查失敗")
    schema = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    result = {"_schema": {"count": len(schema), "sha256": digest(json.dumps(schema).encode())}}
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for (name,) in tables:
        quoted = '"' + name.replace('"', '""') + '"'
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({quoted})")]
        rows = []
        for row in conn.execute(f"SELECT * FROM {quoted}"):
            values = [v.hex() if isinstance(v, bytes) else v for v in row]
            if name == "operation_reports":
                # Relocation may change this field only; all historical values stay fixed.
                values[columns.index("path")] = "<relocated>"
            rows.append(json.dumps(values, ensure_ascii=False, separators=(",", ":")))
        result[name] = {"count": len(rows), "sha256": digest("\n".join(sorted(rows)).encode())}
    return result


def report_paths(conn):
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operation_reports'"
    ).fetchone():
        return []
    return conn.execute("SELECT task_id, path, sha256 FROM operation_reports").fetchall()


def report_name(task_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task_id):
        raise ValueError("報表識別不安全")
    return f"exports/operations/{task_id}.xlsx"


def safe_environment(settings, plan, scheduler):
    return (
        "# 交接設定：金鑰未搬移，尚未啟動任何採集程序。\n"
        f"DATABASE_URL=sqlite:///./{DATABASE}\nAPP_ENV=local\n"
        "RAPIDAPI_KEY=\nRAPIDAPI_FREE_PLAN_CONFIRMED=false\nGUMMY_API_TOKEN=\n"
        f"LIVE_TOTAL_LIMIT={settings.total_limit}\nLIVE_DAILY_LIMIT={settings.daily_limit}\n"
        f"GUMMY_ACTIVE_PLAN={plan}\nGUMMY_SCHEDULER_CONFIG={scheduler}\n"
    ).encode()


def package_transfer(root, settings, output=None):
    root = Path(root).resolve(strict=True)
    source = local_database(settings)
    if not source.is_relative_to(root):
        raise ValueError("請先將資料庫置於專案內再交接")
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output = Path(output or root / "transfer" / f"gummy-transfer-{timestamp}.zip").absolute()
    if output.is_symlink() or output.exists():
        raise ValueError("交接包已存在；請使用新檔名")
    if any(p.is_symlink() for p in output.parents):
        raise ValueError("輸出資料夾不可使用符號連結")
    output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    files = {}
    executable = set()

    def add(name):
        path = inside(root, name)
        if not path.is_file():
            raise ValueError(f"交接所需檔案不存在：{name}")
        files[name] = path.read_bytes()
        if path.stat().st_mode & 0o100:
            executable.add(name)

    def relative(value):
        if not value:
            return ""
        path = Path(value)
        path = path if path.is_absolute() else root / path
        name = path.absolute().relative_to(root).as_posix()
        if Path(name).suffix != ".json" or not re.fullmatch(r"[A-Za-z0-9_./-]+", name):
            raise ValueError("排程／計畫必須是專案內 JSON 檔案")
        add(name)
        return name

    db_lock = lock_database(source)
    with ExitStack() as locks:
        for purpose in ("admission", "schedule-control", "collection"):
            locks.enter_context(process_lock(db_lock, purpose))
        with tempfile.TemporaryDirectory() as temporary:
            snapshot_path = Path(temporary) / "snapshot.db"
            with (
                closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as live,
                closing(sqlite3.connect(snapshot_path)) as snapshot,
            ):
                idle(live)
                live.backup(snapshot)
                before = inspect_database(snapshot)
                plan = relative(settings.active_plan_path)
                scheduler = relative(settings.scheduler_config_path)
                if scheduler:
                    from gummy.python_scheduler import load_scheduler

                    if Path(scheduler).parent != Path("."):
                        raise ValueError("目前交接要求排程設定 JSON 位於專案根目錄")
                    load_scheduler(inside(root, scheduler))
                    config = json.loads(files[scheduler])
                    for key in ("plan", "initial_review_workbook"):
                        if config.get(key):
                            suffix = ".json" if key == "plan" else ".xlsx"
                            if Path(config[key]).suffix != suffix:
                                raise ValueError("排程來源檔案類型不安全")
                            add(config[key])
                    state = config.get("state_file", "data/python-scheduler-state.json")
                    if state and inside(root, state).exists():
                        if Path(state).suffix != ".json":
                            raise ValueError("排程狀態檔案必須是 JSON")
                        add(state)
                for task_id, old_path, sha in report_paths(snapshot):
                    name = report_name(task_id)
                    if Path(old_path).absolute() != inside(root, name).absolute():
                        raise ValueError("已登錄報表路徑異常；請先修復，不能直接搬移")
                    add(name)
                    if digest(files[name]) != sha:
                        raise ValueError("已登錄報表內容與資料庫雜湊不一致")
                    add(name + ".verified.json")
                    snapshot.execute(
                        "UPDATE operation_reports SET path=? WHERE task_id=?", (name, task_id)
                    )
                snapshot.commit()
                if inspect_database(snapshot) != before:
                    raise ValueError("交接快照意外改變歷史資料")
            files[DATABASE] = snapshot_path.read_bytes()
            for name in ("README.md", "pyproject.toml", "requirements.lock.txt", ".env.example"):
                add(name)
            for directory, extensions in (
                ("gummy", {".py"}),
                ("scripts", {".py", ".mjs", ".sh"}),
                ("docs", {".md", ".json", ".png", ".jpg", ".svg"}),
                ("plans", {".json"}),
                ("exports", {".xlsx", ".csv", ".json"}),
            ):
                folder = inside(root, directory)
                if folder.exists():
                    for path in sorted(folder.rglob("*")):
                        if path.suffix in extensions and "__pycache__" not in path.parts:
                            add(path.relative_to(root).as_posix())
            files["transfer.env"] = safe_environment(settings, plan, scheduler)
            for secret in (settings.rapidapi_key, settings.api_token):
                if secret and any(secret.encode() in data for data in files.values()):
                    raise ValueError("交接內容包含目前金鑰；已拒絕打包，請檢查來源文件")
            manifest = {
                "schema_version": 1,
                "created_at": datetime.now(UTC).isoformat(),
                "database": DATABASE,
                "tables": before,
                "files": {name: digest(data) for name, data in sorted(files.items())},
                "credentials_included": False,
                "services_started": False,
            }
            files[MANIFEST] = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
            # A private temporary ZIP is published exclusively after read-back verification.
            fd, temporary_zip = tempfile.mkstemp(prefix=".gummy-transfer-", dir=output.parent)
            os.close(fd)
            temporary_zip = Path(temporary_zip)
            try:
                with zipfile.ZipFile(temporary_zip, "w", zipfile.ZIP_DEFLATED) as archive:
                    for name, data in sorted(files.items()):
                        entry = zipfile.ZipInfo(name)
                        entry.external_attr = (0o100700 if name in executable else 0o100600) << 16
                        entry.compress_type = zipfile.ZIP_DEFLATED
                        archive.writestr(entry, data)
                with zipfile.ZipFile(temporary_zip) as archive:
                    if archive.testzip() is not None or any(
                        digest(archive.read(name)) != sha for name, sha in manifest["files"].items()
                    ):
                        raise ValueError("交接包讀回驗證失敗")
                os.link(temporary_zip, output)
            finally:
                temporary_zip.unlink(missing_ok=True)
    return {"output": str(output), "sha256": digest(output.read_bytes()), "tables": before}


def prepare_transfer(root):
    root = Path(root).resolve(strict=True)
    marker = inside(root, PREPARED)
    if marker.exists():
        previous = json.loads(marker.read_bytes())
        if previous.get("root") != str(root):
            raise ValueError("已準備的資料夾再次換位置；請從原機重新打包，不要直接搬資料夾")
        return {"status": "already_prepared", "root": str(root), "services_started": False}
    manifest = json.loads(inside(root, MANIFEST).read_bytes())
    if manifest.get("schema_version") != 1 or manifest.get("database") != DATABASE:
        raise ValueError("不支援的交接包版本")
    hashes = manifest["files"]
    if DATABASE not in hashes or "transfer.env" not in hashes:
        raise ValueError("交接包缺少資料庫或接收端設定")
    for name, sha in hashes.items():
        path = inside(root, name)
        if not path.is_file() or (name != DATABASE and digest(path.read_bytes()) != sha):
            raise ValueError(f"交接檔案缺失或已變更：{name}")
    env = inside(root, ".env")
    if env.exists():
        raise ValueError("已有 .env；請在全新的解壓資料夾執行，避免覆寫設定")
    database = inside(root, DATABASE)
    database_hash_matches = digest(database.read_bytes()) == hashes[DATABASE]
    # The ZIP contains no credentials, but the stored source/review data is still private.
    for directory in ("data", "exports"):
        folder = inside(root, directory)
        if folder.exists():
            os.chmod(folder, 0o700)
            for path in folder.rglob("*"):
                checked = inside(root, path.relative_to(root).as_posix())
                os.chmod(checked, 0o700 if checked.is_dir() else 0o600)
    with ExitStack() as locks:
        for purpose in ("admission", "schedule-control", "collection"):
            locks.enter_context(process_lock(lock_database(database), purpose))
        with closing(sqlite3.connect(database)) as conn:
            idle(conn)
            reports = report_paths(conn)
            if not database_hash_matches and not (
                reports
                and all(
                    old_path == str(inside(root, report_name(task_id)))
                    for task_id, old_path, _ in reports
                )
            ):
                raise ValueError("資料庫檔案雜湊不符；請重新解壓交接包")
            # Also allows a safe retry after interruption: only report paths may differ.
            if inspect_database(conn) != manifest["tables"]:
                raise ValueError("資料庫與交接快照不同；停止準備，避免改寫已有資料")
            conn.execute("BEGIN IMMEDIATE")
            for task_id, old_path, sha in reports:
                name = report_name(task_id)
                target = inside(root, name)
                if old_path not in {name, str(target)} or hashes.get(name) != sha:
                    raise ValueError("交接報表路徑或雜湊異常")
                conn.execute(
                    "UPDATE operation_reports SET path=? WHERE task_id=?", (str(target), task_id)
                )
            conn.commit()
            if inspect_database(conn) != manifest["tables"]:
                raise ValueError("準備後的資料庫歷史核對失敗")
        # Nothing in this configuration enables collection. Never copy sender credentials.
        publish_new(env, inside(root, "transfer.env").read_bytes())
        result = {
            "status": "prepared",
            "root": str(root),
            "services_started": False,
            "credentials_required": True,
            "tables": manifest["tables"],
        }
        try:
            publish_new(marker, json.dumps(result, ensure_ascii=False, indent=2).encode())
        except OSError:
            env.unlink()  # Permit retry if marker creation failed; database paths are retry-safe.
            raise
    return result
