"""Human-readable schedule editor, reviewed before atomic application."""

import secrets
from datetime import timedelta
from html import escape

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from gummy.errors import RunConflict
from gummy.local_forms import LocalForms
from gummy.models import SourceName
from gummy.schedule_management import (
    ScheduleEdit,
    apply_schedule,
    effective_schedule,
    preview_schedule,
)
from gummy.scheduled import local_now
from gummy.ui import cell, taipei_time
from gummy.workspace_ui import input_field, select_field, shell, url


def edit_fields(values):
    allowed = {"csrf", "enabled", "collect_at", "report_at"} | {
        f"{key}_{i}" for i in range(30) for key in ("date", "category", "query", "max_products")
    }
    if set(values) - allowed:
        raise ValueError("表單含不支援的設定。")
    batches = []
    for i in range(30):
        row = {k: values.get(f"{k}_{i}", "") for k in ("date", "category", "query", "max_products")}
        if not any(row.values()):
            continue
        batches.append(row)
    return ScheduleEdit(
        enabled=values["enabled"],
        collect_at=values["collect_at"],
        report_at=values["report_at"],
        batches=batches,
    )


def schedule_page(db, settings, csrf, source, message=""):
    cfg, plan, meta = effective_schedule(db, settings.scheduler_config_path, upcoming=True)
    source = plan.source
    today = local_now().date()
    fixed = [b for b in plan.batches if b.date <= today]
    future = [b for b in plan.batches if b.date > today]
    fixed_html = "".join(
        f"<tr><td>{cell(b.date)}</td><td>{cell(b.category)}</td><td>{cell(b.query)}</td><td>{b.max_products}</td></tr>"
        for b in fixed
    )
    rows = ""
    for i in range(min(30 - len(fixed), len(future) + 3)):
        b = future[i] if i < len(future) else None
        rows += f'<fieldset><legend>{"安排 " + str(i + 1) if b else "新增安排（留空即不新增）"}</legend><div class="filters">'
        rows += input_field(
            f"date_{i}", "日期", b.date if b else "", "date", f'min="{today + timedelta(days=1)}"'
        )
        rows += input_field(
            f"category_{i}", "搜尋分類", b.category if b else "", extra='maxlength="80"'
        )
        rows += input_field(
            f"query_{i}", "搜尋關鍵字", b.query if b else "", extra='maxlength="120"'
        )
        rows += input_field(
            f"max_products_{i}",
            "最多商品數（1–2）",
            b.max_products if b else "",
            "number",
            'min="1" max="2"',
        )
        rows += '</div><p class="hint">刪除安排：清空這一列的四個欄位。新安排從明天起，不補抓歷史。</p></fieldset>'
    history = "".join(
        f"<tr><td>#{r['id']}</td><td>{taipei_time(r['created_at'])}</td><td>{'啟用' if r['enabled'] else '暫停'}</td><td>{cell(r['config']['collect_at'])}／{cell(r['config']['report_at'])}</td><td>{cell(r['effective_from'])}</td></tr>"
        for r in reversed(meta["history"])
    )
    body = f'''<section class="card"><h2>每日採集設定</h2><p>Python 獨立排程 · {"啟用中" if meta["enabled"] else "已暫停"} · 版本 #{meta["latest_revision"]} · 台灣時間</p><p class="hint">變更時間與商品安排從明天生效；暫停立即生效。只保存設定，不會立即抓資料；報表與採集一起暫停。今天與過去的紀錄保留。</p>{f'<p class="note" role="status">{cell(message)}</p>' if message else ""}<form method="post" action="/schedules/preview"><input type="hidden" name="csrf" value="{escape(csrf, quote=True)}"><div class="filters">{select_field("enabled", "排程狀態", str(meta["enabled"]).lower(), [("true", "啟用"), ("false", "暫停")])}{input_field("collect_at", "每日採集時間", cfg.collect_at, "time", 'required min="09:00"')}{input_field("report_at", "每日報表時間", cfg.report_at, "time", "required")}</div><details><summary>今天與過去安排（保留）</summary><div class="scroll"><table><thead><tr><th>日期</th><th>分類</th><th>關鍵字</th><th>件數</th></tr></thead><tbody>{fixed_html}</tbody></table></div></details><h3>明天起的安排</h3>{rows}<button type="submit">預覽變更與用量</button></form></section><section class="card"><h2>套用歷程</h2><p>每次確認保留一個版本，原計畫檔及已抓資料不覆寫。</p><div class="scroll"><table><thead><tr><th>版本</th><th>確認時間（台灣）</th><th>狀態</th><th>採集／報表</th><th>新時間起日</th></tr></thead><tbody>{history or '<tr><td colspan="5">尚未透過網站調整，使用原設定。</td></tr>'}</tbody></table></div></section>'''
    return shell("排程管理", "/schedules", source, body)


