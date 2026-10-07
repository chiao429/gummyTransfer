import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const [inputPath, workbookPath, versionArgument] = process.argv.slice(2);
if (!inputPath || !workbookPath) {
  throw new Error("Usage: node verify_portfolio.mjs DATA.json OUTPUT.xlsx");
}
const payload = JSON.parse(await fs.readFile(inputPath, "utf8"));
const version = Number(versionArgument ?? payload.workbook_schema_version ?? 3);
if (![1, 2, 3].includes(version)) throw new Error("Unsupported workbook schema version");
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(workbookPath));

function equal(actual, expected, label) {
  if (actual !== expected) throw new Error(`${label}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
}

function text(value) {
  if (value == null || value === "") return null;
  return String(value);
}

function num(value) {
  if (value == null || value === "") return null;
  const result = Number(value);
  if (!Number.isFinite(result)) throw new Error("Non-numeric source value in portfolio payload");
  return result;
}

function key(runId, asin) {
  return `${runId}:${asin}`;
}

function timestampMs(value) {
  // Spreadsheet editors may return a 1900-system Excel serial instead of an ISO date.
  // Normalize floating point day fractions to Date's integer-millisecond precision.
  // Keep the existing one-second editor tolerance; do not widen it for real changes.
  return typeof value === "number" ? Math.round((value - 25569) * 86400000) : new Date(value).getTime();
}

function timestamp(actual, expected, label) {
  if (!expected) {
    equal(actual ?? null, null, label);
    return;
  }
  const observed = timestampMs(actual);
  const source = timestampMs(expected);
  if (!Number.isFinite(observed) || !Number.isFinite(source) || Math.abs(observed - source) > 1000) {
    throw new Error(`${label}: timestamp differs from the source snapshot`);
  }
}

const checkedSheets = new Set();
function noFormulas(sheetName) {
  if (checkedSheets.has(sheetName)) return;
  const formulas = workbook.worksheets.getItem(sheetName).getUsedRange().formulas;
  if (formulas.some((row) => row.some((formula) => formula))) {
    throw new Error(`${sheetName} contains a formula`);
  }
  checkedSheets.add(sheetName);
}

function noUnexpectedData(sheetName, firstRow, expectedRows, expectedColumns) {
  const rows = workbook.worksheets.getItem(sheetName).getUsedRange().values;
  for (let index = firstRow - 1; index < rows.length; index += 1) {
    const row = rows[index] || [];
    const extra = index < firstRow - 1 + expectedRows ? row.slice(expectedColumns) : row;
    if (extra.some((value) => value !== null && value !== undefined && value !== "")) {
      throw new Error(`${sheetName} contains unexpected data outside the verified table`);
    }
  }
}

function productRows(sheetName, expected) {
  noFormulas(sheetName);
  const sheet = workbook.worksheets.getItem(sheetName);
  const rows = sheet.getRange(`A4:R${3 + expected.length}`).values;
  equal(rows.length, expected.length, `${sheetName} row count`);
  const byKey = new Map();
  for (const row of rows) {
    const id = key(row[0], row[2]);
    if (byKey.has(id)) throw new Error(`${sheetName} duplicate product ${id}`);
    byKey.set(id, row);
  }
  for (const item of expected) {
    const id = key(item.run_id, item.asin);
    const row = byKey.get(id);
    if (!row) throw new Error(`${sheetName} missing product ${id}`);
    const fields = [
      [0, num(item.run_id)], [1, text(item.category)], [2, text(item.asin)],
      [3, text(item.title)], [4, text(item.brand)], [5, num(item.price)],
      [6, text(item.currency)], [7, num(item.rating)], [8, num(item.rating_count)],
      [9, text(item.material)], [10, text(item.measurement_system)],
      [11, text(item.title_measurement_candidates)], [12, text(item.size)],
      [13, text(item.color)],
      [14, item.evidence_status === "captured_selected_fields" ? "已留存選定欄位" : "本批未留存來源欄位"],
      [15, text(item.run_verification)], [16, num(item.product_issue_count)],
    ];
    for (const [column, expectedValue] of fields) {
      equal(row[column] ?? null, expectedValue, `${sheetName} ${id} column ${column + 1}`);
    }
    timestamp(row[17], item.observed_at, `${sheetName} ${id} observed at`);
    byKey.delete(id);
  }
  equal(byKey.size, 0, `${sheetName} unexpected products`);
  noUnexpectedData(sheetName, 4, expected.length, 18);
}

const cover = workbook.worksheets.getItem("分類總覽");
noFormulas("分類總覽");
const overview = cover.getRange("A2:G2").values[0];
equal(overview[1], payload.source, "overview source");
equal(overview[3], num(payload.through_run_id), "overview through run");
equal(overview[5], num(payload.run_count), "overview successful runs");
const categories = [
  ...payload.coverage.categories.map((item) => ({...item, type: "原表分類"})),
  ...payload.coverage.outside_reference.map((item) => ({...item, type: "參考表以外"})),
];
const coverRows = cover.getRange(`A5:G${4 + categories.length}`).values;
const categoryTabs = [];
for (let index = 0; index < categories.length; index += 1) {
  const item = categories[index];
  const row = coverRows[index];
  equal(row[0], item.type, `category ${index} type`);
  equal(row[1], item.category, `category ${index} name`);
  equal(row[3], num(item.distinct_product_count), `category ${index} distinct ASINs`);
  equal(row[4], num(item.run_count), `category ${index} run count`);
  equal(row[5] ?? null, num(item.latest_run_id), `category ${index} latest run`);
  if (item.run_count) {
    if (!row[6]) throw new Error(`Sampled category ${item.category} has no tab`);
    const observations = payload.observations.filter((product) => product.category === item.category);
    productRows(row[6], observations);
    categoryTabs.push(row[6]);
  } else {
    equal(row[6] ?? null, null, `unsampled category ${item.category} tab`);
  }
}
noUnexpectedData("分類總覽", 5, categories.length, 7);
const additionalAttributes = payload.additional_attributes ?? [];
if (!Array.isArray(additionalAttributes)) throw new Error("Additional source attributes must be a list");
const expectedSheets = [
  "分類總覽", "規格組合", "跨分類商品", "對帳索引", "來源欄位對照",
  ...(additionalAttributes.length ? ["來源附加欄位"] : []),
  ...(version >= 2 && payload.specification_mappings?.length ? ["規格映射"] : []),
  ...(version >= 2 && payload.review_samples?.length ? [version === 2 ? "評論標註" : "評論來源"] : []), ...categoryTabs,
  ...(payload.web_reviews ? ["網站核對"] : []),
];
const actualSheets = workbook.worksheets.items.map((sheet) => sheet.name);
equal(JSON.stringify(actualSheets), JSON.stringify(expectedSheets), "workbook sheet order and membership");

productRows("跨分類商品", payload.observations);
if (!Array.isArray(payload.combinations)) throw new Error("Portfolio has no specification groups");
const expectedGroups = new Map();
for (const item of payload.observations) {
  const structured = item.measurement_system;
  const candidates = item.title_measurement_candidates.split(";").filter(Boolean);
  const measurements = structured ? [[structured, false]]
    : candidates.length ? candidates.map((value) => [value, true]) : [["", false]];
  for (const [measurement, guessed] of measurements) {
    const groupKey = JSON.stringify([item.category, item.material, measurement, item.size, item.color]);
    if (!expectedGroups.has(groupKey)) expectedGroups.set(groupKey, []);
    expectedGroups.get(groupKey).push({item, guessed});
  }
}
equal(payload.combinations.length, expectedGroups.size, "specification group count");
const combinationSheet = workbook.worksheets.getItem("規格組合");
noFormulas("規格組合");
const combinationRows = combinationSheet.getRange(`A4:N${3 + payload.combinations.length}`).values;
equal(combinationRows.length, payload.combinations.length, "specification sheet row count");
const seenGroups = new Set();
for (let index = 0; index < payload.combinations.length; index += 1) {
  const group = payload.combinations[index];
  const groupKey = JSON.stringify([
    group.category, group.material, group.measurement_system, group.size, group.color,
  ]);
  if (seenGroups.has(groupKey)) throw new Error(`Duplicate specification group ${groupKey}`);
  seenGroups.add(groupKey);
  const members = expectedGroups.get(groupKey);
  if (!members) throw new Error(`Specification group has no matching observations: ${groupKey}`);
  equal(group.observation_count, members.length, `specification group ${index} observation count`);
  equal(group.distinct_asin_count, new Set(members.map(({item}) => item.asin)).size,
    `specification group ${index} distinct ASINs`);
  const prices = members.map(({item}) => num(item.price)).filter((value) => value !== null);
  equal(group.priced_observation_count, prices.length, `specification group ${index} priced observations`);
  if (prices.length) {
    const mean = prices.reduce((total, value) => total + value, 0) / prices.length;
    if (Math.abs(num(group.average_price_usd) - mean) > 0.005001) {
      throw new Error(`Specification group ${index} average price differs`);
    }
  } else equal(group.average_price_usd, null, `specification group ${index} missing price`);
  equal(group.title_candidate_observation_count, members.filter(({guessed}) => guessed).length,
    `specification group ${index} title candidates`);
  equal(group.source_evidence_observation_count,
    members.filter(({item}) => item.evidence_status === "captured_selected_fields").length,
    `specification group ${index} source evidence`);
  equal(group.source_pass_observation_count,
    members.filter(({item}) => item.run_verification === "PASS").length,
    `specification group ${index} source verification`);
  equal(JSON.stringify(group.asins), JSON.stringify([...new Set(members.map(({item}) => item.asin))].sort()),
    `specification group ${index} ASINs`);
  equal(JSON.stringify(group.run_asins), JSON.stringify(members.map(({item}) => `${item.run_id}:${item.asin}`)),
    `specification group ${index} run/ASIN references`);
  const row = combinationRows[index];
  const expected = [
    text(group.category), text(group.material), text(group.measurement_system), text(group.size),
    text(group.color), num(group.observation_count), num(group.distinct_asin_count),
    num(group.priced_observation_count), num(group.average_price_usd),
    num(group.title_candidate_observation_count), num(group.source_evidence_observation_count),
    num(group.source_pass_observation_count), text(group.asins.join("; ")),
    text(group.run_asins.join("; ")),
  ];
  for (let column = 0; column < expected.length; column += 1) {
    equal(row[column] ?? null, expected[column], `specification group ${index} column ${column + 1}`);
  }
}
noUnexpectedData("規格組合", 4, payload.combinations.length, 14);
const audit = workbook.worksheets.getItem("對帳索引");
noFormulas("對帳索引");
const auditRows = audit.getRange(`A4:O${3 + payload.observations.length}`).values;
equal(auditRows.length, payload.observations.length, "audit row count");
for (let index = 0; index < payload.observations.length; index += 1) {
  const item = payload.observations[index];
  const row = auditRows[index];
  const expected = [
    num(item.run_id), text(item.category), text(item.asin),
    item.evidence_status === "captured_selected_fields" ? "已留存選定欄位" : "本批未留存來源欄位",
    text(item.run_verification), num(item.product_issue_count),
    text(item.source_title), text(item.title), text(item.source_price), num(item.price),
  ];
  for (let column = 0; column < expected.length; column += 1) {
    equal(row[column] ?? null, expected[column], `audit ${key(item.run_id, item.asin)} column ${column + 1}`);
  }
  timestamp(row[13], item.observed_at, `audit ${key(item.run_id, item.asin)} observed at`);
  const expectedUrl = payload.source === "rapidapi" && /^[A-Z0-9]{10}$/u.test(item.asin)
    ? `https://www.amazon.com/dp/${item.asin}` : null;
  equal(row[14] ?? null, expectedUrl, `audit ${key(item.run_id, item.asin)} Amazon URL`);
}
noUnexpectedData("對帳索引", 4, payload.observations.length, 15);

const sourceDetail = workbook.worksheets.getItem("來源欄位對照");
noFormulas("來源欄位對照");
const sourceRows = sourceDetail.getRange(`A4:Y${3 + payload.observations.length}`).values;
equal(sourceRows.length, payload.observations.length, "source detail row count");
for (let index = 0; index < payload.observations.length; index += 1) {
  const item = payload.observations[index];
  const fields = item.source_audit;
  if (!fields) throw new Error(`Missing selected source fields for ${key(item.run_id, item.asin)}`);
  const row = sourceRows[index];
  const expected = [
    num(item.run_id), text(item.category), text(item.asin),
    item.evidence_status === "captured_selected_fields" ? "已留存選定欄位" : "本批未留存來源欄位",
    text(fields.source_title), text(fields.stored_title), text(fields.source_price),
    text(fields.stored_price_raw), num(fields.stored_price_usd),
    text(fields.source_brand), text(fields.stored_brand),
    text(fields.source_material), text(fields.stored_material),
    text(fields.source_measurement_system), text(fields.stored_measurement_system),
    text(fields.title_measurement_candidates_unverified),
    text(fields.source_size), text(fields.stored_size),
    text(fields.source_color), text(fields.stored_color),
    text(fields.source_rating), num(fields.stored_rating),
    text(fields.source_rating_count), num(fields.stored_rating_count),
  ];
  for (let column = 0; column < expected.length; column += 1) {
    equal(row[column] ?? null, expected[column], `source detail ${key(item.run_id, item.asin)} column ${column + 1}`);
  }
  timestamp(row[24], fields.observed_at, `source detail ${key(item.run_id, item.asin)} observed at`);
}
noUnexpectedData("來源欄位對照", 4, payload.observations.length, 25);

if (additionalAttributes.length) {
  const sheetName = "來源附加欄位";
  noFormulas(sheetName);
  const extraRows = workbook.worksheets.getItem(sheetName)
    .getRange(`A4:H${3 + additionalAttributes.length}`).values;
  equal(extraRows.length, additionalAttributes.length, "additional attribute row count");
  for (let index = 0; index < additionalAttributes.length; index += 1) {
    const item = additionalAttributes[index];
    const section = ({search_listing: "搜尋結果", product_summary: "商品明細摘要", product_information: "商品資訊", product_details: "商品詳情", top_review_sample: "精選評論樣本"})[item.source_section];
    if (!section) throw new Error(`Unknown source section for additional attribute ${index}`);
    const row = extraRows[index];
    const expected = [
      num(item.run_id), text(item.category), text(item.asin), null,
      section, text(item.field_name), text(item.source_value), "未分類；待人工確認",
    ];
    for (let column = 0; column < expected.length; column += 1) {
      if (column !== 3) equal(row[column] ?? null, expected[column],
        `additional attribute ${index} column ${column + 1}`);
    }
    timestamp(row[3], item.observed_at, `additional attribute ${index} observed at`);
  }
  noUnexpectedData(sheetName, 4, additionalAttributes.length, 8);
}

if (payload.web_reviews) {
  equal(payload.web_reviews.length, payload.observations.length, "web review count");
  noFormulas("網站核對");
  const sheet = workbook.worksheets.getItem("網站核對");
  equal(sheet.getRange("B2").values[0][0], payload.web_review_snapshot, "web review snapshot");
  const rows = sheet.getRange(`A4:J${3 + payload.web_reviews.length}`).values;
  for (let index = 0; index < payload.web_reviews.length; index += 1) {
    const item = payload.web_reviews[index];
    equal(key(item.run_id, item.asin), key(payload.observations[index].run_id, payload.observations[index].asin), "web review identity");
    const expected = [num(item.run_id), text(item.category), text(item.asin), text(item.review_state),
      text(item.review_classification), text(item.review_source), text(item.reviewer),
      num(item.review_revision), null, text(item.review_note)];
    expected.forEach((value, column) => {
      if (column !== 8) equal(rows[index][column] ?? null, value, `web review ${index} column ${column}`);
    });
    timestamp(rows[index][8], item.review_updated_at, `web review ${index} updated at`);
  }
  noUnexpectedData("網站核對", 4, payload.web_reviews.length, 10);
}

if (version >= 2) {
  for (const [name, items, columns, values, dateColumns] of [
    ["規格映射", payload.specification_mappings ?? [], 11, (row) => [
      num(row.run_id), text(row.category), text(row.asin), text(row.source_section), text(row.field_name),
      text(row.source_value), text(row.dimension), text(row.candidate_value), text(row.stored_value),
      text(row.status), text(row.mapping_version),
    ], []],
    [version === 2 ? "評論標註" : "評論來源", payload.review_samples ?? [], version === 2 ? 16 : 10, (row) => version === 2 ? [
      num(row.run_id), text(row.category), text(row.asin), text(row.review_id), num(row.review_star_rating),
      text(row.review_date), text(row.review_title), text(row.review_comment), text(row.theme),
      text(row.sentiment), text(row.annotation_state), text(row.annotation_note), text(row.annotation_reviewer),
      num(row.annotation_revision), row.annotation_updated_at, row.observed_at,
    ] : [
      num(row.run_id), text(row.category), text(row.asin), text(row.review_id), num(row.review_star_rating),
      text(row.review_date), text(row.review_title), text(row.review_comment), row.observed_at, text(payload.source),
    ], version === 2 ? [14, 15] : [8]],
  ]) {
    if (!items.length) continue;
    noFormulas(name);
    const sheet = workbook.worksheets.getItem(name);
    const last = name === "規格映射" ? "K" : version === 2 ? "P" : "J";
    const rows = sheet.getRange(`A4:${last}${3 + items.length}`).values;
    equal(rows.length, items.length, `${name} row count`);
    items.forEach((item, index) => values(item).forEach((expected, column) => {
      if (dateColumns.includes(column)) timestamp(rows[index][column], expected, `${name} ${index} date ${column}`);
      else equal(rows[index][column] ?? null, expected, `${name} ${index} column ${column + 1}`);
    }));
    noUnexpectedData(name, 4, items.length, columns);
    if (name === "評論標註") equal(sheet.getRange("B2").values[0][0], payload.web_review_snapshot ?? 0, "annotation snapshot");
    if (name === "評論來源") equal(sheet.getRange("B2").values[0][0], payload.source, "review source");
  }
}

console.log(JSON.stringify({
  status: "PASS", source: payload.source, through_run_id: payload.through_run_id,
  observations_checked: payload.observations.length, categories_checked: categories.length,
  category_tabs_checked: categoryTabs.length, combination_groups_checked: combinationRows.length,
  source_rows_checked: sourceRows.length,
  additional_attributes_checked: additionalAttributes.length,
  web_review_rows_checked: payload.web_reviews?.length ?? 0,
  workbook_schema_version: version,
  specification_mapping_rows_checked: version >= 2 ? payload.specification_mappings?.length ?? 0 : 0,
  review_sample_rows_checked: version >= 2 ? payload.review_samples?.length ?? 0 : 0,
}));
