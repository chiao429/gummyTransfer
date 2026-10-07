"""Local product, period, export and settings pages. No provider calls or client secrets."""

from datetime import datetime, timedelta
from html import escape
from urllib.parse import urlencode

from gummy.catalog import EXPORT_FIELDS, TAIPEI
from gummy.ui import UI_STYLES, cell, source_label, taipei_time

FIELD_LABELS = dict(
    zip(
        EXPORT_FIELDS,
        [
            "批次",
            "資料來源",
            "模擬資料",
            "搜尋分類",
            "搜尋關鍵字",
            "市場",
            "ASIN",
            "商品名稱",
            "品牌",
            "價格",
            "幣別",
            "評分",
            "評價數",
            "材質",
            "結構化公制／英制",
            "標題制式候選",
            "候選依據",
            "尺寸",
            "顏色",
            "採集時間 UTC",
            "資料提醒",
            "來源證據狀態",
        ],
    )
)
FIELD_LABELS.update(
    dict(
        zip(
            [
                "review_state",
                "review_classification",
                "review_source",
                "review_note",
                "reviewer",
                "review_revision",
                "review_updated_at",
            ],
            [
                "網站核對狀態",
                "網站分類核對",
                "網站來源核對",
                "網站核對備註",
                "填寫人（自填）",
                "核對版本",
                "核對時間 UTC",
            ],
        )
    )
)
STYLES = """
.ops-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;margin:20px 0}.ops-metrics .stat{min-width:0}.ops-metrics .stat strong{font-size:30px;overflow-wrap:anywhere}.ops-metrics .next-date{font-size:21px}.attention{border-left:5px solid #ae7615}.task-steps{padding:0;list-style:none;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:24px 0}.task-steps li{padding:14px;border:1px solid #d9e2dd;border-radius:8px;background:#f6f8f7;min-width:0}.task-steps li span{display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:50%;background:#d9e2dd;margin-right:8px}.task-steps li small{display:block;margin-top:8px;color:#586861}.task-steps .current{border:2px solid #286750;background:#edf6f0}.task-steps .visited{background:#edf6f0}button:disabled{background:#e1e6e3;color:#4a5750;cursor:not-allowed}button.secondary{background:white;color:#245941;border:1px solid #b6cbbf}@media(max-width:1150px){.ops-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:600px){.ops-metrics,.task-steps{grid-template-columns:1fr}}
.analysis-stats{grid-template-columns:repeat(4,minmax(0,1fr))}.analysis-stats .stat{min-width:0}.analysis-stats strong{font-size:32px}
.source-review{min-width:0;border-top:1px solid #d9e2dd;padding:16px 0}.source-review h3{overflow-wrap:anywhere}.sample-text{white-space:pre-wrap;overflow-wrap:anywhere}#source-reviews,#sample-labels{scroll-margin-top:20px}
@media(max-width:1150px){.analysis-stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:600px){.analysis-stats{grid-template-columns:1fr}}
.sidebar a:first-of-type{background:transparent;color:#44574f}.sidebar a[aria-current=page]{background:#e5efea;color:#154d3c}
.primary-filters .field:first-child{grid-column:span 2}.filters{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin:20px 0}.field{min-width:0}.field label{display:block;font-size:14px;font-weight:600;margin-bottom:6px}.field input,.field select{width:100%;min-width:0;font:inherit;min-height:44px;border:1px solid #bbc9c0;border-radius:8px;padding:10px;background:white}.wide{grid-column:span 2}.filter-actions,.toolbar,.pagination{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:18px}.toolbar{justify-content:space-between;margin-bottom:16px}.pagination{justify-content:center}.product-name{font-weight:600;display:block}.metadata{display:block;color:#586861;font-size:13px;margin-top:5px}.nowrap{white-space:nowrap}.empty{padding:36px 20px;text-align:center;background:#f7faf8;border-radius:8px}.empty h2{margin-bottom:12px}.hint{font-size:14px;color:#586861;margin-top:12px}.choices{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.choices label{display:flex;align-items:center;gap:10px;min-height:44px;font-size:14px}.choices input{width:20px;height:20px;flex-shrink:0}.settings-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}.settings-grid .card{margin:0}.facts{margin:20px 0}.facts>div{display:grid;grid-template-columns:130px minmax(0,1fr);gap:16px;border-bottom:1px solid #e5eae7;padding:12px 0}.facts dt{color:#586861}.facts dd{margin:0;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f1f5f2;padding:18px;border-radius:8px;font-size:14px}code{overflow-wrap:anywhere}fieldset{border:1px solid #dce4df;border-radius:8px;padding:18px;margin:20px 0}legend{font-weight:600;padding:0 8px}.table-link{display:inline-flex;align-items:center;min-height:44px}.feedback{padding:20px;border:1px solid #d8a3a3;background:#fff1f1;border-radius:10px}.table-summary{font-size:14px;color:#586861}.table-summary strong{color:#20332f}
@media(max-width:1050px){.filters,.choices{grid-template-columns:repeat(2,minmax(0,1fr))}.settings-grid{grid-template-columns:1fr}}
@media(max-width:600px){.filters,.choices{grid-template-columns:1fr}.wide,.primary-filters .field:first-child{grid-column:auto}.filter-actions>*{width:100%}.facts>div{grid-template-columns:1fr;gap:2px}.toolbar{align-items:start}pre{font-size:13px}}
"""