def schedule_preview(edit, check, token, source):
    reasons = "".join(f"<li>{cell(r)}</li>" for r in check["reasons"])
    rows = "".join(
        f"<tr><td>{cell(b['date'])}</td><td>{cell(b['category'])}</td><td>{cell(b['query'])}</td><td>{b['max_products']}</td></tr>"
        for b in check["after"]["batches"]
    )
    q = check["quota"]
    skipped = check["after"].get("skipped_dates", [])
    skip_note = (
        '<p class="note">以下日期因先前確認暫停而跳過，不補抓：'
        + cell("、".join(skipped))
        + "。</p>"
        if skipped
        else ""
    )
    button = (
        f'<form method="post" action="/schedules/apply"><input type="hidden" name="token" value="{escape(token, quote=True)}"><button>確認套用排程設定</button></form>'
        if check["allowed"]
        else "<button disabled>目前無法套用</button>"
    )
    body = f'''<section class="card"><h2>套用前確認</h2><p class="hint">本次操作 API 用量 0 次；以下為未來採集預估。時間與內容 {cell(check["effective_from"])} 起生效；暫停則立即生效。</p>{f'<div class="feedback" role="alert"><ul>{reasons}</ul></div>' if reasons else '<p class="note">檢查通過，確認時再核對工作與用量。</p>'}<dl class="facts"><div><dt>排程狀態</dt><dd>{"啟用" if check["enabled_before"] else "暫停"} → {"啟用" if edit.enabled else "暫停"}</dd></div><div><dt>採集／報表</dt><dd>{cell("／".join(check["before_times"]))} → {cell(edit.collect_at)}／{cell(edit.report_at)}（台灣）</dd></div><div><dt>計畫剩餘請求</dt><dd>{check["old_requests"]} → {check["new_requests"]} 次（暫停仍保留計畫額度）</dd></div><div><dt>排隊預留</dt><dd>{check["queued_reserved"]} 次</dd></div><div><dt>本機剩餘</dt><dd>{q["total_limit"] - q["total_used"]} 次累計；今天 {q["daily_limit"] - q["daily_used"]} 次</dd></div></dl><h3>套用後完整安排</h3><div class="scroll"><table><thead><tr><th>日期</th><th>分類</th><th>關鍵字</th><th>件數</th></tr></thead><tbody>{rows}</tbody></table></div><div class="filter-actions">{button}<a class="button secondary" href="{url("/schedules", source=source)}">返回調整</a></div><p class="hint">預覽 5 分鐘有效；重複確認不會建立第二份版本。失敗與中斷工作不因修改排程而重送。</p></section>'''
    return shell("排程變更預覽", "/schedules", source, skip_note + body)


