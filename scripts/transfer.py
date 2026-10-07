"""Build an offline handover ZIP, or prepare an extracted copy on a new computer."""

import argparse
import json
import sqlite3
from pathlib import Path

from gummy.config import Settings
from gummy.errors import GummyError
from gummy.transfer import package_transfer, prepare_transfer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="打包資料與程式，不含金鑰或 .venv")
    export.add_argument("--output", type=Path)
    commands.add_parser("prepare", help="接收端驗證資料並修正報表路徑；不啟動採集")
    args = parser.parse_args()
    root = Path.cwd()
    try:
        result = (
            package_transfer(root, Settings.from_env(), args.output)
            if args.command == "export"
            else prepare_transfer(root)
        )
    except (ValueError, OSError, KeyError, sqlite3.Error, GummyError) as error:
        parser.exit(1, f"交接未完成：{error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
