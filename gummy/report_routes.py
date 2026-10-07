"""One fixed scope for web summaries, product lists, CSV and versioned XLSX."""

import secrets
from html import escape
from typing import Annotated

from fastapi import HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response

from gummy.analysis import AnalysisQuery, analysis_csv
from gummy.errors import RunConflict
from gummy.local_forms import LocalForms
from gummy.models import SourceName
from gummy.operations import TaskInput, admission, enqueue, task_view
from gummy.operations_ui import STATUSES
from gummy.report_center import center_rows, fixed_report_data, prepare_report, version
from gummy.ui import cell, taipei_time
from gummy.workspace_ui import input_field, select_field, shell, table, url


def summary_html(data):
    s = data["summary"]
    metrics = [
        ("取樣商品", s["product_count"]),
        ("取樣均價 USD", s["average_price"] if s["average_price"] is not None else "未提供"),
        (
            "已知累計評價數",
            s["known_rating_count_sum"] if s["known_rating_count_sum"] is not None else "未提供",
        ),
        ("留存評論樣本", s["retained_review_sample_count"]),
    ]
    metrics = "".join(
        f'<div class="stat"><span>{label}</span><strong>{cell(value)}</strong></div>'
        for label, value in metrics
    )
    rows = "".join(
        f"<tr><td>{cell(g['group_value'])}</td><td>{cell(g['price_band_label'] or '全部')}</td><td>{g['product_count']}</td><td>{cell(g['average_price'])}</td><td>{cell(g['known_rating_count_sum'])}</td><td>{g['retained_review_sample_count']}</td></tr>"
        for g in data["groups"]
    )
    return f'<div class="ops-metrics">{metrics}</div><p class="hint">每個國家／ASIN 取期間內最新一筆，再篩選。評價數為來源累計數，不是本期新增評論；評論樣本非完整評論。銷量與銷售金額未提供。</p><div class="scroll" role="region" tabindex="0" aria-label="固定範圍取樣統計，可左右捲動"><table><thead><tr><th>分類／品牌</th><th>價格帶</th><th>商品數</th><th>均價 USD</th><th>已知累計評價數</th><th>評論樣本數</th></tr></thead><tbody>{rows or "<tr><td colspan=6>沒有符合的資料</td></tr>"}</tbody></table></div>'


