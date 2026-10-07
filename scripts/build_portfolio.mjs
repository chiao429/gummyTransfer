import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const [inputPath, outputPath, previewDir, reviewPath] = process.argv.slice(2);
if (!inputPath || !outputPath) {
  throw new Error("Usage: node build_portfolio.mjs INPUT.json OUTPUT.xlsx [PREVIEW_DIR] [REVIEW.xlsx]");
}
try {
  await fs.access(outputPath);
  throw new Error("Output already exists: " + outputPath);
} catch (error) {
  if (error.code !== "ENOENT") throw error;
}
const payload = JSON.parse(await fs.readFile(inputPath, "utf8"));
if (!Array.isArray(payload.observations) || !Array.isArray(payload.combinations) || !Array.isArray(payload.coverage?.categories)) {
  throw new Error("Invalid portfolio data");
}

const previousReviews = new Map();
let priorHasDetail = false;
if (reviewPath) {
  const prior = await SpreadsheetFile.importXlsx(await FileBlob.load(reviewPath));
  for (const sheet of prior.worksheets.items) {
    const formulas = sheet.getUsedRange().formulas;
    if (formulas.some((row) => row.some((formula) => formula))) {
      throw new Error("Review workbook contains formulas; enter manual review as plain text");
    }
  }
  const priorSource = prior.worksheets.getItem("分類總覽").getRange("B2").values[0][0];
  if (priorSource !== payload.source) throw new Error("Review workbook source differs from the export");
  const priorDetail = prior.worksheets.items.find((sheet) => sheet.name === "來源欄位對照");
  const previousDetails = new Map();
  priorHasDetail = Boolean(priorDetail);
  if (priorDetail) {
    for (const row of priorDetail.getUsedRange().values.slice(3)) {
      if (row[0] == null && row[2] == null) continue;
      const runId = Number(row[0]);
      const asin = String(row[2] ?? "").trim();
      if (!Number.isInteger(runId) || runId < 1 || !/^[A-Z0-9]{10}$/u.test(asin)) {
        throw new Error("A source detail row has no valid run ID and ASIN");
      }
      const key = runId + "\u0000" + asin;
      if (previousDetails.has(key)) throw new Error("Duplicate source detail run ID and ASIN");
      previousDetails.set(key, row);
    }
  }
  const reviewRows = prior.worksheets.getItem("對帳索引").getUsedRange().values;
  for (const row of reviewRows.slice(3)) {
    const manual = row.slice(10, 13).map((value) => value == null ? "" : String(value).trim());
    if (!manual.some((value) => value && value !== "待確認")) continue;
    const runId = Number(row[0]);
    const asin = String(row[2] ?? "").trim();
    if (!Number.isInteger(runId) || runId < 1 || !/^[A-Z0-9]{10}$/u.test(asin)) {
      throw new Error("A completed review row has no valid run ID and ASIN");
    }
    const key = runId + "\u0000" + asin;
    if (previousReviews.has(key)) throw new Error("Duplicate reviewed run ID and ASIN");
    previousReviews.set(key, {manual, compared: row.slice(6, 10), detail: previousDetails.get(key)});
  }
}

const wb = Workbook.create();
const navy = "#18324D";
const blue = "#2762A3";
const font = "Arial";
const sheets = [];
const tableRanges = new Map();

function safe(value) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value !== "string") return value;
  return /^[\s]*[=+\-@]/u.test(value) ? "'" + value : value;
}

function amazonUrl(asin) {
  if (payload.source !== "rapidapi" || !/^[A-Z0-9]{10}$/u.test(asin)) return null;
  return "https://www.amazon.com/dp/" + asin;
}

