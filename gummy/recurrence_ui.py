"""Accessible server-rendered recurring schedule forms and history."""

# ruff: noqa: E501
import secrets
from datetime import UTC, datetime, timedelta
from html import escape

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from gummy.errors import RunConflict
from gummy.local_forms import LocalForms
from gummy.models import SourceName
from gummy.recurrence import (
    TZ,
    Rule,
    active,
    budget,
    fingerprint,
    forecast,
    get_rule,
    history,
    next_times,
    occurrence,
    overview,
    run_now,
    save_rule,
    versions,
)
from gummy.ui import cell, taipei_time
from gummy.workspace_ui import input_field, select_field, shell

LABELS = {
    "QUEUED": "排隊中",
    "RUNNING": "執行中",
    "SUCCEEDED": "完成",
    "PARTIAL": "部分完成",
    "FAILED": "採集失敗",
    "BLOCKED": "額度或供應商條件不足，未執行",
    "MISSED": "錯過時間，未補抓",
    "SKIPPED": "已跳過",
    "CANCELLED": "已取消",
    "DATA_READY": "資料已存，報表待處理",
    "INTERRUPTED": "執行中斷，未自動重試",
}
STATE = {"enabled": "啟用", "paused": "暫停", "archived": "封存"}
WEEK = ["一", "二", "三", "四", "五", "六", "日"]


def describe(spec):
    freq = {
        "daily": "每天",
        "weekly": "每週" + "、".join(WEEK[i] for i in spec.weekdays),
        "once": str(spec.start) + "，僅一次",
    }[spec.frequency]
    return freq + " " + spec.at


def table(headers, rows):
    return (
        '<div class="scroll" role="region" tabindex="0" aria-label="排程資料表，可左右捲動"><table><thead><tr>'
        + "".join('<th scope="col">' + cell(h) + "</th>" for h in headers)
        + "</tr></thead><tbody>"
        + (
            "".join("<tr>" + "".join("<td>" + c + "</td>" for c in row) + "</tr>" for row in rows)
            or '<tr><td colspan="' + str(len(headers)) + '">目前沒有紀錄。</td></tr>'
        )
        + "</tbody></table></div>"
    )


def links(row):
    tid, source = row["id"], row["spec"]["source"]
    return f'<a class="table-link" href="/schedules/executions/{tid}?source={source}">查看分類結果與報表</a>'