def center_page(data, listings, token):
    q = data["query"]
    versions, daily, legacy = listings
    filters = select_field(
        "source", "資料來源", q.source, [("rapidapi", "真實資料"), ("demo", "模擬資料")]
    )
    filters += input_field("date_from", "開始日期（台灣）", q.date_from, "date") + input_field(
        "date_to", "結束日期（含當日）", q.date_to, "date"
    )
    filters += select_field(
        "category",
        "搜尋分類",
        q.category,
        [("", "全部分類")] + [(c, c) for c in data["facets"]["category"]],
    )
    filters += input_field("brand", "品牌", q.brand, extra='maxlength="120"') + input_field(
        "q", "商品名稱或 ASIN", q.q, extra='maxlength="120"'
    )
    filters += select_field(
        "group_by",
        "統計方式",
        q.group_by,
        [("category_price_band", "分類 × 價格帶"), ("category", "分類"), ("brand", "品牌")],
    )
    entries = ""
    for row in versions:
        scope = row["scope"]
        status = "已驗證" if row["verified"] else STATUSES.get(row["status"], "需確認")
        entries += f'<tr><td><a class="table-link" href="{url("/reports/" + row["id"], source=q.source)}">版本 {row["version"]} · {cell(row["id"][:8])}</a></td><td>{cell(scope.get("date_from") or "最早")} ～ {cell(scope.get("date_to") or "截至固定批次")}<span class="metadata">{cell(scope.get("category") or "全部分類")}</span></td><td>{cell(scope["snapshot"])}</td><td>{cell(status)}</td><td>{taipei_time(row["created_at"])}</td></tr>'
    older = "".join(
        f'<tr><td>{cell(b["date"])}</td><td>{cell(b["category"])}</td><td>截至批次 #{b["run_id"]} 的累計資料</td><td>{"已驗證" if b["report"]["content_verified"] else "內容待核對"}</td><td><a class="table-link" href="{url("/v1/exports/workbooks/" + str(b["run_id"]) + ".xlsx", source=q.source)}">下載原每日 Excel</a></td></tr>'
        for b in daily
    )
    recurring_reports = [r for r in legacy if r["workbook_version"] == 5]
    legacy = [r for r in legacy if r["workbook_version"] != 5]
    recurring_table = "".join(
        f'<tr><td>{taipei_time(r["created_at"])}</td><td>本次多分類批次（非累計）</td><td><a href="{url("/v1/operations/reports/" + r["task_id"] + ".xlsx", source=q.source)}">下載 Excel</a> · <a href="{url("/tasks/" + r["task_id"], source=q.source)}">執行來源與完整性</a></td></tr>'
        for r in recurring_reports
    )
    older += "".join(
        f'<tr><td>{taipei_time(r["created_at"])}</td><td>網站累計報表</td><td>截至批次 #{r["through_run_id"]}</td><td>保留原範圍</td><td><a class="table-link" href="{url("/v1/operations/reports/" + r["task_id"] + ".xlsx", source=q.source)}">下載原版本</a></td></tr>'
        for r in legacy
    )
    action = f'<form method="post" action="/reports/preview"><input type="hidden" name="token" value="{escape(token, quote=True)}"><button {"disabled" if not data["rows"] else ""}>預覽並保存本期 Excel（0 次 API）</button></form>'
    body = f"""<section class="card"><h2>選擇本期內容</h2><form method="get" action="/reports"><div class="filters">{filters}</div><button>查詢並固定資料範圍</button></form><p class="hint">查詢時固定成功批次與網站核對版本。新的採集不會加入這次預覽；再次查詢才更新。日期依批次完成的台灣日期。</p><p>固定批次：{cell(q.snapshot)} · 網站核對截止：{q.review_snapshot}</p>{summary_html(data)}<div class="filter-actions">{action}</div></section><section class="card"><h2>本期商品明細</h2><p>共 {data["total"]} 件，畫面顯示前 {len(data["page_rows"])} 件；保存及匯出包含全部符合商品。</p>{table(data["page_rows"], q.source)}</section><section class="card"><h2>保存的期間報表與版本</h2><p class="hint">顯示此來源最近 100 份期間報表。點入版本可看原範圍、下載或以相同資料重產。版本不覆寫。</p><div class="scroll" role="region" tabindex="0" aria-label="報表版本，可左右捲動"><table><thead><tr><th>版本</th><th>期間／分類</th><th>固定批次</th><th>結果</th><th>建立時間（台灣）</th></tr></thead><tbody>{entries or '<tr><td colspan="5">尚未保存期間報表。上方查詢後即可建立。</td></tr>'}</tbody></table></div></section><section class="card"><h2>多分類排程報表</h2><p>只包含各次執行成功的分類；不完整時會在 Excel 內標明。可從執行結果查看原規則與所有分類。</p><div class="scroll"><table><thead><tr><th>產生時間</th><th>資料範圍</th><th>操作</th></tr></thead><tbody>{recurring_table or '<tr><td colspan="3">尚未產生多分類報表。</td></tr>'}</tbody></table></div></section><section class="card"><h2>原每日與累計報表</h2><p class="hint">原報表是累計資料，不一定等於上方選定期間；保留原格式與檔案。</p><div class="scroll"><table><thead><tr><th>日期</th><th>分類</th><th>原資料範圍</th><th>結果</th><th>下載</th></tr></thead><tbody>{older or '<tr><td colspan="5">目前沒有原報表。</td></tr>'}</tbody></table></div></section>"""
    return shell("報表中心", "/reports", q.source, body)


def preview_page(spec, data, check, token):
    reasons = "".join(f"<li>{cell(r)}</li>" for r in check["reasons"])
    gate = (
        f'<div class="feedback" role="alert"><ul>{reasons}</ul></div>'
        if reasons
        else '<p class="note">可建立背景報表工作。只讀已存資料，API 用量 0 次。</p>'
    )
    action = (
        f'<form method="post" action="/reports/confirm"><input type="hidden" name="token" value="{escape(token, quote=True)}"><button>確認保存新版本（0 次 API）</button></form>'
        if check["allowed"]
        else "<button disabled>目前無法建立報表</button>"
    )
    return shell(
        "報表保存預覽",
        "/reports",
        spec.source,
        f'<section class="card"><h2>確認固定內容</h2>{gate}<p>固定成功批次：{cell(data["query"].snapshot)} · 資料表版本 4 · {"原版本重產" if spec.parent_report else "新期間報表"}</p>{summary_html(data)}<p>Excel 包含報表說明、取樣統計、商品明細、批次來源四頁。取樣方法、來源缺值與累計評價數的定義會一併保存。</p><div class="filter-actions">{action}<a class="button secondary" href="{url("/reports", source=spec.source)}">返回報表中心</a></div></section>',
    )