function number(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function utc(value) {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? safe(value) : parsed;
}

function timestampMs(value) {
  // Calc may return a 1900-system Excel serial after saving an XLSX workbook.
  // Normalize floating point day fractions to Date's integer-millisecond precision.
  // Keep the existing one-second editor tolerance; do not widen it for real changes.
  return typeof value === "number" ? Math.round((value - 25569) * 86400000) : new Date(value).getTime();
}

function displayed(value) {
  if (typeof value === "string" && value.startsWith("'") && /^[\s]*[=+\-@]/u.test(value.slice(1))) {
    return value.slice(1);
  }
  return value;
}

function sameDetail(previous, current) {
  if (!previous || previous.length < 25) return false;
  for (let index = 0; index < 25; index += 1) {
    if (index === 24) {
      const oldTime = timestampMs(previous[index]);
      const newTime = timestampMs(current[index]);
      if (!Number.isFinite(oldTime) || !Number.isFinite(newTime) || Math.abs(oldTime - newTime) > 1000) {
        return false;
      }
    } else if (String(previous[index] ?? "") !== String(displayed(current[index]) ?? "")) {
      return false;
    }
  }
  return true;
}

function put(sheet, cell, rows) {
  if (rows.length) {
    sheet.getRange(cell).writeValues(rows);
    const table = tableRanges.get(sheet.name);
    if (table) table.end = Math.max(table.end, Number(cell.match(/\d+/u)[0]) + rows.length - 1);
  }
}

function addSheet(name, title, lastColumn, headerRow = 3, frozenColumns = 0) {
  const sheet = wb.worksheets.add(name);
  sheets.push(name);
  tableRanges.set(name, {header: headerRow, lastColumn, end: headerRow});
  sheet.showGridLines = false;
  sheet.freezePanes.freezeRows(headerRow);
  if (frozenColumns) sheet.freezePanes.freezeColumns(frozenColumns);
  sheet.getRange("A1").values = [[title]];
  sheet.getRange("A1:" + lastColumn + "1").format.font = {
    name: font, size: 14, bold: true, color: navy,
  };
  sheet.getRange("A" + headerRow + ":" + lastColumn + headerRow).format = {
    fill: navy,
    font: {name: font, size: 10, bold: true, color: "#FFFFFF"},
    rowHeight: 28,
  };
  sheet.getRange("A" + (headerRow + 1) + ":" + lastColumn + "80").format.font = {
    name: font, size: 10, color: "#26384A",
  };
  sheet.getRange("A" + (headerRow + 1) + ":" + lastColumn + "80").format.rowHeight = 48;
  return sheet;
}

function tabName(category, index, outside) {
  const prefix = (outside ? "其他" : "分類") + String(index + 1).padStart(2, "0") + "-";
  const short = category.split("(")[0].replace(/[\\/:*?\[\]]/gu, "-").trim();
  return (prefix + short).slice(0, 31);
}

const categories = [
  ...payload.coverage.categories.map((row, index) => ({
    ...row, type: "原表分類", tab: tabName(row.category, index, false),
  })),
  ...payload.coverage.outside_reference.map((row, index) => ({
    ...row, type: "參考表以外", tab: tabName(row.category, index, true),
  })),
];
const sampled = categories.filter((row) => row.run_count > 0);
if (new Set(sampled.map((row) => row.tab)).size !== sampled.length) {
  throw new Error("Category sheet names collide");
}

const cover = addSheet("分類總覽", "跨批次分類總覽｜成功快照取樣", "G", 4);
put(cover, "A2", [[
  "來源", payload.source, "截至批次", number(payload.through_run_id),
  "成功批次", number(payload.run_count), "分類取樣 " + payload.coverage.sampled_count + "/" + payload.coverage.reference_total,
]]);
put(cover, "A3", [[
  "範圍", "搜尋分類是任務標籤，逐件歸類待人工確認；取樣件數不是銷量。",
]]);
put(cover, "A4", [[
  "類型", "原表 Category", "狀態", "不重複 ASIN", "成功批次", "最新 run_id", "分類分頁",
]]);
put(cover, "A5", categories.map((row) => [
  row.type, safe(row.category), row.run_count ? (payload.source === "demo" ? "模擬取樣" : "已取樣") : "未取樣",
  number(row.distinct_product_count), number(row.run_count), number(row.latest_run_id),
  row.run_count ? row.tab : null,
]));
for (const [column, width] of Object.entries({A: 18, B: 55, C: 16, D: 19, E: 16, F: 16, G: 29})) {
  cover.getRange(column + ":" + column).format.columnWidth = width;
}
cover.getRange("B3").format.wrapText = true;
cover.getRange("A3:G3").format.rowHeight = 45;

const combinations = addSheet("規格組合", "跨分類規格組合｜成功快照取樣", "N", 3, 1);
put(combinations, "A2", [["同一 ASIN 可出現在多組；標題候選未驗證；來源對帳 PASS 不是人工確認。"]]);
put(combinations, "A3", [[
  "搜尋分類", "材質", "公制／英制", "尺寸", "顏色", "觀測列數",
  "不重複 ASIN", "有價格列數", "平均價 USD", "標題候選列數",
  "來源留存列數", "來源對帳 PASS 列數", "ASIN", "批次:ASIN",
]]);
put(combinations, "A4", payload.combinations.map((row) => [
  safe(row.category), safe(row.material), safe(row.measurement_system), safe(row.size),
  safe(row.color), number(row.observation_count), number(row.distinct_asin_count),
  number(row.priced_observation_count), number(row.average_price_usd),
  number(row.title_candidate_observation_count),
  number(row.source_evidence_observation_count), number(row.source_pass_observation_count),
  safe(row.asins.join("; ")), safe(row.run_asins.join("; ")),
]));
for (const [column, width] of Object.entries({A: 47, B: 25, C: 19, D: 22, E: 20, F: 17, G: 20, H: 19, I: 19, J: 21, K: 21, L: 26, M: 50, N: 65})) {
  combinations.getRange(column + ":" + column).format.columnWidth = width;
}
combinations.getRange("I4:I80").setNumberFormat("#,##0.00");
combinations.getRange("M4:N80").format.wrapText = true;

const productHeaders = [
  "批次", "搜尋分類（待人工確認）", "ASIN", "商品標題", "品牌", "價格 USD", "幣別",
  "評分", "評價數", "材質", "結構化制式", "標題候選（未驗證）",
  "尺寸", "顏色", "來源證據", "批次對帳", "商品差異數", "擷取時間 UTC",
];
function productValues(row) {
  return [
    number(row.run_id), safe(row.category), safe(row.asin), safe(row.title), safe(row.brand),
    number(row.price), safe(row.currency), number(row.rating), number(row.rating_count),
    safe(row.material), safe(row.measurement_system), safe(row.title_measurement_candidates),
    safe(row.size), safe(row.color),
    row.evidence_status === "captured_selected_fields" ? "已留存選定欄位" : "本批未留存來源欄位",
    safe(row.run_verification), number(row.product_issue_count), utc(row.observed_at),
  ];
}
function sourceDetailValues(row) {
  const fields = row.source_audit;
  if (!fields) throw new Error("Portfolio observation is missing selected source fields");
  return [
    number(row.run_id), safe(row.category), safe(row.asin),
    row.evidence_status === "captured_selected_fields" ? "已留存選定欄位" : "本批未留存來源欄位",
    safe(fields.source_title), safe(fields.stored_title), safe(fields.source_price),
    safe(fields.stored_price_raw), number(fields.stored_price_usd),
    safe(fields.source_brand), safe(fields.stored_brand),
    safe(fields.source_material), safe(fields.stored_material),
    safe(fields.source_measurement_system), safe(fields.stored_measurement_system),
    safe(fields.title_measurement_candidates_unverified),
    safe(fields.source_size), safe(fields.stored_size),
    safe(fields.source_color), safe(fields.stored_color),
    safe(fields.source_rating), number(fields.stored_rating),
    safe(fields.source_rating_count), number(fields.stored_rating_count),
    utc(fields.observed_at),
  ];
}
function fillProducts(sheet, rows) {
  put(sheet, "A3", [productHeaders]);
  put(sheet, "A4", rows.map(productValues));
  sheet.getRange("A:A").format.columnWidth = 12;
  sheet.getRange("B:B").format.columnWidth = 47;
  sheet.getRange("C:C").format.columnWidth = 17;
  sheet.getRange("D:D").format.columnWidth = 70;
  sheet.getRange("E:R").format.columnWidth = 19;
  sheet.getRange("O:P").format.columnWidth = 25;
  sheet.getRange("R:R").format.columnWidth = 23;
  sheet.getRange("D4:D80").format.wrapText = true;
  sheet.getRange("F4:F80").setNumberFormat("#,##0.00");
  sheet.getRange("H4:H80").setNumberFormat("0.0");
  sheet.getRange("I4:I80").setNumberFormat("#,##0");
  sheet.getRange("R4:R80").setNumberFormat("yyyy-mm-dd hh:mm");
}

const allProducts = addSheet("跨分類商品", "跨分類商品｜每列是一批次中的一件商品", "R", 3, 3);
put(allProducts, "A2", [[
  "觀測列", number(payload.observation_count), "不重複 ASIN", number(payload.distinct_asin_count),
  "同一 ASIN 在不同批次保留各自的價格與來源證據。",
]]);
fillProducts(allProducts, payload.observations);

const audit = addSheet("對帳索引", "跨批次來源對帳索引｜需人工核對來源內容", "O", 3, 3);
put(audit, "A2", [[
  "PASS 僅核對留存欄位；INCOMPLETE 表示缺來源證據。", null,
  "標題候選未驗證；K–M 欄供人工填寫。",
  ...Array(11).fill(null), "O 欄為目前商品頁，與擷取時內容可能不同。",
]]);
put(audit, "A3", [[
  "批次", "搜尋分類", "ASIN", "證據狀態", "批次對帳", "商品差異數",
  "來源標題", "入庫標題", "來源價格", "入庫價格 USD",
  "人工分類確認", "人工來源核對", "人工備註", "擷取時間 UTC", "Amazon 網址（複製開啟）",
]]);
let carriedReviews = 0;
let resetReviews = 0;
put(audit, "A4", payload.observations.map((row) => {
  const previous = previousReviews.get(number(row.run_id) + "\u0000" + row.asin);
  const compared = [safe(row.source_title), safe(row.title), safe(row.source_price), number(row.price)];
  const sameIndex = previous && previous.compared.every((value, index) => (
    String(value ?? "") === String(displayed(compared[index]) ?? "")
  ));
  const sameSource = sameIndex && (!priorHasDetail || sameDetail(previous.detail, sourceDetailValues(row)));
  if (sameSource) carriedReviews += 1;
  else if (previous) resetReviews += 1;
  const manual = sameSource ? previous.manual : null;
  return [
    number(row.run_id), safe(row.category), safe(row.asin),
    row.evidence_status === "captured_selected_fields" ? "已留存選定欄位" : "本批未留存來源欄位",
    safe(row.run_verification), number(row.product_issue_count),
    safe(row.source_title), safe(row.title), safe(row.source_price), number(row.price),
    safe(manual?.[0] || "待確認"), safe(manual?.[1] || "待確認"), safe(manual?.[2]), utc(row.observed_at),
    amazonUrl(row.asin),
  ];
}));
for (const [column, width] of Object.entries({A: 12, B: 47, C: 17, D: 25, E: 16, F: 16, G: 70, H: 70, I: 18, J: 19, K: 18, L: 18, M: 35, N: 23, O: 50})) {
  audit.getRange(column + ":" + column).format.columnWidth = width;
}
audit.getRange("G4:H80").format.wrapText = true;
audit.getRange("J4:J80").setNumberFormat("#,##0.00");
audit.getRange("N4:N80").setNumberFormat("yyyy-mm-dd hh:mm");
audit.getRange("K4:M" + (3 + payload.observations.length)).format.fill = "#FFF6DF";

const sourceDetail = addSheet("來源欄位對照", "跨批次來源欄位對照｜僅留存的選定欄位", "Y", 3, 3);
put(sourceDetail, "A2", [[
  "來源欄空白可能是供應商未提供或舊批次未留存；請先看證據狀態，不能把空白當成一致。",
]]);
put(sourceDetail, "A3", [[
  "批次", "搜尋分類", "ASIN", "來源證據狀態", "來源標題", "入庫標題",
  "來源價格", "入庫原價格", "入庫價格 USD", "來源品牌", "入庫品牌",
  "來源材質", "入庫材質", "來源制式", "入庫制式", "標題候選（未驗證）",
  "來源尺寸", "入庫尺寸", "來源顏色", "入庫顏色", "來源評分", "入庫評分",
  "來源評價數", "入庫評價數", "擷取時間 UTC",
]]);
put(sourceDetail, "A4", payload.observations.map(sourceDetailValues));
sourceDetail.getRange("A:A").format.columnWidth = 12;
sourceDetail.getRange("B:B").format.columnWidth = 47;
sourceDetail.getRange("C:C").format.columnWidth = 17;
sourceDetail.getRange("D:D").format.columnWidth = 25;
sourceDetail.getRange("E:F").format.columnWidth = 70;
sourceDetail.getRange("G:X").format.columnWidth = 20;
sourceDetail.getRange("P:P").format.columnWidth = 27;
sourceDetail.getRange("U:X").format.columnWidth = 23;
sourceDetail.getRange("Y:Y").format.columnWidth = 23;
sourceDetail.getRange("E4:F80").format.wrapText = true;
for (const column of ["E", "G", "J", "L", "N", "Q", "S", "U", "W"]) {
  sourceDetail.getRange(column + "4:" + column + "80").format.fill = "#EDF3F9";
}
sourceDetail.getRange("U4:X80").format.horizontalAlignment = "right";
sourceDetail.getRange("I4:I80").setNumberFormat("#,##0.00");
sourceDetail.getRange("V4:V80").setNumberFormat("0.0");
sourceDetail.getRange("X4:X80").setNumberFormat("#,##0");
sourceDetail.getRange("Y4:Y80").setNumberFormat("yyyy-mm-dd hh:mm");

if (payload.additional_attributes?.length) {
  const hasReviews = payload.additional_attributes.some((item) => item.source_section === "top_review_sample");
  const extra = addSheet("來源附加欄位", hasReviews ? "供應商附加欄位與精選評論樣本" : "供應商其他商品屬性｜未分類、未納入規格組合", "H", 3, 3);
  put(extra, "A2", [[hasReviews ? "保留供應商原文。評論僅為精選樣本；sales_volume 是非精確購買提示，不是實際銷量。" : "僅供評估後續映射；sales_volume 是非精確購買提示，不是實際銷量。"]]);
  put(extra, "A3", [[
    "批次", "搜尋分類", "ASIN", "擷取時間 UTC", "來源區塊", "來源欄名", "來源原值", "分類狀態",
  ]]);
  put(extra, "A4", payload.additional_attributes.map((item) => [
    number(item.run_id), safe(item.category), safe(item.asin), utc(item.observed_at),
    ({search_listing: "搜尋結果", product_summary: "商品明細摘要", product_information: "商品資訊", product_details: "商品詳情", top_review_sample: "精選評論樣本"})[item.source_section],
    safe(item.field_name), safe(item.source_value), "未分類；待人工確認",
  ]));
  for (const [column, width] of Object.entries({A: 12, B: 47, C: 17, D: 23, E: 18, F: 35, G: 70, H: 23})) {
    extra.getRange(column + ":" + column).format.columnWidth = width;
  }
  const lastExtraRow = 3 + payload.additional_attributes.length;
  extra.getRange("A4:H" + lastExtraRow).format.font = {name: font, size: 10, color: "#26384A"};
  extra.getRange("A4:H" + lastExtraRow).format.rowHeight = 48;
  extra.getRange("D4:D" + lastExtraRow).setNumberFormat("yyyy-mm-dd hh:mm");
  extra.getRange("F4:G" + lastExtraRow).format.wrapText = true;
  payload.additional_attributes.forEach((item, index) => {
    if (item.source_section === "top_review_sample" && item.field_name.endsWith("_review_comment")) {
      const row = index + 4;
      const lines = Math.ceil(String(item.source_value).length / 60);
      extra.getRange(`A${row}:H${row}`).format.rowHeight = Math.min(409, Math.max(48, lines * 15 + 12));
    }
  });
}

if (payload.specification_mappings?.length) {
  const sheet = addSheet("規格映射", "來源規格與標準欄位對照", "K", 3, 3);
  put(sheet, "A2", [["欄名映射版本", payload.specification_mapping_version, "候選尚未套用到商品或規格組合；衝突須人工核對。"]]);
  put(sheet, "A3", [["批次", "搜尋分類", "ASIN", "來源區塊", "來源欄名", "來源原值", "標準欄位", "候選值", "既有入庫值", "狀態", "映射版本"]]);
  put(sheet, "A4", payload.specification_mappings.map((row) => [
    number(row.run_id), safe(row.category), safe(row.asin), safe(row.source_section),
    safe(row.field_name), safe(row.source_value), safe(row.dimension), safe(row.candidate_value),
    safe(row.stored_value), safe(row.status), safe(row.mapping_version),
  ]));
  for (const [column, width] of Object.entries({A:12, B:34, C:17, D:25, E:28, F:36, G:24, H:36, I:36, J:28, K:24})) sheet.getRange(column + ":" + column).format.columnWidth = width;
  const end = payload.specification_mappings.length + 3;
  sheet.getRange("A4:K" + end).format.wrapText = true;
  sheet.getRange("F4:F" + end).format.fill = "#EDF3F9";
  payload.specification_mappings.forEach((row, index) => {
    const units = Array.from(row.source_value).reduce((sum, char) => sum + (char.codePointAt(0) > 255 ? 2 : 1), 0);
    sheet.getRange(`A${index + 4}:K${index + 4}`).format.rowHeight = Math.max(54, Math.ceil(units / 32) * 15 + 12);
  });
}

if (payload.review_samples?.length) {
  const sheet = addSheet("評論來源", "採集到的評論來源資料", "J", 3, 3);
  put(sheet, "A2", [["來源", payload.source, "每列為一個批次留存的精選評論樣本，不代表全部評論。"]]);
  put(sheet, "A3", [["批次", "搜尋分類", "ASIN", "評論 ID", "星等", "評論日期原文", "評論標題原文", "評論全文原文", "擷取時間 UTC", "資料來源"]]);
  put(sheet, "A4", payload.review_samples.map((row) => [
    number(row.run_id), safe(row.category), safe(row.asin), safe(row.review_id), number(row.review_star_rating),
    safe(row.review_date), safe(row.review_title), safe(row.review_comment), utc(row.observed_at), safe(payload.source),
  ]));
  for (const [column, width] of Object.entries({A:12, B:34, C:17, D:23, E:12, F:38, G:45, H:72, I:26, J:18})) sheet.getRange(column + ":" + column).format.columnWidth = width;
  const end = payload.review_samples.length + 3;
  sheet.getRange("A4:J" + end).format.wrapText = true;
  sheet.getRange("D4:H" + end).format.fill = "#EDF3F9";
  sheet.getRange("I4:I" + end).setNumberFormat("yyyy-mm-dd hh:mm:ss");
  payload.review_samples.forEach((row, index) => {
    const textLines = String(row.review_comment).split("\n").reduce((sum, line) => sum + Math.max(1, Math.ceil(Array.from(line).reduce((n, ch) => n + (ch.codePointAt(0) > 255 ? 2 : 1), 0) / 67)), 0);
    sheet.getRange(`A${index + 4}:J${index + 4}`).format.rowHeight = Math.min(409, Math.max(60, textLines * 15 + 12));
  });
}

for (const category of sampled) {
  const sheet = addSheet(category.tab, category.category + "｜搜尋分類待人工確認", "R", 3, 3);
  const rows = payload.observations.filter((row) => row.category === category.category);
  put(sheet, "A2", [[
    "分類觀測列", rows.length, "不重複 ASIN", number(category.distinct_product_count),
    "成功批次", number(category.run_count), "取樣資料，不代表市場總量。",
  ]]);
  fillProducts(sheet, rows);
}

if (payload.web_reviews) {
  const web = addSheet("網站核對", "網站核對結果", "J", 3, 3);
  put(web, "A2", [["網站儲存快照", payload.web_review_snapshot, "此分頁為匯出快照；請回網站修改。對帳索引 K–M 為另存的 Excel 手填結果，兩者未互相同步。"]]);
  put(web, "A3", [["批次", "搜尋分類", "ASIN", "核對狀態", "分類判斷", "來源核對", "填寫人（自填）", "版本", "核對時間 UTC", "備註"]]);
  put(web, "A4", payload.web_reviews.map((item) => [
    number(item.run_id), safe(item.category), safe(item.asin), safe(item.review_state),
    safe(item.review_classification), safe(item.review_source), safe(item.reviewer),
    number(item.review_revision), utc(item.review_updated_at), safe(item.review_note),
  ]));
  for (const [column, width] of Object.entries({A: 12, B: 28, C: 17, D: 27, E: 15, F: 15, G: 24, H: 10, I: 26, J: 70})) {
    web.getRange(column + ":" + column).format.columnWidth = width;
  }
  const end = payload.web_reviews.length + 3;
  web.getRange("A4:J" + end).format.rowHeight = 54;
  web.getRange("B4:J" + end).format.wrapText = true;
  web.getRange("I4:I" + end).setNumberFormat("yyyy-mm-dd hh:mm:ss");
  payload.web_reviews.forEach((item, index) => {
    if (item.review_note) {
      const units = Array.from(item.review_note).reduce((sum, char) => sum + (char.codePointAt(0) > 255 ? 2 : 1), 0);
      const row = index + 4;
      web.getRange(`A${row}:J${row}`).format.rowHeight = Math.min(409, Math.max(54, Math.ceil(units / 60) * 15 + 12));
    }
  });
}

// Actual cell borders survive Excel/Numbers and printing even with gridlines hidden.
for (const name of sheets) {
  const sheet = wb.worksheets.getItem(name);
  const {header, lastColumn, end} = tableRanges.get(name);
  sheet.getRange(`A${header}:${lastColumn}${end}`).format.borders = {preset:"all", style:"thin", color:"#AAB8C6"};
  sheet.getRange(`A${header}:${lastColumn}${end}`).format.verticalAlignment = "center";
  const heading = sheet.getRange(`A${header}:${lastColumn}${header}`);
  heading.format.borders = {preset:"all", style:"thin", color:"#FFFFFF"};
  heading.format.wrapText = true;
  heading.format.horizontalAlignment = "center";
  heading.format.rowHeight = 42;
}

wb.recalculate();
const previewNames = sheets;
for (const name of previewNames) {
  const range = name === "網站核對" ? "A1:J6" : name === "分類總覽" ? "A1:G12" : name === "對帳索引" ? "I2:O6" : name === "規格映射" ? "D3:K7" : name === "評論來源" ? "D3:J5" : "A1:H6";
  const result = await wb.inspect({
    kind: "table", range: name + "!" + range, include: "values,formulas",
    tableMaxRows: 18, tableMaxCols: 10, maxChars: 4000,
  });
  if (!result.ndjson.includes('"kind":"table"')) throw new Error("Unable to inspect " + name);
  if (previewDir) {
    await fs.mkdir(previewDir, {recursive: true});
    const preview = await wb.render({sheetName: name, range, scale: 1.5, format: "png"});
    await fs.writeFile(path.join(previewDir, name + ".png"), new Uint8Array(await preview.arrayBuffer()));
  }
}
const errors = await wb.inspect({
  kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!",
  options: {useRegex: true, maxResults: 100}, summary: "final formula error scan",
});
if (!errors.ndjson.includes("Cell search matched 0 entries.")) {
  throw new Error("Workbook contains a formula-style error marker");
}
await fs.mkdir(path.dirname(outputPath), {recursive: true});
const output = await SpreadsheetFile.exportXlsx(wb);
await output.save(outputPath);
console.log(JSON.stringify({
  output: outputPath, sheets, observations: payload.observation_count,
  distinct_asins: payload.distinct_asin_count, carried_reviews: carriedReviews, reset_reviews: resetReviews,
}));
