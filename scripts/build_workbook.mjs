import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const [inputPath, outputPath, previewDir] = process.argv.slice(2);
if (!inputPath || !outputPath) {
  throw new Error("Usage: node build_workbook.mjs INPUT.json OUTPUT.xlsx [PREVIEW_DIR]");
}
try {
  await fs.access(outputPath);
  throw new Error(`Output already exists: ${outputPath}`);
} catch (error) {
  if (error.code !== "ENOENT") throw error;
}
const payload = JSON.parse(await fs.readFile(inputPath, "utf8"));
if (payload.report.status !== "SUCCEEDED" || !Array.isArray(payload.details)) {
  throw new Error("Only a completed snapshot can be exported");
}

const wb = Workbook.create();
const navy = "#18324D";
const blue = "#2762A3";
const pale = "#EDF3F9";
const text = "#26384A";
const font = "Arial";
const tabs = ["分類覆蓋", "規格組合", "商品明細", "規格細項", "來源對帳"];
const sheets = Object.fromEntries(tabs.map((name) => [name, wb.worksheets.add(name)]));

function safe(value) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value !== "string") return value;
  return /^[\s]*[=+\-@]/u.test(value) ? `'${value}` : value;
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

function put(sheet, cell, matrix) {
  sheet.getRange(cell).writeValues(matrix);
}

function base(sheet, title, lastColumn, headerRow = 3) {
  sheet.showGridLines = false;
  sheet.getRange(`A1:${lastColumn}1`).format.font = {
    name: font,
    size: 14,
    bold: true,
    color: navy,
  };
  sheet.getRange(`A${headerRow}:${lastColumn}${headerRow}`).format = {
    fill: navy,
    font: { name: font, size: 10, bold: true, color: "#FFFFFF" },
    rowHeight: 27,
    verticalAlignment: "center",
  };
  sheet.getRange(`A${headerRow}:${lastColumn}${headerRow}`).format.horizontalAlignment = "center";
  sheet.getRange(`A1:${lastColumn}60`).format.font.name = font;
  sheet.getRange(`A${headerRow + 1}:${lastColumn}60`).format.font.color = text;
  sheet.getRange(`A${headerRow + 1}:${lastColumn}60`).format.font.size = 10;
  sheet.getRange(`A${headerRow + 1}:${lastColumn}60`).format.rowHeight = 22;
  sheet.getRange("A1").values = [[title]];
  if (headerRow === 3) sheet.freezePanes.freezeRows(3);
}

const cover = sheets["分類覆蓋"];
base(cover, "參考表分類覆蓋｜成功快照取樣", "E", 5);
put(cover, "A2", [["資料來源", payload.coverage.source, "已取樣分類", payload.coverage.sampled_count, payload.coverage.reference_total]]);
put(cover, "A3", [["範圍", `截至本報表 run_id ${payload.report.run_id} 的同來源成功批次；商品數不等於市場覆蓋率。`]]);
put(cover, "A5", [["原表 Category", "狀態", "不重複 ASIN", "成功批次", "最新 run_id"]]);
const coverRows = payload.coverage.categories.map((row) => [
  safe(row.category), row.run_count ? (payload.coverage.source === "demo" ? "模擬取樣" : "已取樣") : "未取樣",
  number(row.distinct_product_count), number(row.run_count), number(row.latest_run_id),
]);
put(cover, "A6", coverRows);
const outsideHeaderRow = 7 + coverRows.length;
put(cover, `A${outsideHeaderRow}`, [["參考表以外的分類"]]);
put(cover, `A${outsideHeaderRow + 1}`, [["分類", "狀態", "不重複 ASIN", "成功批次", "最新 run_id"]]);
cover.getRange(`A${outsideHeaderRow + 1}:E${outsideHeaderRow + 1}`).format = {
  fill: blue, font: {name: font, size: 10, bold: true, color: "#FFFFFF"}, rowHeight: 27,
};
const outsideRows = payload.coverage.outside_reference.map((row) => [
  safe(row.category), payload.coverage.source === "demo" ? "模擬取樣" : "已取樣",
  number(row.distinct_product_count), number(row.run_count), number(row.latest_run_id),
]);
if (outsideRows.length) put(cover, `A${outsideHeaderRow + 2}`, outsideRows);
else put(cover, `A${outsideHeaderRow + 2}`, [["無"]]);
for (const [column, width] of Object.entries({A: 55, B: 18, C: 18, D: 16, E: 18})) {
  cover.getRange(`${column}:${column}`).format.columnWidth = width;
}
cover.getRange("B3:E3").merge();
cover.getRange("B3:E3").format.wrapText = true;
cover.getRange("A3:E3").format.rowHeight = 52;
cover.getRange("A3:E3").format.font.color = "#8A5200";