def install_report_routes(app, db, settings, protected, respond):
    forms = LocalForms("report")

    @app.get("/reports", dependencies=protected)
    def center(request: Request, query: Annotated[AnalysisQuery, Query()]):
        try:
            data, _ = fixed_report_data(db, query.model_dump(mode="json"))
        except ValueError:
            raise HTTPException(422, "請調整日期、分類或資料範圍。") from None
        return forms.response(
            request,
            respond,
            lambda s: center_page(
                data,
                center_rows(db, settings, query.source),
                forms.seal(s, "preview", {"scope": data["query"].model_dump(mode="json")}),
            ),
        )

    def report_for(report_id, source):
        try:
            row = version(db, report_id, source)
            data, digest = fixed_report_data(db, row["scope"])
            if digest != row["data_digest"]:
                raise ValueError("資料變更")
            return row, data
        except ValueError:
            raise HTTPException(
                409, "找不到版本或固定來源已改變，請重新查詢；原檔仍保留。"
            ) from None

    @app.get("/reports/{report_id}", dependencies=protected)
    def detail(request: Request, report_id: str, source: SourceName = "rapidapi"):
        row, data = report_for(report_id, source)
        task = task_view(db, report_id)

        def render(session):
            token = forms.seal(session, "preview", {"parent": report_id, "scope": row["scope"]})
            download = (
                f'<a class="button" href="{url("/v1/operations/reports/" + report_id + ".xlsx", source=source)}">下載原版本 Excel</a>'
                if task["report"] and task["report"]["verified"]
                else "<p>報表尚未完成或檔案待核對，請查看任務。</p>"
            )
            body = f'''<section class="card"><h2>版本 {row["version"]} · {cell(report_id[:8])}</h2><p>建立：{taipei_time(row["created_at"])} · 任務：{cell(STATUSES.get(task["status"], "需確認"))} · 資料表版本 4</p><p>期間：{cell(row["scope"].get("date_from") or "最早")} ～ {cell(row["scope"].get("date_to") or "截至固定批次")} · 分類：{cell(row["scope"].get("category") or "全部")}</p><p>固定批次：{cell(row["scope"]["snapshot"])} · {len(data["runs"])} 批 · 來源：{cell(source)}</p><p>源版本：{cell(row["parent_id"] or "新建")} · 每次重產另建版本，原檔保留。</p>{summary_html(data)}<div class="filter-actions">{download}<a class="button secondary" href="{url("/reports/" + report_id + "/products.csv", source=source)}">下載同範圍商品 CSV</a><a class="button secondary" href="{url("/reports/" + report_id + "/statistics.csv", source=source)}">下載同範圍統計 CSV</a><a class="table-link" href="{url("/tasks/" + report_id, source=source)}">查看背景任務</a></div><form method="post" action="/reports/preview"><input type="hidden" name="token" value="{escape(token, quote=True)}"><button>預覽相同範圍重產（0 次 API）</button></form><details><summary>完整固定查詢條件</summary><pre>{cell(__import__("json").dumps(row["scope"], ensure_ascii=False, indent=2))}</pre></details></section><section class="card"><h2>原範圍商品</h2>{table(data["page_rows"], source)}</section>'''
            return shell(
                "報表版本明細",
                "/reports",
                source,
                body,
                refresh_seconds=5 if task["status"] in {"QUEUED", "RUNNING"} else None,
            )

        return forms.response(request, respond, render)

    @app.get("/reports/{report_id}/{kind}.csv", dependencies=protected)
    def csv(report_id: str, kind: str, source: SourceName = "rapidapi"):
        if kind not in {"products", "statistics"}:
            raise HTTPException(404, "不支援的格式")
        _, data = report_for(report_id, source)
        return Response(
            analysis_csv(data, "products" if kind == "products" else "groups"),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="gummy-{report_id}-{kind}.csv"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/reports/preview", dependencies=protected)
    async def preview(request: Request):
        values = await forms.fields(request)
        if set(values) != {"token"}:
            raise HTTPException(422, "格式不符")
        signed = forms.unseal(request, values["token"], "preview")
        try:
            spec, data = prepare_report(db, AnalysisQuery.model_validate(signed["scope"]))
            if signed.get("parent"):
                old = version(db, signed["parent"], spec.source)
                if old["scope"] != spec.report_scope or old["data_digest"] != spec.report_digest:
                    raise ValueError("原版本不同")
                spec = spec.model_copy(update={"parent_report": old["id"]})
            check = admission(db, settings, spec)
        except ValueError:
            raise HTTPException(409, "資料範圍已變更或無商品，請重新查詢。") from None
        return forms.response(
            request,
            respond,
            lambda s: preview_page(
                spec,
                data,
                check,
                forms.seal(
                    s,
                    "confirm",
                    {
                        "spec": spec.model_dump(mode="json"),
                        "fingerprint": check["fingerprint"],
                        "id": secrets.token_hex(16),
                    },
                ),
            ),
        )

    @app.post("/reports/confirm", dependencies=protected)
    async def confirm(request: Request):
        values = await forms.fields(request)
        if set(values) != {"token"}:
            raise HTTPException(422, "格式不符")
        signed = forms.unseal(request, values["token"], "confirm")
        try:
            task_id = enqueue(
                db,
                settings,
                TaskInput.model_validate(signed["spec"]),
                signed["id"],
                signed["fingerprint"],
            )
        except (ValueError, RunConflict):
            raise HTTPException(409, "資料或工作狀態已改變，請重新預覽。") from None
        return RedirectResponse("/reports/" + task_id + "?source=" + signed["spec"]["source"], 303)