def list_page(db, settings, source, rule_filter=""):
    from gummy.operations import worker_health
    from gummy.python_scheduler import scheduler_health

    data = overview(db, settings)
    health = scheduler_health(settings.scheduler_config_path, db=db)
    worker = worker_health(db)
    q = db.quota_status(settings.total_limit, settings.daily_limit)
    provider = db.provider_quota_status(settings.rapidapi_key)
    body = '<section class="hero"><div><p class="eyebrow">設定一次，自動循環</p><h2>排程管理</h2><p>決定何時抓取、抓哪些分類；完成後自動產生 Excel。</p></div><a class="button" href="/schedules/new">新增排程</a></section>'
    body += f'<section class="card"><h2>運作狀態</h2><dl class="facts"><div><dt>Python 排程器</dt><dd>{"在線" if health.get("online") else "未連線，無法準時派發"}</dd></div><div><dt>背景執行器</dt><dd>{"在線" if worker["online"] else "未連線，任務暫留佇列"}</dd></div><div><dt>下次預定執行</dt><dd>{taipei_time(data["next_at"]) if data["next_at"] else "沒有未來安排"}</dd></div><div><dt>本機剩餘次數</dt><dd>今天 {q["daily_limit"] - q["daily_used"]} ／累計 {q["total_limit"] - q["total_used"]}</dd></div></dl><p class="hint">台灣時間；每日帳本於 08:00（UTC 00:00）重置。累計限額不會自動重置。排程啟用不代表程序正在運作，電腦仍須開機。</p>'
    body += (
        '<p class="hint">供應商最近觀測：'
        + (
            f"{provider['remaining_estimate']} 次，{taipei_time(provider['last_header_at'])}；不是即時帳號餘額。"
            if provider
            else "未知；請核對供應商控制台。"
        )
        + "</p></section>"
    )
    rows = []
    latest = {}
    for event in data["history"]:
        latest.setdefault(event["rule_id"], event)
    for row in data["rules"]:
        spec = Rule.model_validate(row["spec"])
        state = STATE[spec.state]
        if spec.state == "enabled" and spec.frequency == "once" and not row["next_at"]:
            state = "單次時間已過，請查看結果"
        actions = f'<a class="button secondary" href="/schedules/rules/{row["id"]}">查看／編輯</a>'
        if spec.state != "archived":
            actions += (
                f' <a class="table-link" href="/schedules/rules/{row["id"]}/run">立即執行一次</a>'
            )
        rows.append(
            [
                cell(spec.name)
                + '<span class="metadata">'
                + ("模擬資料" if spec.source == "demo" else "真實資料")
                + "</span>",
                cell(describe(spec)),
                cell("、".join(c.category for c in spec.categories)),
                cell(f"{len(spec.categories)} 分類／預估 {spec.requests} 次"),
                taipei_time(row["next_at"]) if row["next_at"] else "—",
                cell(state),
                cell(LABELS.get(latest[row["id"]]["status"], latest[row["id"]]["status"]))
                if row["id"] in latest
                else "尚無紀錄",
                actions,
            ]
        )
    body += (
        '<section class="card"><h2>排程清單</h2><p class="hint">此頁管理所有資料來源，每列是一個規則；暫停、恢復與封存在「查看／編輯」。多分類依設定順序採集，額度不足則整次不執行。</p>'
        + table(
            ["名稱", "執行規則", "採集分類", "每次用量", "下次時間", "狀態", "最近結果", "操作"],
            rows,
        )
        + "</section>"
    )
    options = [("", "全部排程")] + [(r["id"], r["spec"]["name"]) for r in data["rules"]]
    future = [r for r in data["upcoming"] if not rule_filter or r["rule_id"] == rule_filter]
    body += (
        '<section class="card"><h2>未來 30 天</h2><form method="get" action="/schedules" class="filters">'
        + select_field("rule_filter", "查看排程", rule_filter, options)
        + '<button class="secondary">篩選</button></form>'
    )
    body += f"<p>共 {len(future)} 次安排，預估 {sum(r['requests'] for r in future)} 次 API 請求。這是預測，不是已預留額度。</p>"
    body += (
        table(
            ["預定時間（台灣）", "排程", "分類數", "預估 API 次數"],
            [
                [
                    taipei_time(r["scheduled_at"]),
                    cell(r["name"]),
                    str(r["category_count"]),
                    str(r["requests"]),
                ]
                for r in future
            ],
        )
        + "</section>"
    )
    past = history(db, rule_filter or None)
    body += (
        '<section class="card" id="history"><h2>執行紀錄</h2><p class="hint">最近 100 次。漏跑、失敗與額度不足不會自動重抓。</p>'
        + table(
            ["預定時間", "排程／版本", "結果", "詳情"],
            [
                [
                    taipei_time(r["scheduled_at"]),
                    cell(r["spec"]["name"] + f" / #{r['revision']}"),
                    cell(LABELS.get(r["status"], r["status"])),
                    links(r),
                ]
                for r in past
            ],
        )
        + "</section>"
    )
    body += '<section class="card"><h2>轉換前的安排</h2><p>舊安排、設定版本與報表完整保留；未來日期已轉成單次規則。</p><a class="button secondary" href="/schedules/legacy">查看舊安排與變更歷程</a></section>'
    return shell("排程管理", "/schedules", source, body)


