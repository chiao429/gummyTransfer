"""Local credential setup; never expose credential values through the web UI."""

import getpass
import os
import re
import stat
import sys
import tempfile
import warnings
from pathlib import Path

from dotenv import dotenv_values, set_key


def credential_status(settings, path: Path = Path(".env")) -> dict:
    readable = not path.is_symlink()
    values = {}
    if readable:
        try:
            values = dotenv_values(path, interpolate=False) if path.is_file() else {}
        except (OSError, UnicodeError):
            readable = False
    stored = values.get("RAPIDAPI_KEY") or ""
    return {
        "runtime_key_configured": bool(settings.rapidapi_key),
        "local_file_readable": readable,
        "local_key_configured": bool(stored),
        "local_differs_from_runtime": bool(readable and stored != settings.rapidapi_key),
        "free_plan_confirmed": settings.free_plan_confirmed,
        "api_access_protected": bool(settings.api_token),
        "connection_tested": False,
    }


def save_local_rapidapi_key(path: Path, key: str) -> None:
    # The pattern checks local input only; it cannot prove provider authentication.
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,512}", key):
        raise ValueError("金鑰格式不符：請貼入 8–512 字元的英數、底線或連字號")
    if path.is_symlink():
        raise ValueError("拒絕寫入符號連結設定檔")
    original = None
    if path.exists():
        if not path.is_file() or path.stat().st_uid != os.getuid() or path.stat().st_nlink != 1:
            raise ValueError("設定檔必須是目前使用者擁有的獨立一般檔案")
        original = path.read_bytes()
    fd, temporary = tempfile.mkstemp(prefix=".gummy-secret-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(original or b"")
        set_key(temporary, "RAPIDAPI_KEY", key, quote_mode="always")
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        with open(temporary, "rb") as handle:
            os.fsync(handle.fileno())
        if path.is_symlink() or (path.read_bytes() if path.exists() else None) != original:
            raise ValueError("設定檔已變動，請重新執行設定")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def configure_rapidapi() -> dict:
    path = Path.cwd() / ".env"
    if path.is_symlink():
        raise ValueError("拒絕寫入符號連結設定檔")
    file_env = dotenv_values(path, interpolate=False) if path.is_file() else {}
    if os.getenv("APP_ENV", file_env.get("APP_ENV") or "local") != "local":
        raise ValueError("雲端金鑰請透過部署平台的秘密設定管理")
    if os.getenv("RAPIDAPI_KEY"):
        raise ValueError("目前終端已有 RAPIDAPI_KEY 環境設定；請先解除該覆蓋來源再更新本機檔案")
    if not sys.stdin.isatty():
        raise ValueError("請在互動式終端執行；金鑰不可放在命令參數或管線")
    try:
        # Abort before getpass can fall back to visibly echoed input.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = getpass.getpass("RapidAPI 金鑰（輸入不顯示）：").strip()
            confirm = getpass.getpass("再次輸入金鑰：").strip()
    except (getpass.GetPassWarning, EOFError, KeyboardInterrupt):
        raise ValueError("已取消或無法隱藏輸入，未儲存金鑰") from None
    if key != confirm:
        raise ValueError("兩次輸入不一致，未儲存")
    save_local_rapidapi_key(path, key)
    return {
        "configured": True,
        "external_requests": 0,
        "detail": "已儲存；尚未測試供應商連線。既有排程與額度未變更，網站重啟後讀取新設定。",
    }