def url(path, query=None, **updates):
    values = query.model_dump(mode="json") if query else {}
    values.update(updates)
    params = {key: value for key, value in values.items() if value is not None and value != ""}
    return escape(path + ("?" + urlencode(params, doseq=True) if params else ""), quote=True)


def hidden(query, exclude=()):
    values = query.model_dump(mode="json")
    return "".join(
        f'<input type="hidden" name="{key}" value="{escape(str(item), quote=True)}">'
        for key, value in values.items()
        if key not in exclude and value is not None and value != ""
        for item in (value if isinstance(value, list) else [value])
    )


def shell(title, active, source, body, refresh_seconds=None):
    items = [
        ("/", "營運總覽"),
        ("/tasks", "任務與排程"),
        ("/schedules", "排程管理"),
        ("/products", "商品資料"),
        ("/reports", "報表中心"),
        ("/periods", "批次歷史"),
        ("/analysis", "取樣分析"),
        ("/reviews", "來源查驗"),
        ("/settings", "設定中心"),
    ]
    links = "".join(
        f'<a href="{url(path.split("#")[0], source=source)}{("#" + path.split("#")[1]) if "#" in path else ""}" {"aria-current=page" if path == active else ""}>{label}</a>'
        for path, label in items
    )
    refresh = (
        f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">' if refresh_seconds else ""
    )
    return f'''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">{refresh}<title>{title} · gummy</title><style>{UI_STYLES}{STYLES}</style></head><body>
<a class="skip" href="#main">跳至主要內容</a><header class="topbar"><div class="topbar-inner"><a class="brandmark" href="{url("/", source=source)}"><span class="brand-symbol" aria-hidden="true">g</span>gummy <small>Amazon 商品研究</small></a><span class="local-label">本機版 · {source_label(source)}</span></div></header>
<div class="layout"><nav class="sidebar" aria-label="工作台導覽"><p>商品研究工作台</p>{links}<div class="sidebar-note">查詢與下載只讀取已存資料。<br>立即採集需先預覽與確認。</div></nav><main id="main"><div class="page-heading"><div><p class="eyebrow">GUMMY / RESEARCH WORKSPACE</p><h1>{title}</h1></div><span class="badge neutral">{source_label(source)}</span></div>{body}<footer class="footer">資料是有限取樣，並非完整市場。價格以採集時間為準；分類與標題候選仍需人工核對。</footer></main></div></body></html>'''


def select_field(name, label, selected, options):
    opts = "".join(
        f'<option value="{escape(str(value), quote=True)}" {"selected" if str(value) == str(selected) else ""}>{cell(text)}</option>'
        for value, text in options
    )
    return f'<div class="field"><label for="{name}">{label}</label><select id="{name}" name="{name}">{opts}</select></div>'


def input_field(name, label, value, kind="text", extra=""):
    return f'<div class="field"><label for="{name}">{label}</label><input id="{name}" name="{name}" type="{kind}" value="{escape(str(value) if value is not None else "", quote=True)}" {extra}></div>'