def edit_page(spec, rule_id, csrf, revision=0, errors=""):
    body = (
        '<section class="card"><h2>'
        + ("編輯排程" if rule_id else "新增排程")
        + "</h2><p>台灣時間。先選頻率，再設定分類；儲存不會立即採集。</p>"
    )
    body = (
        '<style>.recurring-form .weekly-fields{display:none}.recurring-form:has(select[name="frequency"] option[value="weekly"]:checked) .weekly-fields{display:block}.weekly-fields label{min-height:44px;display:flex;align-items:center;gap:8px}.weekly-fields input{width:auto}</style>'
        + body
    )
    if errors:
        body += '<p class="feedback" role="alert">' + cell(errors) + "</p>"
    body += f'<form class="recurring-form" method="post" action="/schedules/rules/preview"><input type="hidden" name="csrf" value="{escape(csrf, quote=True)}"><input type="hidden" name="rule_id" value="{escape(rule_id or "", quote=True)}">'
    body += '<fieldset><legend>1. 執行規則</legend><div class="filters">'
    body += input_field("name", "排程名稱", spec.name, extra='required maxlength="80"')
    body += select_field(
        "source",
        "資料來源",
        spec.source,
        [("rapidapi", "Amazon 真實資料"), ("demo", "模擬資料（不消耗額度）")],
    )
    body += select_field(
        "frequency",
        "執行頻率",
        spec.frequency,
        [("daily", "每天"), ("weekly", "每週指定星期"), ("once", "指定日期，僅一次")],
    )
    body += input_field("at", "執行時間", spec.at, "time", "required")
    body += input_field("start", "開始日期／單次日期", spec.start, "date", "required")
    body += input_field("end", "結束日期（選填，循環排程適用）", spec.end or "", "date")
    body += '</div><fieldset class="weekly-fields"><legend>每週執行星期（只有選「每週」時適用）</legend><div class="filter-actions">'
    for i, label in enumerate(WEEK):
        body += f'<label><input type="checkbox" name="weekday_{i}" value="1" {"checked" if i in spec.weekdays else ""}> 星期{label}</label>'
    body += (
        '</div></fieldset><div class="filters">'
        + select_field(
            "state",
            "排程狀態",
            spec.state,
            list(STATE.items()) if rule_id else [("enabled", "啟用"), ("paused", "暫存，先不執行")],
        )
        + '</div><p class="hint">每日不需勾星期；單次只使用開始日期。封存保留歷史且不可恢復。編輯會取消尚未開始的舊版本任務，已開始的工作繼續完成。</p></fieldset>'
    )
    body += "<fieldset><legend>2. 要抓取的分類與順序</legend><p>每次依序抓完這份清單。每分類最多 2 件；預估請求＝搜尋 1 次＋商品件數。空白列不新增；移除分類請清空該列的名稱、關鍵字及商品數。</p>"
    for i in range(min(20, len(spec.categories) + 2)):
        item = spec.categories[i] if i < len(spec.categories) else None
        body += (
            "<fieldset><legend>"
            + (f"分類 {i + 1}" if item else "新增分類（選填）")
            + '</legend><div class="filters">'
        )
        body += input_field(f"order_{i}", "執行順序", i + 1, "number", 'min="1" max="20"')
        body += input_field(
            f"category_{i}", "分類名稱", item.category if item else "", extra='maxlength="80"'
        )
        body += input_field(
            f"query_{i}", "搜尋關鍵字", item.query if item else "", extra='maxlength="120"'
        )
        body += input_field(
            f"max_products_{i}",
            "最多商品數",
            item.max_products if item else "",
            "number",
            'min="1" max="2"',
        )
        body += "</div></fieldset>"
    body += '</fieldset><p class="hint">報表於採集完成後自動產生，不另設時間。多分類額度不足時整次不執行，不自動增加上限。</p><div class="filter-actions"><button name="intent" value="preview">預覽時間與用量</button><button class="secondary" name="intent" value="add">增加分類欄位</button><a class="button secondary" href="/schedules">返回清單</a></div></form></section>'
    return shell("設定排程", "/schedules", spec.source, body)


