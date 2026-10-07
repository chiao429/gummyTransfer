#!/bin/sh
# Local-only category workbook from persisted snapshots; no provider calls.
set -eu
umask 077

if [ "$#" -ne 3 ]; then
  echo "用法：scripts/export_portfolio_local.sh demo|rapidapi THROUGH_RUN_ID OUTPUT.xlsx" >&2
  exit 2
fi

source_name=$1
through_run_id=$2
output_file=$3
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
if [ -e "$output_file" ]; then
  echo "輸出檔案已存在，請改用新檔名：$output_file" >&2
  exit 2
fi
if [ -e "$output_file.verified.json" ]; then
  echo "同名報表已有驗證紀錄，請先檢查：$output_file.verified.json" >&2
  exit 2
fi

artifact_node_dir=${GUMMY_ARTIFACT_NODE_DIR:-"$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/node"}
if [ ! -x "$artifact_node_dir/bin/node" ] || [ ! -d "$artifact_node_dir/node_modules/@oai/artifact-tool" ]; then
  echo "找不到本機 Excel 產生器所需的 Codex 工作環境" >&2
  exit 2
fi

mkdir -p .work
runtime_dir=$(mktemp -d .work/portfolio.XXXXXX)
trap 'rm -rf -- "$runtime_dir"' EXIT HUP INT TERM
cp scripts/build_portfolio.mjs "$runtime_dir/build_portfolio.mjs"
cp scripts/verify_portfolio.mjs "$runtime_dir/verify_portfolio.mjs"
ln -s "$artifact_node_dir/node_modules" "$runtime_dir/node_modules"
.venv/bin/gummy export-portfolio-data --source "$source_name" --through-run-id "$through_run_id" \
  --include-web-reviews --output "$runtime_dir/data.json"
preview_dir=${GUMMY_PORTFOLIO_PREVIEW_DIR:-"$runtime_dir/previews"}
review_file=${GUMMY_REVIEW_WORKBOOK:-}
if [ -n "$review_file" ] && [ ! -f "$review_file" ]; then
  echo "找不到要承接人工核對的活頁簿：$review_file" >&2
  exit 2
fi
"$artifact_node_dir/bin/node" "$runtime_dir/build_portfolio.mjs" \
  "$runtime_dir/data.json" "$output_file" "$preview_dir" "$review_file"
"$artifact_node_dir/bin/node" "$runtime_dir/verify_portfolio.mjs" \
  "$runtime_dir/data.json" "$output_file"
.venv/bin/python - "$output_file" "$source_name" "$through_run_id" "$runtime_dir/data.json" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

output = Path(sys.argv[1])
with output.open("rb") as workbook:
    digest = hashlib.file_digest(workbook, "sha256").hexdigest()
record = {
    "schema_version": 1,
    "source": sys.argv[2],
    "through_run_id": int(sys.argv[3]),
    "verification_status": "PASS",
    "sha256": digest,
    "web_review_snapshot": json.loads(Path(sys.argv[4]).read_text())["web_review_snapshot"],
    "workbook_schema_version": json.loads(Path(sys.argv[4]).read_text())["workbook_schema_version"],
}
with Path(str(output) + ".verified.json").open("x", encoding="utf-8") as manifest:
    json.dump(record, manifest, ensure_ascii=False, sort_keys=True)
    manifest.write("\n")
PY
