#!/bin/sh
# Check an existing workbook against persisted snapshots; never collect data.
set -eu
umask 077

if [ "$#" -ne 3 ]; then
  echo "用法：scripts/verify_portfolio_local.sh demo|rapidapi THROUGH_RUN_ID WORKBOOK.xlsx" >&2
  exit 2
fi

source_name=$1
through_run_id=$2
workbook_file=$3
case "$source_name" in
  demo|rapidapi) ;;
  *) echo "資料來源只能是 demo 或 rapidapi" >&2; exit 2 ;;
esac
case "$through_run_id" in
  ''|*[!0-9]*) echo "THROUGH_RUN_ID 必須是正整數" >&2; exit 2 ;;
esac
if [ "$through_run_id" -eq 0 ]; then
  echo "THROUGH_RUN_ID 必須大於 0" >&2
  exit 2
fi

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"
if [ ! -f "$workbook_file" ]; then
  echo "找不到要驗證的報表：$workbook_file" >&2
  exit 2
fi

artifact_node_dir=${GUMMY_ARTIFACT_NODE_DIR:-}
if [ -z "$artifact_node_dir" ]; then
  if [ -z "${HOME:-}" ]; then
    echo "找不到本機 Excel 驗證器：請設定 GUMMY_ARTIFACT_NODE_DIR" >&2
    exit 2
  fi
  artifact_node_dir=$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/node
fi
if [ ! -x "$artifact_node_dir/bin/node" ] || [ ! -d "$artifact_node_dir/node_modules/@oai/artifact-tool" ]; then
  echo "找不到本機 Excel 驗證器所需的 Codex 工作環境" >&2
  exit 2
fi

mkdir -p .work
runtime_dir=$(mktemp -d .work/portfolio-verify.XXXXXX)
trap 'rm -rf -- "$runtime_dir"' EXIT HUP INT TERM
cp scripts/verify_portfolio.mjs "$runtime_dir/verify_portfolio.mjs"
ln -s "$artifact_node_dir/node_modules" "$runtime_dir/node_modules"
record_fields=$(.venv/bin/python - "$workbook_file" <<'PY'
import json
import sys
from pathlib import Path
record = Path(sys.argv[1] + ".verified.json")
data = json.loads(record.read_text()) if record.is_file() else {}
value = data.get("web_review_snapshot")
version = data.get("workbook_schema_version", 1 if record.is_file() else 3)
if type(version) is not int or version not in (1, 2, 3):
    raise ValueError("Invalid workbook schema version")
if value is not None and (type(value) is not int or not 0 <= value < 2**63):
    raise ValueError("Invalid review snapshot in workbook verification record")
print(f"{value if value is not None else 'none'} {version}")
PY
)
review_snapshot=${record_fields% *}
workbook_version=${record_fields##* }
set --
if [ "$review_snapshot" != "none" ]; then
  set -- --include-web-reviews --review-snapshot "$review_snapshot"
fi
RAPIDAPI_KEY= RAPIDAPI_FREE_PLAN_CONFIRMED=false \
  .venv/bin/gummy export-portfolio-data --source "$source_name" \
  --through-run-id "$through_run_id" --workbook-version "$workbook_version" "$@" --output "$runtime_dir/data.json" >/dev/null
"$artifact_node_dir/bin/node" "$runtime_dir/verify_portfolio.mjs" \
  "$runtime_dir/data.json" "$workbook_file" "$workbook_version"