def period_fields(query):
    return (
        select_field(
            "source", "資料來源", query.source, [("rapidapi", "真實資料"), ("demo", "模擬資料")]
        )
        + input_field(
            "date_from",
            "開始日期（台灣）",
            query.date_from,
            "date",
            'min="1900-01-01" max="9998-12-31"',
        )
        + input_field(
            "date_to",
            "結束日期（含當日）",
            query.date_to,
            "date",
            'min="1900-01-01" max="9998-12-31"',
        )
    )


def scope_note(data):
    query = data["query"]
    return f'<p class="hint">期間：{cell(query.date_from or "最早")} ～ {cell(query.date_to or "目前")}，依批次完成的台灣日期計算。已固定 {len(data["runs"])} 個成功批次；重新套用篩選或更新資料時，才會納入新的批次。搜尋分類是採集任務標籤。</p>'


def table(rows, source):
    if not rows:
        return '<div class="empty"><h2>目前沒有符合的商品</h2><p>請調整日期、分類或價格，或清除篩選條件。查不到商品不代表已下架。</p></div>'
    body = ""
    for row in rows:
        audit_url = url(f"/reviews/{row['run_id']}/{row['asin']}", source=source)
        rating_count = row["rating_count"]
        count_text = "未提供" if rating_count is None else f"{rating_count:,}"
        body += f'''<tr><td class="title"><span class="product-name">{cell(row["title"])}</span><span class="metadata">{cell(row["brand"])} · {cell(row["asin"])}</span></td><td>{cell(row["category"])}<span class="metadata">批次 #{row["run_id"]}</span></td><td class="nowrap">{cell(row["price"])}<span class="metadata">{cell(row["currency"])}</span></td><td class="nowrap">{cell(row["rating"])}<span class="metadata">評價數 {count_text}</span></td><td class="nowrap">{taipei_time(row["observed_at"])}<span class="metadata">台灣時間</span></td><td><span class="badge {"green" if row["evidence_status"] == "captured" else "amber"}">{"已留存" if row["evidence_status"] == "captured" else "未留存"}</span></td><td><a class="table-link" href="{audit_url}">查看來源</a></td></tr>'''
    return f'<div class="scroll" role="region" tabindex="0" aria-label="商品資料表，可左右捲動"><table><thead><tr><th>商品／品牌／ASIN</th><th>搜尋分類</th><th>採集時價格</th><th>評分／評價數（擷取時）</th><th>採集時間</th><th>來源證據</th><th>詳細</th></tr></thead><tbody>{body}</tbody></table></div>'