def install_schedule_routes(app, db, settings, protected, respond):
    forms = LocalForms("schedule")
    from gummy.recurrence_ui import install

    install(app, db, settings, protected, respond)

    @app.get("/schedules/legacy", dependencies=protected)
    def legacy(source: SourceName = "rapidapi"):
        from gummy.monitor import active_plan_overview
        from gummy.python_scheduler import load_scheduler
        from gummy.recurrence_ui import table
        from gummy.schedule_management import revisions

        data = active_plan_overview(settings, db)
        rows = [
            [
                cell(b["date"]),
                cell(b.get("category", "")),
                cell(b["status"]),
                cell(b.get("run_id", "—")),
            ]
            for b in data.get("legacy_batches", data.get("batches", []))
        ]
        changes = revisions(db, load_scheduler(settings.scheduler_config_path)[2])
        body = '<section class="card"><h2>轉換前安排（唯讀）</h2>' + table(
            ["日期", "分類", "當時批次狀態", "批次"], rows
        )
        body += "<h3>原設定版本</h3>" + table(
            ["版本", "儲存時間", "採集時間", "報表時間"],
            [
                [
                    str(r["id"]),
                    taipei_time(r["created_at"]),
                    cell(r["config"]["collect_at"]),
                    cell(r["config"]["report_at"]),
                ]
                for r in changes
            ],
        )
        body += '<p>轉換後未來安排請至新清單查閱；原計畫的待執行狀態不再代表會由舊引擎派發。</p><a href="/schedules">返回排程管理</a></section>'
        return respond(shell("舊安排", "/schedules", source, body))

    @app.get("/schedules", dependencies=protected)
    def editor(
        request: Request, source: SourceName = "rapidapi", applied: int = 0, rule_filter: str = ""
    ):
        from gummy.recurrence import active

        if active(db):
            from gummy.recurrence_ui import list_page

            return forms.response(
                request, respond, lambda _: list_page(db, settings, source, rule_filter)
            )
        if not settings.scheduler_config_path:
            raise HTTPException(409, "尚未設定 Python 排程器。")
        try:
            return forms.response(
                request,
                respond,
                lambda s: schedule_page(
                    db,
                    settings,
                    forms.seal(s, "preview", {}),
                    source,
                    "已保存版本。排程器下一次檢查會讀取；請在首頁核對回報。" if applied else "",
                ),
            )
        except (ValueError, OSError):
            raise HTTPException(409, "排程設定無法讀取，請先檢查本機設定。") from None

    @app.post("/schedules/preview", dependencies=protected)
    async def preview(request: Request):
        from gummy.recurrence import active

        if active(db):
            raise HTTPException(409, "已切換循環規則，舊設定僅供查閱")
        values = await forms.fields(request)
        forms.unseal(request, values.pop("csrf", ""), "preview")
        try:
            edit = edit_fields(values)
            check = preview_schedule(db, settings, edit)
        except (ValueError, KeyError, ValidationError):
            return respond(
                shell(
                    "請調整排程",
                    "/schedules",
                    "rapidapi",
                    '<section class="card"><p class="feedback" role="alert">只能填明天起的日期、每批 1–2 件，日期須遞增且不重複；時間須有效且報表晚於採集。請用返回保留欄位並調整。</p><a class="button" href="/schedules">返回排程管理</a></section>',
                ),
                422,
            )
        return forms.response(
            request,
            respond,
            lambda s: schedule_preview(
                edit,
                check,
                forms.seal(
                    s,
                    "apply",
                    {
                        "edit": edit.model_dump(mode="json"),
                        "fingerprint": check["fingerprint"],
                        "id": secrets.token_hex(16),
                    },
                ),
                check["after"]["source"],
            ),
        )

    @app.post("/schedules/apply", dependencies=protected)
    async def apply(request: Request):
        from gummy.recurrence import active

        if active(db):
            raise HTTPException(409, "已切換循環規則，舊設定僅供查閱")
        values = await forms.fields(request)
        if set(values) != {"token"}:
            raise HTTPException(422, "確認格式不符")
        signed = forms.unseal(request, values["token"], "apply")
        try:
            revision = apply_schedule(
                db,
                settings,
                ScheduleEdit.model_validate(signed["edit"]),
                signed["fingerprint"],
                signed["id"],
            )
        except (ValueError, RunConflict):
            raise HTTPException(409, "設定或工作狀態已改變，請重新預覽。") from None
        return RedirectResponse("/schedules?applied=" + str(revision), 303)