const overview = sheets["規格組合"];
overview.showGridLines = false;
overview.tabColor = navy;
overview.getRange("A2").values = [["Amazon 商品取樣與規格組合"]];
overview.getRange("A2").format.font = { name: font, size: 15, bold: true, color: navy };
put(overview, "A4", [
  ["批次", payload.report.run_id, "資料來源", payload.report.source],
  ["分類", safe(payload.report.parameters.category), "查詢詞", safe(payload.report.parameters.query)],
  ["市場", payload.report.parameters.country, "擷取完成 UTC", utc(payload.report.completed_at)],
  ["不重複商品", payload.report.product_count, "銷量資料", "未提供"],
]);
overview.getRange("A4:H7").format.font = { name: font, size: 10, color: text };
overview.getRange("A4:A7").format.font.bold = true;
overview.getRange("C4:C7").format.font.bold = true;
overview.getRange("B5").format.wrapText = true;
overview.getRange("A5:H5").format.rowHeight = 44;
overview.getRange("D6").setNumberFormat("yyyy-mm-dd hh:mm");
overview.getRange("A9").values = [["分類備註"]];
overview.getRange("B9").values = [["標題候選未經結構化規格證實；同一 ASIN 可屬多組，組別件數不可加總。"]];
overview.getRange("B10").values = [["此表只代表搜尋首頁取樣，沒有月銷量、銷售排名或全市場規模。"]];
overview.getRange("B9:B10").format.columnWidth = 78;
overview.getRange("B9:B10").format.font = { name: font, size: 10, color: "#8A5200" };
const groupHeaders = ["材質", "公制／英制", "商品數", "含價格件數", "均價 USD", "ASIN", "標題候選 ASIN", "分類模式"];
put(overview, "A13", [groupHeaders]);
overview.getRange("A13:H13").format = {
  fill: navy,
  font: { name: font, size: 10, bold: true, color: "#FFFFFF" },
  rowHeight: 27,
};
const groups = payload.groups.map((row) => [
  safe(row.material), safe(row.measurement_system), number(row.group_product_count),
  number(row.priced_product_count), number(row.average_price_usd), safe(row.asins),
  safe(row.title_candidate_asins),
  row.classification_mode === "title_candidates_opted_in" ? "標題候選（未驗證）" : "結構化欄位",
]);
if (groups.length) put(overview, "A14", groups);
overview.getRange(`A14:H${Math.max(14, 13 + groups.length)}`).format.font = {
  name: font, size: 10, color: text,
};
overview.getRange("E14:E100").setNumberFormat("#,##0.00");
for (const [column, width] of Object.entries({A: 27, B: 18, C: 12, D: 16, E: 16, F: 31, G: 32, H: 27})) {
  overview.getRange(`${column}:${column}`).format.columnWidth = width;
}
overview.getRange("D6").format.columnWidth = 25;

const detail = sheets["商品明細"];
base(detail, "商品明細｜每列一個 ASIN", "O");
const detailHeaders = ["ASIN", "商品標題", "品牌", "價格 USD", "幣別", "評分", "評價數", "材質", "結構化制式", "標題候選", "候選依據", "尺寸", "顏色", "擷取時間 UTC", "警示"];
put(detail, "A3", [detailHeaders]);
const detailRows = payload.details.map((row) => [
  safe(row.asin), safe(row.title), safe(row.brand), number(row.price), safe(row.currency),
  number(row.rating), number(row.rating_count), safe(row.material),
  safe(row.measurement_system), safe(row.title_measurement_candidates),
  safe(row.title_measurement_evidence), safe(row.size), safe(row.color),
  utc(row.observed_at), safe(row.warnings),
]);
if (detailRows.length) put(detail, "A4", detailRows);
detail.getRange("B:B").format.columnWidth = 72;
detail.getRange("A:A").format.columnWidth = 17;
detail.getRange("C:O").format.columnWidth = 19;
detail.getRange("N:N").format.columnWidth = 22;
detail.getRange(`B4:B${Math.max(4, 3 + detailRows.length)}`).format.wrapText = true;
detail.getRange(`A4:O${Math.max(4, 3 + detailRows.length)}`).format.rowHeight = 62;
detail.getRange("D4:D100").setNumberFormat("#,##0.00");
detail.getRange("F4:F100").setNumberFormat("0.0");
detail.getRange("G4:G100").setNumberFormat("#,##0");
detail.getRange("N4:N100").setNumberFormat("yyyy-mm-dd hh:mm");

const specs = sheets["規格細項"];
base(specs, "規格細項｜每列一個商品規格值", "E");
put(specs, "A3", [["分類", "ASIN", "規格欄位", "規格值", "資料依據"]]);
const specRows = payload.specifications.map((row) => [
  safe(row.category), safe(row.asin),
  ({material: "材質", measurement_system: "公制／英制", size: "尺寸", color: "顏色"})[row.dimension],
  safe(row.value),
  row.evidence === "unverified_title_keyword" ? "標題關鍵字（未驗證）" : "商品明細結構化欄位",
]);
if (specRows.length) put(specs, "A4", specRows);
for (const [column, width] of Object.entries({A: 29, B: 17, C: 25, D: 36, E: 31})) {
  specs.getRange(`${column}:${column}`).format.columnWidth = width;
}