def products_page(data):
    query, facets = data["query"], data["facets"]
    controls = input_field("q", "搜尋名稱、ASIN 或品牌", query.q, extra='maxlength="120"')
    controls += select_field(
        "view",
        "顯示方式",
        query.view,
        [("latest", "每件商品最新一筆"), ("observations", "全部歷史採集紀錄")],
    )
    advanced = select_field(
        "sort",
        "排序",
        query.sort,
        [
            ("newest", "最近採集優先"),
            ("price_asc", "價格由低到高"),
            ("price_desc", "價格由高到低"),
            ("title", "商品名稱"),
        ],
    )
    for key, label in (
        ("category", "搜尋分類"),
        ("brand", "品牌"),
        ("material", "材質"),
        ("measurement_system", "結構化公制／英制"),
    ):
        options = sorted(
            set(facets[key]) | ({getattr(query, key)} if getattr(query, key) else set())
        )
        control = select_field(
            key, label, getattr(query, key), [("", "全部"), *((x, x) for x in options)]
        )
        if key == "category":
            controls += control
        else:
            advanced += control
    advanced += input_field(
        "min_price",
        "最低價格 USD",
        query.min_price,
        "number",
        'min="0" max="99999999.99" step="0.01"',
    )
    advanced += input_field(
        "max_price",
        "最高價格 USD",
        query.max_price,
        "number",
        'min="0" max="99999999.99" step="0.01"',
    )
    controls += select_field(
        "source", "資料來源", query.source, [("rapidapi", "真實資料"), ("demo", "模擬資料")]
    )
    advanced += input_field(
        "date_from",
        "開始日期（台灣）",
        query.date_from,
        "date",
        'min="1900-01-01" max="9998-12-31"',
    )
    advanced += input_field(
        "date_to", "結束日期（含當日）", query.date_to, "date", 'min="1900-01-01" max="9998-12-31"'
    )
    advanced_open = (
        any(
            getattr(query, key) not in (None, "")
            for key in (
                "brand",
                "material",
                "measurement_system",
                "min_price",
                "max_price",
                "date_from",
                "date_to",
            )
        )
        or query.sort != "newest"
    )
    batch = (
        f'<p class="note">目前限定批次 #{query.run_id}。<a href="{url("/products", query, run_id=None, page=1)}">查看同期間全部批次</a></p>'
        if query.run_id
        else ""
    )
    body = f'''<p class="muted">搜尋已抓取的商品，查閱來源，再匯出你需要的範圍。</p><p><a class="table-link" href="#results">查看 {data["total"]} 筆查詢結果</a></p><section class="card"><h2>找到要研究的商品</h2>{batch}<form method="get" action="/products">{hidden(query, exclude=[key for key in type(query).model_fields if key != "run_id"])}<div class="filters primary-filters">{controls}</div><details {"open" if advanced_open else ""}><summary>更多篩選：日期、價格、品牌與規格</summary><div class="filters">{advanced}</div></details><div class="filter-actions"><button type="submit">套用篩選</button><a class="button secondary" href="{url("/products", source=query.source)}">清除篩選</a></div></form><p class="hint">「最新一筆」先在指定日期及搜尋分類內選出每件商品最新紀錄，再套用價格等條件。制式篩選只採用結構化值，不把標題猜測當成已確認規格。</p></section>
<section class="card" id="results"><div class="toolbar"><div><h2>查詢結果</h2><p class="table-summary"><strong>{data["total"]}</strong> 筆紀錄 · {data["distinct_asins"]} 件不重複商品 · {"每件商品最新一筆" if query.view == "latest" else "全部歷史採集紀錄"}</p></div><a class="button" href="{url("/exports/preview", query, page=1)}">預覽與匯出</a><a class="button secondary" href="{url("/analysis", query, view="latest", page=1)}">查看取樣統計</a></div>{scope_note(data)}<p class="hint"><a class="table-link" href="{url("/products", query, snapshot=None, review_snapshot=None, page=1)}">更新至最新資料</a></p>{table(data["page_rows"], query.source)}<nav class="pagination" aria-label="商品分頁">'''
    if data["page"] > 1:
        body += f'<a class="button secondary" href="{url("/products", query, page=data["page"] - 1)}">上一頁</a>'
    body += f"<span>第 {data['page']} / {data['pages']} 頁</span>"
    if data["page"] < data["pages"]:
        body += f'<a class="button secondary" href="{url("/products", query, page=data["page"] + 1)}">下一頁</a>'
    body += "</nav></section>"
    return shell("商品資料", "/products", query.source, body)


def export_page(data):
    query = data["query"]
    choices = "".join(
        f'<label><input type="checkbox" name="columns" value="{field}" {"checked" if field in query.columns else ""}>{FIELD_LABELS[field]}</label>'
        for field in EXPORT_FIELDS
    )
    filters = (
        "；".join(
            f"{label}：{cell(getattr(query, field))}"
            for field, label in (
                ("q", "關鍵字"),
                ("category", "分類"),
                ("brand", "品牌"),
                ("material", "材質"),
                ("measurement_system", "制式"),
                ("min_price", "最低 USD"),
                ("max_price", "最高 USD"),
                ("run_id", "批次"),
            )
            if getattr(query, field) is not None and getattr(query, field) != ""
        )
        or "無額外篩選"
    )
    body = f'''<section class="card"><span class="badge green">只使用已存資料</span><h2>確認這次的匯出範圍</h2><p><strong>{data["total"]} 筆紀錄</strong>，{data["distinct_asins"]} 件不重複商品。會下載所有符合條件的紀錄，包含其他分頁。</p><p>{"每件商品最新一筆" if query.view == "latest" else "全部歷史採集紀錄"} · {source_label(query.source)}</p><p>{filters}</p>{scope_note(data)}<p class="hint">採集資料含尚待人工確認的商品；可勾選網站核對欄位，核對狀態、版本與時間會一併保存；預覽後新增的修改不會混入本次下載。Excel 工作檔中的人工結論尚未匯入網站，標題候選仍為未驗證。來源、批次範圍、日期與顯示方式會隨檔案保存。</p>
<form method="get" action="/v1/exports/catalog.csv">{hidden(query, exclude=("columns", "page"))}<input type="hidden" name="columns" value=""><fieldset><legend>選擇 CSV 欄位（至少一項）</legend><div class="choices">{choices}</div></fieldset><div class="filter-actions">{('<button type="submit">下載 ' + str(data["total"]) + " 筆 CSV</button>") if data["total"] else "<span>沒有符合資料，請先調整篩選。</span>"}<a class="button secondary" href="{url("/products", query)}">返回商品資料</a></div></form></section><section class="card"><h2>資料預覽（前 5 筆）</h2>{table(data["rows"][:5], query.source)}<p class="hint">預覽表為固定摘要欄位；下載內容依上方勾選欄位輸出。既有累計 Excel 可到「每期報表」下載。</p></section>'''
    return shell("預覽與匯出", "/products", query.source, body)