def parse_rule(values):
    permitted = {
        "csrf",
        "rule_id",
        "name",
        "source",
        "frequency",
        "at",
        "start",
        "end",
        "state",
        "intent",
    }
    permitted |= {f"weekday_{i}" for i in range(7)}
    permitted |= {
        f"{k}_{i}" for k in ("order", "category", "query", "max_products") for i in range(20)
    }
    if set(values) - permitted:
        raise ValueError("表單含不支援欄位")
    categories = []
    for i in range(20):
        row = {key: values.get(f"{key}_{i}", "") for key in ("category", "query", "max_products")}
        if any(row.values()):
            order = int(values.get(f"order_{i}", i + 1))
            if not 1 <= order <= 20:
                raise ValueError("順序必須為 1–20")
            categories.append((order, i, row))
    fields = {k: values[k] for k in ("name", "source", "frequency", "at", "start", "state")}
    return Rule(
        **fields,
        end=values.get("end") or None,
        weekdays=[i for i in range(7) if values.get(f"weekday_{i}") == "1"],
        categories=[r[2] for r in sorted(categories)],
    )


def install(app, db, settings, protected, respond):
    forms = LocalForms("recurring")

    def ensure():
        if not active(db):
            raise HTTPException(409, "循環排程尚未切換")

    def render(request, callback):
        ensure()
        return forms.response(request, respond, callback)

    @app.get("/schedules/new", dependencies=protected)
    def new(request: Request):
        now = datetime.now(TZ)
        spec = Rule(
            name="新的商品追蹤",
            frequency="daily",
            start=now.date() + timedelta(days=1),
            categories=[{"category": "套筒扳手", "query": "socket wrench set", "max_products": 2}],
        )
        return render(
            request,
            lambda session: edit_page(
                spec, None, forms.seal(session, "edit", {"rule_id": None, "revision": 0})
            ),
        )

    @app.get("/schedules/rules/{rule_id}", dependencies=protected)
    def edit(request: Request, rule_id: str):
        try:
            row = get_rule(db, rule_id)
        except ValueError:
            raise HTTPException(404, "找不到排程") from None
        spec = Rule.model_validate(row["spec"])

        def page(session):
            content = edit_page(
                spec,
                rule_id,
                forms.seal(session, "edit", {"rule_id": rule_id, "revision": row["revision"]}),
                row["revision"],
            )
            with db.engine.connect() as conn:
                entries = list(
                    conn.execute(
                        select(versions)
                        .where(versions.c.rule_id == rule_id)
                        .order_by(versions.c.revision.desc())
                    ).mappings()
                )
            detail = (
                '<section class="card"><h2>設定修改歷程</h2>'
                + table(
                    ["版本", "儲存時間", "規則", "狀態"],
                    [
                        [
                            str(r["revision"]),
                            taipei_time(r["created_at"]),
                            cell(describe(Rule.model_validate(r["spec"]))),
                            cell(STATE[r["spec"]["state"]]),
                        ]
                        for r in entries
                    ],
                )
                + f'<a href="/schedules?rule_filter={rule_id}#history">查看執行紀錄</a></section>'
            )
            return content.replace("</main>", detail + "</main>")

        return render(request, page)

    @app.post("/schedules/rules/preview", dependencies=protected)
    async def preview(request: Request):
        ensure()
        values = await forms.fields(request)
        signed = forms.unseal(request, values.pop("csrf", ""), "edit")
        try:
            spec = parse_rule(values)
            rid = values.get("rule_id") or None
            if rid != signed["rule_id"] or (
                rid and get_rule(db, rid)["revision"] != signed["revision"]
            ):
                raise ValueError("排程已變更，請重新開啟編輯")
            if values.get("intent") == "add":
                return render(
                    request,
                    lambda session: edit_page(
                        spec, rid, forms.seal(session, "edit", signed), signed["revision"]
                    ),
                )
            now = datetime.now(UTC)
            upcoming = next_times(spec, now)
            if spec.state == "enabled" and not upcoming:
                raise ValueError("沒有未來執行時間，請調整日期")
            check = budget(db, settings, spec)
            fp = fingerprint(db, settings, spec, rid)
        except (ValueError, KeyError) as exc:
            return respond(
                shell(
                    "請調整排程",
                    "/schedules",
                    "rapidapi",
                    '<section class="card"><h2>設定尚未儲存</h2><p class="feedback">'
                    + cell(str(exc))
                    + "</p><p>請使用瀏覽器返回，保留已填內容並調整。</p></section>",
                ),
                422,
            )

        def page(session):
            token = forms.seal(
                session,
                "apply",
                {
                    "spec": spec.model_dump(mode="json"),
                    "rule_id": rid,
                    "fingerprint": fp,
                    "id": secrets.token_hex(16),
                },
            )
            body = (
                '<section class="card"><h2>確認排程</h2><p>'
                + cell(spec.name + " · " + describe(spec) + " · " + STATE[spec.state])
                + "</p>"
            )
            body += f"<p>每次 {len(spec.categories)} 分類，預估 {spec.requests} 次請求；未來 30 天預估 {len(forecast(spec, now)) * spec.requests} 次。報表隨採集完成自動產生。</p>"
            body += (
                "<ol>"
                + "".join(
                    "<li>"
                    + cell(c.category + "：" + c.query + f"（最多 {c.max_products} 件）")
                    + "</li>"
                    for c in spec.categories
                )
                + "</ol>"
            )
            body += (
                "<h3>接下來 5 次</h3><ul>"
                + "".join("<li>" + taipei_time(t.isoformat()) + "</li>" for t in upcoming)
                + "</ul>"
            )
            if check["reasons"]:
                body += (
                    '<div class="feedback" role="status"><strong>目前用量提醒</strong><ul>'
                    + "".join("<li>" + cell(r) + "</li>" for r in check["reasons"])
                    + "</ul><p>可保存規則，但實際到期若仍不足，整次不執行。</p></div>"
                )
            body += f'<p class="hint">儲存耗用 API 0 次；預览 5 分鐘有效。歷史資料保留，變更只影響未開始的工作。</p><form method="post" action="/schedules/rules/apply"><input type="hidden" name="token" value="{escape(token, quote=True)}"><button>確認儲存排程</button></form><p><a href="/schedules">返回清單</a></p></section>'
            return shell("確認排程", "/schedules", spec.source, body)

        return render(request, page)

    @app.post("/schedules/rules/apply", dependencies=protected)
    async def apply(request: Request):
        ensure()
        values = await forms.fields(request)
        signed = forms.unseal(request, values.get("token", ""), "apply")
        try:
            save_rule(
                db,
                settings,
                Rule.model_validate(signed["spec"]),
                signed["rule_id"],
                signed["fingerprint"],
                signed["id"],
            )
        except (ValueError, RunConflict) as exc:
            raise HTTPException(409, str(exc)) from None
        return RedirectResponse("/schedules", 303)

    @app.get("/schedules/rules/{rule_id}/run", dependencies=protected)
    def manual(request: Request, rule_id: str):
        try:
            row = get_rule(db, rule_id)
        except ValueError:
            raise HTTPException(404, "找不到排程") from None
        spec = Rule.model_validate(row["spec"])
        check = budget(db, settings, spec)

        def page(session):
            token = forms.seal(
                session,
                "run",
                {
                    "rule_id": rule_id,
                    "fingerprint": fingerprint(db, settings, spec, rule_id),
                    "id": secrets.token_hex(16),
                },
            )
            body = f'<section class="card"><h2>立即執行一次：{cell(spec.name)}</h2><p>依目前版本採集全部 {len(spec.categories)} 個分類，預估 {spec.requests} 次 API。這次手動工作不改變循環時間。</p>'
            body += "<ul>" + "".join("<li>" + cell(r) + "</li>" for r in check["reasons"]) + "</ul>"
            if check["allowed"] and spec.state != "archived":
                body += f'<form method="post" action="/schedules/run"><input type="hidden" name="token" value="{escape(token, quote=True)}"><button>確認立即採集</button></form>'
            body += '<p><a href="/schedules">返回排程</a></p></section>'
            return shell("立即執行一次", "/schedules", spec.source, body)

        return render(request, page)

    @app.post("/schedules/run", dependencies=protected)
    async def manual_apply(request: Request):
        ensure()
        values = await forms.fields(request)
        signed = forms.unseal(request, values.get("token", ""), "run")
        try:
            tid = run_now(db, settings, signed["rule_id"], signed["fingerprint"], signed["id"])
        except (ValueError, RunConflict) as exc:
            raise HTTPException(409, str(exc)) from None
        return RedirectResponse("/schedules/executions/" + tid, 303)

    @app.get("/schedules/executions/{tid}", dependencies=protected)
    def execution(request: Request, tid: str, source: SourceName = "rapidapi"):
        try:
            row = occurrence(db, tid)
        except ValueError:
            raise HTTPException(404, "找不到執行紀錄") from None

        def page(session):
            from pathlib import Path

            from gummy.db import operation_reports, tasks
            from gummy.monitor import report_content_verified

            spec = Rule.model_validate(row["spec"])
            results = {r["index"]: r for r in row["results"]}
            rows = []
            for i, c in enumerate(spec.categories):
                item = results.get(i)
                batch = item["run_id"] if item else None
                rows.append(
                    [
                        str(i + 1),
                        cell(c.category),
                        cell(c.query),
                        "成功" if item else "尚未成功／未執行",
                        f'<a href="/snapshot?source={spec.source}&run_id={batch}">批次 #{batch}</a>'
                        if batch
                        else "—",
                    ]
                )
            body = (
                f'<section class="card"><h2>{cell(spec.name)} · 執行結果</h2><p>{taipei_time(row["scheduled_at"])} · 規則版本 #{row["revision"]} · {cell(LABELS.get(row["status"], row["status"]))}</p><p>每個分類保留獨立批次，失敗不以舊資料補入。</p><p><a class="button secondary" href="/schedules/executions/{tid}">更新結果</a></p>'
                + table(["順序", "分類", "關鍵字", "結果", "資料"], rows)
            )
            with db.engine.connect() as conn:
                report_rows = list(
                    conn.execute(
                        select(operation_reports, tasks.c.request).join(
                            tasks, tasks.c.id == operation_reports.c.task_id
                        )
                    ).mappings()
                )
            for rep in report_rows:
                if rep["task_id"] == tid or rep["request"].get("occurrence_id") == tid:
                    if report_content_verified(
                        Path(rep["path"]), spec.source, rep["through_run_id"]
                    ):
                        body += f'<p><a class="button" href="/v1/operations/reports/{rep["task_id"]}.xlsx?source={spec.source}">下載已驗證 Excel</a> {taipei_time(rep["created_at"])}</p>'
            if results and row["status"] not in {"QUEUED", "RUNNING"}:
                token = forms.seal(session, "rebuild", {"tid": tid, "id": secrets.token_hex(16)})
                body += f'<form method="post" action="/schedules/rebuild"><input type="hidden" name="token" value="{escape(token, quote=True)}"><button class="secondary">從已存資料重產報表（API 0 次）</button></form>'
            body += '<p><a href="/schedules">返回排程清單</a></p></section>'
            return shell("排程結果", "/schedules", spec.source, body)

        return render(request, page)

    @app.post("/schedules/rebuild", dependencies=protected)
    async def rebuild(request: Request):
        ensure()
        values = await forms.fields(request)
        signed = forms.unseal(request, values.get("token", ""), "rebuild")
        from gummy.recurrence_report import enqueue_rebuild

        try:
            enqueue_rebuild(db, settings, signed["tid"], signed["id"])
        except (ValueError, RunConflict) as exc:
            raise HTTPException(409, str(exc)) from None
        return RedirectResponse("/schedules/executions/" + signed["tid"], 303)