const audit = sheets["來源對帳"];
base(audit, "來源對帳｜原始選定欄位與入庫值", "Y");
const auditKeys = [
  "asin", "evidence_status", "source_title", "stored_title", "source_price",
  "stored_price_raw", "stored_price_usd", "source_brand", "stored_brand",
  "source_material", "stored_material", "source_measurement_system",
  "stored_measurement_system", "title_measurement_candidates_unverified",
  "source_size", "stored_size", "source_color", "stored_color", "source_rating",
  "stored_rating", "source_rating_count", "stored_rating_count", "observed_at", "source", "run_id",
];
const auditHeaders = [
  "ASIN", "來源證據狀態", "來源標題", "入庫標題", "來源價格", "入庫原價格",
  "入庫價格 USD", "來源品牌", "入庫品牌", "來源材質", "入庫材質",
  "來源制式", "入庫制式", "標題候選（未驗證）", "來源尺寸", "入庫尺寸",
  "來源顏色", "入庫顏色", "來源評分", "入庫評分", "來源評價數",
  "入庫評價數", "擷取時間 UTC", "資料來源", "批次",
];
put(audit, "A3", [auditHeaders]);
const auditRows = payload.audit.map((row) => auditKeys.map((key) => {
  if (["stored_price_usd", "stored_rating", "stored_rating_count"].includes(key)) return number(row[key]);
  if (key === "observed_at") return utc(row[key]);
  if (key === "run_id") return number(row[key]);
  if (key === "evidence_status") {
    return row[key] === "captured_selected_fields" ? "已留存選定欄位" : "本批未留存來源欄位";
  }
  return safe(row[key]);
}));
if (auditRows.length) put(audit, "A4", auditRows);
audit.getRange("A:A").format.columnWidth = 17;
audit.getRange("B:B").format.columnWidth = 29;
audit.getRange("C:D").format.columnWidth = 72;
audit.getRange("E:Y").format.columnWidth = 23;
audit.getRange("W:W").format.columnWidth = 22;
audit.getRange(`C4:D${Math.max(4, 3 + auditRows.length)}`).format.wrapText = true;
audit.getRange(`A4:Y${Math.max(4, 3 + auditRows.length)}`).format.rowHeight = 62;
audit.getRange("G4:G100").setNumberFormat("#,##0.00");
audit.getRange("W4:W100").setNumberFormat("yyyy-mm-dd hh:mm");

for (const [sheet, header, end, last] of [
  [cover, 5, 5 + coverRows.length, "E"],
  [cover, outsideHeaderRow + 1, outsideHeaderRow + 1 + Math.max(1, outsideRows.length), "E"],
  [overview, 13, 13 + groups.length, "H"],
  [detail, 3, 3 + detailRows.length, "O"],
  [specs, 3, 3 + specRows.length, "E"],
  [audit, 3, 3 + auditRows.length, "Y"],
]) {
  sheet.getRange(`A${header}:${last}${end}`).format.borders = {preset:"all", style:"thin", color:"#AAB8C6"};
  sheet.getRange(`A${header}:${last}${end}`).format.verticalAlignment = "center";
  const heading = sheet.getRange(`A${header}:${last}${header}`);
  heading.format.borders = {preset:"all", style:"thin", color:"#FFFFFF"};
  heading.format.wrapText = true;
  heading.format.horizontalAlignment = "center";
  heading.format.rowHeight = 42;
}

wb.recalculate();
for (const [name, range] of [
  ["分類覆蓋", "A1:E12"],
  ["規格組合", "A2:H17"], ["商品明細", "A1:H6"],
  ["規格細項", "A1:E12"], ["來源對帳", "A1:G6"],
]) {
  const result = await wb.inspect({kind: "table", range: `${name}!${range}`, include: "values,formulas", tableMaxRows: 18, tableMaxCols: 8, maxChars: 4000});
  if (!result.ndjson.includes('"kind":"table"')) throw new Error(`Unable to inspect ${name}`);
  if (previewDir) {
    await fs.mkdir(previewDir, {recursive: true});
    const preview = await wb.render({sheetName: name, range, scale: 1.5, format: "png"});
    await fs.writeFile(`${previewDir}/${name}.png`, new Uint8Array(await preview.arrayBuffer()));
  }
}
const errors = await wb.inspect({kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!", options: {useRegex: true, maxResults: 100}, summary: "final formula error scan"});
if (!errors.ndjson.includes("Cell search matched 0 entries.")) {
  throw new Error("Workbook contains a formula-style error marker");
}
await fs.mkdir(path.dirname(outputPath), {recursive: true});
const output = await SpreadsheetFile.exportXlsx(wb);
await output.save(outputPath);
console.log(JSON.stringify({output: outputPath, sheets: tabs, product_count: payload.report.product_count}));