def periods_page(data, plan):
    query = data["query"]
    today = datetime.now(TAIPEI).date()
    presets = [("最近 7 天", today - timedelta(days=6)), ("本月", today.replace(day=1))]
    links = "".join(
        f'<a class="button secondary" href="{url("/periods", source=query.source, date_from=start, date_to=today)}">{label}</a>'
        for label, start in presets
    )
    reports = (
        {
            batch["run_id"]: batch.get("report")
            for batch in plan.get("batches", [])
            if batch.get("run_id")
        }
        if plan.get("source") == query.source
        else {}
    )
    rows = ""
    for run in sorted(data["runs"], key=lambda x: (x["completed_at"], x["id"]), reverse=True):
        entries = [row for row in data["rows"] if row["run_id"] == run["id"]]
        report = reports.get(run["id"])
        download = (
            f'<a class="table-link" href="{url("/v1/exports/workbooks/" + str(run["id"]) + ".xlsx", source=query.source)}">下載累計 Excel 副本</a><span class="metadata">{"讀回驗證通過" if report["content_verified"] else "內容仍需核對"}</span>'
            if report and report["exists"]
            else "尚無對應計畫報表"
        )
        rows += f'<tr><td>{taipei_time(run["completed_at"])}<span class="metadata">批次 #{run["id"]}</span></td><td>{cell(run["parameters"]["category"])}</td><td>{len(entries)} 筆</td><td><a class="table-link" href="{url("/products", query, run_id=run["id"], view="observations", page=1)}">查看當批商品</a></td><td>{download}</td></tr>'
    body = f'''<p class="muted">依日期找回採集紀錄，或下載已產生的累計報表。</p><section class="card"><h2>選擇資料期間</h2><form method="get" action="/periods"><div class="filters">{period_fields(query)}</div><button type="submit">查詢期間</button></form><div class="filter-actions">{links}<a class="button secondary" href="{url("/periods", source=query.source)}">全部期間</a></div><p class="hint">只列出該期間完成的成功批次；沒有資料的日期不會自動補抓。失敗或漏跑請到總覽的採集排程查看。</p></section>
<section class="card"><div class="toolbar"><div><h2>期間內的採集紀錄</h2><p>{len(data["runs"])} 個批次 · {data["total"]} 筆觀測 · {data["distinct_asins"]} 件不重複商品</p></div><a class="button" href="{url("/analysis", query, view="latest", page=1)}">查看期間取樣統計</a><a class="button secondary" href="{url("/exports/preview", query, view="observations", page=1)}">匯出期間資料</a></div>{scope_note(data)}<p class="hint"><a class="table-link" href="{url("/periods", query, snapshot=None)}">更新至最新資料</a></p><p class="note">「匯出期間資料」只含上述期間。「累計 Excel」則包含截至該批次的歷史資料，兩者範圍不同；下載的是既有檔案副本，不會重產或回存人工修改。</p>{('<div class="scroll" tabindex="0" role="region" aria-label="期間批次，可左右捲動"><table><thead><tr><th>完成時間（台灣）</th><th>搜尋分類</th><th>採集數</th><th>本批資料</th><th>截至該批次的累計報表</th></tr></thead><tbody>' + rows + "</tbody></table></div>") if rows else '<div class="empty">所選期間沒有成功批次，請調整日期。</div>'}</section>'''
    return shell("每期報表", "/periods", query.source, body)


