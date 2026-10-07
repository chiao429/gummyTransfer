"""Generate an OS process supervisor; daily timing remains inside Python."""

import argparse
import json
import plistlib
import sys
from pathlib import Path

from gummy.python_scheduler import load_scheduler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="python-scheduler.json")
    parser.add_argument("--platform", choices=["macos", "linux"], default="macos")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    path = Path(args.config).resolve()
    load_scheduler(path)
    root = path.parent
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.platform == "macos":
        value = {
            "Label": "com.woobe.gummy.scheduler",
            "ProgramArguments": [sys.executable, "-m", "gummy", "scheduler", "--config", str(path)],
            "WorkingDirectory": str(root),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 30,
            "StandardOutPath": str(root / "data/python-scheduler.stdout.log"),
            "StandardErrorPath": str(root / "data/python-scheduler.stderr.log"),
        }
        with output.open("xb") as handle:
            output.chmod(0o600)
            plistlib.dump(value, handle)
    else:
        if any(
            '"' in str(value) or "\n" in str(value) or "%" in str(value)
            for value in (sys.executable, path, root)
        ):
            raise ValueError("Unsupported character in service path")
        content = f'''[Unit]
Description=gummy independent Python scheduler

[Service]
Type=simple
WorkingDirectory="{root}"
ExecStart="{sys.executable}" -m gummy scheduler --config "{path}"
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
'''
        with output.open("x", encoding="utf-8") as handle:
            output.chmod(0o600)
            handle.write(content)
    print(json.dumps({"output": str(output.resolve()), "status": "GENERATED_NOT_INSTALLED"}))


if __name__ == "__main__":
    main()
