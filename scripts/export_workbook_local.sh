#!/bin/sh
# Local-only workbook export using the Codex-bundled artifact tool runtime.
set -eu

if [ "$#" -ne 3 ]; then
  echo "用法：scripts/export_workbook_local.sh demo|rapidapi RUN_ID OUTPUT.xlsx" >&2
  exit 2
fi

source_name=$1
run_id=$2
output_file=$3
case "$source_name" in
  demo|rapidapi) ;;
  *) echo "資料來源只能是 demo 或 rapidapi" >&2; exit 2 ;;
esac
case "$run_id" in
  ''|*[!0-9]*) echo "RUN_ID 必須是正整數" >&2; exit 2 ;;
esac
if [ "$run_id" -eq 0 ]; then
  echo "RUN_ID 必須大於 0" >&2
  exit 2
fi

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"
if [ -e "$output_file" ]; then
  echo "輸出檔案已存在，請改用新檔名：$output_file" >&2
  exit 2
fi

artifact_node_dir=${GUMMY_ARTIFACT_NODE_DIR:-"$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/node"}
if [ ! -x "$artifact_node_dir/bin/node" ] || [ ! -d "$artifact_node_dir/node_modules/@oai/artifact-tool" ]; then
  echo "找不到本機 Excel 產生器所需的 Codex 工作環境" >&2
  exit 2
fi

mkdir -p .work
runtime_dir=$(mktemp -d .work/workbook.XXXXXX)
trap 'rm -rf -- "$runtime_dir"' EXIT HUP INT TERM
cp scripts/build_workbook.mjs "$runtime_dir/build_workbook.mjs"
ln -s "$artifact_node_dir/node_modules" "$runtime_dir/node_modules"
.venv/bin/gummy export-workbook-data --source "$source_name" --run-id "$run_id" \
  --output "$runtime_dir/data.json"
"$artifact_node_dir/bin/node" "$runtime_dir/build_workbook.mjs" \
  "$runtime_dir/data.json" "$output_file" "$runtime_dir/previews"