def settings_page(status, quota, provider, latest, plan):
    diff = (
        '<p class="note">本機檔案與目前網站載入的金鑰不同。新啟動的採集程序會重新讀取設定；若有環境變數覆蓋，應先確認其來源。重啟網站後再確認狀態。</p>'
        if status["local_differs_from_runtime"]
        else ""
    )
    local = "已設定" if status["local_key_configured"] else "未設定"
    if not status["local_file_readable"]:
        local = "無法安全讀取"
    remaining = (
        f"{provider['remaining_estimate']} 次（{taipei_time(provider['last_header_at'])} 觀測）"
        if provider
        else "尚無觀測紀錄"
    )
    schedule_labels = {
        "LOADED_AND_MATCHED": "已載入，設定一致",
        "NOT_LOADED": "未載入",
        "CONFIG_MISMATCH": "設定不符，需處理",
        "UNKNOWN": "尚無法確認",
    }
    body = f"""<p class="muted">確認資料來源、使用量與存取方式。這裡不會發出供應商請求。</p><div class="settings-grid"><section class="card"><div class="section-heading"><h2>資料來源金鑰</h2><span class="badge neutral">RapidAPI</span></div><dl class="facts"><div><dt>網站啟動時設定</dt><dd>{"已載入金鑰" if status["runtime_key_configured"] else "未設定"}</dd></div><div><dt>本機設定檔</dt><dd>{local}</dd></div><div><dt>免費方案確認</dt><dd>{"已記錄使用者確認" if status["free_plan_confirmed"] else "尚未確認"}</dd></div><div><dt>最近成功採集</dt><dd>{taipei_time(latest["completed_at"]) if latest else "尚無成功採集"}</dd></div></dl>{diff}<p class="hint">「已設定」只代表金鑰存在，不代表目前有效。最近成功採集屬於歷史紀錄，也不代表新換的金鑰已驗證。</p><a class="button secondary" href="#configure">設定／更換金鑰</a></section>
<section class="card"><h2>額度與成本</h2><dl class="facts"><div><dt>本機累計</dt><dd>{quota["total_used"]} / {quota["total_limit"]} 次</dd></div><div><dt>本機當日</dt><dd>{quota["daily_used"]} / {quota["daily_limit"]} 次<br><small>帳本日 {quota["day"]}（UTC）</small></dd></div><div><dt>供應商最後估計</dt><dd>{remaining}</dd></div></dl><p class="hint">本機累計不是每月額度。供應商估計可能已過時，其他程式用量不一定已反映；更換金鑰不重設本機帳本。</p><p class="hint">查詢、來源對帳與下載：0 次外部請求。</p></section></div>
<section class="card" id="configure" style="margin-top:20px"><h2>安全設定／更換 RapidAPI 金鑰</h2><ol><li>在本機終端貼上以下兩行，啟動設定精靈。</li><li>依提示輸入兩次金鑰；文字不會顯示，也不用貼到聊天。</li><li>精靈只更新金鑰。完成後重啟網站程序，再確認此頁狀態。</li></ol><pre><code>cd /Users/eddiechu/Program/Rapid
.venv/bin/gummy configure-rapidapi</code></pre><p class="hint">使用本機 .env，設定檔僅擁有者可讀寫。儲存不測試連線、不啟用新排程，也不調高額度。供應商金鑰撤銷仍需到 RapidAPI 操作。</p><p class="note">目前網站沒有管理員登入，因此金鑰由本機安全輸入。網頁內保存金鑰與會消耗額度的連線測試尚未開放。</p></section>
<div class="settings-grid"><section class="card"><h2>網站存取權限</h2><p>{"已啟用 gummy 共用存取 Token" if status["api_access_protected"] else "目前未設定 gummy 存取 Token，限本機使用"}</p><p class="hint">RapidAPI 金鑰用來採集；gummy Token 用來保護本站 API，兩者不同。目前沒有個人登入、成員權限或對外 Token 管理。</p></section><section class="card"><h2>現有採集安排</h2><p>採集：{cell(plan.get("scheduled_time_local"))} · {schedule_labels.get(plan.get("schedule_status"), "尚未設定")}</p><p>報表：{cell(plan.get("report_scheduled_time_local"))} · {schedule_labels.get(plan.get("report_schedule_status"), "尚未設定")}</p><p class="hint">以上時間為既有本機安排；本版不會延長計畫、補抓歷史日期或更改額度。</p><a class="table-link" href="/?source=rapidapi#schedule">查看每日執行狀態</a></section></div>"""
    return shell("設定中心", "/settings", "rapidapi", body)
