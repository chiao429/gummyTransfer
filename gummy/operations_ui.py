"""Human-readable operations dashboard, bounded collection preview and task lineage."""

from datetime import UTC, datetime, timedelta
from html import escape

from gummy.ui import cell, taipei_time
from gummy.workspace_ui import input_field, select_field, shell, url

STATUSES = {
    "QUEUED": "等待執行",
    "RUNNING": "執行中",
    "SUCCEEDED": "已完成",
    "FAILED": "執行失敗",
    "CANCELLED": "已取消",
    "INTERRUPTED": "中斷，需確認",
    "DATA_READY": "資料已取得，報表待處理",
    "PENDING": "待執行",
    "SKIPPED_PAUSED": "因暫停跳過，不補抓",
    "MISSED": "未執行",
    "CONFLICT": "批次需處理",
    "VERIFICATION_FAILED": "來源核對未通過",
    "READY": "計畫可執行",
    "FINISHED_OR_EXPIRED": "計畫已結束",
    "NOT_CONFIGURED": "未設定計畫",
    "LOADED_AND_MATCHED": "已載入，設定一致",
    "NOT_RUNNING": "排程器未運作",
    "NOT_LOADED": "尚未載入",
    "CONFIG_MISMATCH": "設定不一致",
    "UNKNOWN": "無法確認",
    "PASS": "核對通過",
}
PHASES = {
    "queued": "排隊",
    "search": "搜尋商品",
    "details": "取得明細",
    "verify": "保存與來源核對",
    "collected": "資料已取得",
    "report": "產生及驗證報表",
    "done": "完成",
    "stopped": "已停止",
    "cancel_requested": "要求停止",
}
ERRORS = {
    "configuration_changed": "設定或計畫已改變，請重新預覽後建立任務。",
    "preflight_changed": "執行前額度或條件已變動，任務已停止。",
    "worker_restarted": "執行器曾中斷。請先檢查已取得資料與記帳次數，再決定是否新增任務。",
    "report_verification_failed": "資料保留，但報表產生或讀回核對未通過，可另建立報表重產任務。",
    "SourceError": "供應商請求或回應未通過檢查，請查看本批請求結果。",
    "QuotaExceeded": "執行時額度不足，後續請求已停止。",
    "RunConflict": "另一個採集正在執行，或批次狀態需確認。",
    "TimeoutExpired": "報表產生超時，已保留資料；請先檢查輸出再重產。",
    "TaskCancelled": "已依要求停止後續工作，已記帳的次數仍保留。",
}


def badge(status):
    tone = "green" if status in {"SUCCEEDED", "PASS", "LOADED_AND_MATCHED", "READY"} else "amber"
    return f'<span class="badge {tone}">{cell(STATUSES.get(status, status))}</span>'


def quota_panel(data):
    q, p = data["quota"], data["provider"]
    daily = max(0, q["daily_limit"] - q["daily_used"])
    total = max(0, q["total_limit"] - q["total_used"])
    held = sum(
        t["reserved_requests"] for t in data["tasks"] if t["status"] in {"QUEUED", "RUNNING"}
    )
    future = data["plan"].get("remaining_planned_requests", 0)
    reset = datetime.combine(datetime.now(UTC).date() + timedelta(days=1), datetime.min.time(), UTC)
    provider = (
        f"<strong>{p['remaining_estimate']}<span>次，最近觀測估計</span></strong>"
        f"<small>觀測：{taipei_time(p['last_header_at'])}（台灣）<br>帳號其他入口可能另有使用；供應商重置時間尚未留存。</small>"
        if p
        else "<strong>尚未取得</strong><small>請核對供應商控制台，不視為零或無限額度。</small>"
    )
    reservation_note = (
        f"原計畫未來預計 <strong>{future}</strong> 次。新增採集會保留原計畫所需次數。"
    )
    if data["plan"].get("recurrence"):
        total_forecast = sum(r["requests"] for r in data["plan"]["recurrence"]["upcoming"])
        reservation_note = f"未來 30 天預估 <strong>{total_forecast}</strong> 次；預測不預留額度，到期前檢查整次用量。"
    return f'''<section class="card" id="quota"><div class="toolbar"><h2>額度與未來安排</h2><a class="table-link" href="{url("/settings", source=data["source"])}">查看設定</a></div>
<div class="ops-metrics"><div class="stat"><span>今天平台剩餘</span><strong>{daily}<span>次</span></strong><small>已記帳 {q["daily_used"]} / {q["daily_limit"]} 次<br>下次重置：{taipei_time(reset.isoformat())}（台灣）</small></div>
<div class="stat"><span>本次試點剩餘</span><strong>{total}<span>次</span></strong><small>累計已記帳 {q["total_used"]} / {q["total_limit"]} 次<br>這是平台累計上限，不是月額度。</small></div>
<div class="stat"><span>RapidAPI 最近回報</span>{provider}</div></div>
<p>排隊任務預留 <strong>{held}</strong> 次；{reservation_note}</p>
<p class="hint">更新畫面、查商品及重產報表只讀已存資料。API 用量是請求次數，失敗或逾時仍保守記帳。</p></section>'''


def schedule_table(data):
    if data.get("plan", data).get("recurrence"):
        from gummy.recurrence_ui import LABELS, table

        recurring = data.get("plan", data)["recurrence"]
        rows = [
            [
                taipei_time(r["scheduled_at"]),
                cell(r["name"]),
                cell(str(r["category_count"]) + " 分類"),
                cell(str(r["requests"]) + " 次"),
            ]
            for r in recurring["upcoming"][:10]
        ]
        content = "<p>Python 循環排程；採集完成後自動產報。以下為最近 10 次未來安排。</p>"
        content += table(["預定時間", "名稱", "採集內容", "預估 API"], rows)
        alerts = [
            r
            for r in recurring["history"][:10]
            if r["status"]
            in {"BLOCKED", "MISSED", "FAILED", "PARTIAL", "DATA_READY", "INTERRUPTED"}
        ]
        content += "".join(
            '<p class="feedback">' + cell(r["spec"]["name"] + "：" + LABELS[r["status"]]) + "</p>"
            for r in alerts
        )
        return content
    plan = data["plan"]
    if plan["status"] == "NOT_CONFIGURED":
        return '<p class="empty">目前未指定採集計畫。可使用立即採集，建立小量任務。</p>'
    if not plan.get("batches"):
        return f'<p class="feedback">計畫暫不可讀：{cell(STATUSES.get(plan["status"], plan["status"]))}</p>'
    rows = ""
    for b in plan["batches"]:
        run = (
            f'<a class="table-link" href="{url("/snapshot", source=plan["source"], run_id=b["run_id"])}">批次 #{b["run_id"]}</a>'
            if b["run_id"] and b["status"] == "SUCCEEDED"
            else "—"
        )
        report = b.get("report")
        report_cell = (
            f'<a class="table-link" href="{url("/v1/exports/workbooks/" + str(b["run_id"]) + ".xlsx", source=plan["source"])}">下載 Excel</a>'
            if report and report["exists"]
            else "尚未產出"
        )
        if report and report["exists"]:
            report_cell += (
                '<span class="metadata">'
                + ("匯出核對通過" if report["content_verified"] else "內容待核對")
                + "</span>"
            )
        time_label = (
            "實際 " + taipei_time(b["started_at"]).split(" ")[-1]
            if b.get("started_at")
            else "預定 " + (plan.get("scheduled_time_local") or "時間未確認")
        )
        rows += f'<tr><td>{cell(b["date"])}<span class="metadata">{cell(time_label)}（台灣）</span></td><td>{cell(b["category"])}<span class="metadata">{cell(b["query"])}</span></td><td>{badge(b["status"])}{run}</td><td>{report_cell}</td></tr>'
    runtime = (
        "Python 獨立排程器（不依賴 Codex）"
        if plan.get("scheduler_backend") == "python"
        else "本機日曆排程"
    )
    heartbeat = (
        f'<p class="hint">Python 最後回報：{taipei_time(plan["scheduler"].get("last_seen_at"))}（台灣）</p>'
        if plan.get("scheduler_backend") == "python" and plan.get("scheduler")
        else ""
    )
    management = plan.get("scheduler") or {}
    if not management.get("enabled", True):
        heartbeat += '<p class="note">排程已暫停：採集與每日報表不執行，計畫額度仍保留。</p>'
    if management.get("latest_revision", 0) > management.get("time_revision", 0):
        heartbeat += f'<p class="note">新設定已保存，{cell(management.get("effective_from"))} 起生效；今天仍使用原時間。</p>'
    return f"""<p><strong>{runtime}</strong> · 採集：{badge(plan.get("schedule_status", "UNKNOWN"))} · 報表：{badge(plan.get("report_schedule_status", "UNKNOWN"))}</p>{heartbeat}
<p class="hint">採集 {cell(plan.get("scheduled_time_local") or "未確認")}；報表 {cell(plan.get("report_scheduled_time_local") or "未確認")}（台灣）。電腦需開機，Python 排程程序需持續運作；實際結果以下方批次為準。</p>
<div class="scroll" role="region" tabindex="0" aria-label="過去與未來排程，可左右捲動"><table><thead><tr><th>日期／時間</th><th>採集內容</th><th>實際結果</th><th>每日累計報表</th></tr></thead><tbody>{rows}</tbody></table></div>"""


def tasks_table(data):
    rows = ""
    for t in data["tasks"]:
        spec = t["request"]
        if t["kind"] in {"schedule", "schedule-report"}:
            from gummy.recurrence_ui import LABELS

            target = spec.get("occurrence_id", t["id"])
            rows += f'<tr><td><a href="/schedules/executions/{target}">{cell(spec["category"])}</a><span class="metadata">多分類排程／報表</span></td><td>{cell(LABELS.get(t["status"], t["status"]))}</td><td>{taipei_time(t["created_at"])}</td><td>{t["actual_requests"]} 次；預留 {t["reserved_requests"]} 次</td><td>{"已驗證" if t["report"] and t["report"]["verified"] else "尚未產出"}</td></tr>'
            continue
        report_scope = spec.get("report_scope")
        report_label = "固定期間報表" if report_scope else "截至批次 #" + str(t["run_id"])
        rows += f'''<tr><td><a class="table-link" href="{url("/tasks/" + t["id"], source=t["source"])}">{"立即採集" if t["kind"] == "collect" else "報表重產"}</a><span class="metadata">{cell(spec["category"]) if t["kind"] == "collect" else report_label}</span></td><td>{badge(t["status"])}<span class="metadata">{cell(PHASES.get(t["phase"], t["phase"]))}</span></td><td>{taipei_time(t["created_at"])}</td><td>{t["actual_requests"]} 次<span class="metadata">預留餘 {t["reserved_requests"]} 次</span></td><td>{"已驗證" if t["report"] and t["report"]["verified"] else "尚無已驗證報表"}</td></tr>'''
    return (
        f'<div class="scroll" role="region" tabindex="0" aria-label="背景任務，可左右捲動"><table><thead><tr><th>任務</th><th>結果與步驟</th><th>建立時間（台灣）</th><th>API 記帳</th><th>報表</th></tr></thead><tbody>{rows}</tbody></table></div>'
        if rows
        else '<p class="empty">還沒有網站建立的任務。歷史採集及原排程仍可在下方查看。</p>'
    )


def overview_page(data, *, task_list=False):
    src = data["source"]
    alerts = "".join(f"<li>{cell(x)}</li>" for x in data["alerts"])
    alerts = (
        f'<section class="card attention"><h2>需要處理</h2><ul>{alerts}</ul></section>'
        if alerts
        else '<p class="note">目前沒有新的待處理事項。以下狀態以最後檢查時間為準。</p>'
    )
    active = next((t for t in data["tasks"] if t["status"] in {"QUEUED", "RUNNING"}), None)
    activity = (
        f'<a class="table-link" href="{url("/tasks/" + active["id"], source=src)}">查看 {cell(PHASES.get(active["phase"], active["phase"]))}中的任務</a>'
        if active
        else "目前沒有此來源的網站任務執行中。"
    )
    successful = sum(r["status"] == "SUCCEEDED" for r in data["today_runs"])
    upcoming = next((b for b in data["plan"].get("batches", []) if b["status"] == "PENDING"), None)
    next_text = (
        f"{upcoming['date']} {data['plan'].get('scheduled_time_local') or ''}"
        if upcoming
        else "目前沒有待執行日期"
    )
    if data["plan"].get("recurrence"):
        next_at = data["plan"]["recurrence"]["next_at"]
        next_text = taipei_time(next_at) if next_at else "目前沒有未來安排"
    if not (data["plan"].get("scheduler") or {}).get("enabled", True):
        next_text = "已暫停（保留未來安排）"
    history = "".join(
        f'<tr><td>#{r["id"]}<span class="metadata">{taipei_time(r["started_at"])}</span></td><td>{cell(r["parameters"]["category"])}<span class="metadata">{cell(r["parameters"]["query"])}</span></td><td>{badge(r["status"])}</td><td>'
        + (
            f'<a class="table-link" href="{url("/snapshot", source=src, run_id=r["id"])}">查看商品與來源</a><a class="table-link" href="{url("/tasks/new", source=src, kind="report", run_id=r["id"])}">產生累計 Excel（0 次）</a>'
            if r["status"] == "SUCCEEDED"
            else f"{cell(r['error_code'] or '尚未完成')}"
        )
        + "</td></tr>"
        for r in data["runs"]
    )
    body = f'''<section class="hero"><div><p class="eyebrow">現在 · 過去 · 未來</p><h2>{"管理任務、追蹤排程與結果" if task_list else "採集營運中心"}</h2><p>查看健康狀態、可用額度、已取得資料與接下來的安排。</p><p class="hint">最後檢查：{taipei_time(data["checked_at"])}（台灣） · 畫面每 30 秒更新，表單頁不自動刷新。</p></div><a class="button" href="{url("/tasks/new", source=src)}">立即採集</a></section>
{alerts}<div class="ops-metrics"><div class="stat"><span>背景執行器</span><strong>{"已連線" if data["worker"]["online"] else "需確認"}</strong><small>最後回報：{taipei_time(data["worker"]["last_seen_at"]) if data["worker"]["last_seen_at"] else "尚無紀錄"}</small></div><div class="stat"><span>今天成功採集</span><strong>{successful}<span>批</span></strong><small>依台灣完成日期，模擬與真實來源分開。</small></div><div class="stat"><span>已取得商品</span><strong>{data["distinct_asins"]}<span>件</span></strong><small>{data["observations"]} 筆觀測，含跨期重複；來源留存 {data["evidence_count"]} 筆。</small></div><div class="stat"><span>下一次採集</span><strong class="next-date">{cell(next_text)}</strong><small>預定時間，實際結果仍須回報確認。</small></div></div>
{quota_panel(data)}<section class="card"><h2>正在執行與最近網站任務</h2><p>{activity}</p>{tasks_table(data)}</section>
<section class="card" id="schedule"><h2>過去與未來排程</h2><p><a class="button secondary" href="{url("/schedules", source=src)}">管理時間、商品安排與暫停</a></p>{schedule_table(data)}</section>
<section class="card" id="history"><h2>最近採集結果</h2><p class="hint">包含原 CLI、排程及網站採集。商品、來源與報表皆可從同一批次追溯。</p><div class="scroll" role="region" tabindex="0" aria-label="採集歷史，可左右捲動"><table><thead><tr><th>批次／台灣開始時間</th><th>採集內容</th><th>結果</th><th>資料與報表</th></tr></thead><tbody>{history}</tbody></table></div></section>'''
    return shell(
        "任務與排程" if task_list else "營運總覽",
        "/tasks" if task_list else "/",
        src,
        body,
        refresh_seconds=30,
    )


def new_task_page(source, csrf, spec=None, error=""):
    spec = spec or {
        "source": source,
        "kind": "collect",
        "query": "socket wrench set",
        "category": "套筒扳手",
        "max_products": 2,
        "run_id": None,
    }
    notice = f'<p class="feedback" role="alert">{cell(error)}</p>' if error else ""
    if spec["kind"] == "report":
        controls = f'<input type="hidden" name="source" value="{cell(source)}"><input type="hidden" name="run_id" value="{cell(spec["run_id"])}"><p>由已存資料產生截至批次 <strong>#{cell(spec["run_id"])}</strong> 的累計 Excel，API 用量 <strong>0 次</strong>。新檔另建版本，原報表保留。</p>'
    else:
        controls = select_field(
            "source",
            "資料來源",
            source,
            [("rapidapi", "真實 Amazon 資料"), ("demo", "模擬資料（0 次）")],
        )
        controls += input_field(
            "category", "搜尋分類", spec["category"], extra='required maxlength="80"'
        )
        controls += input_field(
            "query", "搜尋關鍵字", spec["query"], extra='required maxlength="120"'
        )
        controls += select_field(
            "max_products", "最多取得商品", spec["max_products"], [(1, "1 件"), (2, "2 件")]
        )
        controls = (
            '<div class="filters">'
            + controls
            + "</div><p>市場：美國 US。搜尋第一頁；兩件商品最多使用 3 次 API 請求。明細若提供評論或星級分布才會留存，缺值保持未提供。</p>"
        )
    body = f'''<p><a class="table-link" href="{url("/tasks", source=source)}">← 返回任務與排程</a></p><section class="card"><h2>{"產生報表" if spec["kind"] == "report" else "設定這次採集"}</h2>{notice}
<form method="post" action="/tasks/preview"><input type="hidden" name="csrf" value="{escape(csrf, quote=True)}"><input type="hidden" name="kind" value="{cell(spec["kind"])}">{controls}<div class="filter-actions"><button type="submit">預覽用量與執行條件</button></div></form><p class="hint">這一步只預覽，不會發出供應商請求。額度足夠並確認後才建立背景任務。</p></section>'''
    return shell("立即採集" if spec["kind"] == "collect" else "報表重產", "/tasks", source, body)


def preview_page(spec, check, token):
    reasons = "".join(f"<li>{cell(x)}</li>" for x in check["reasons"])
    gate = (
        f'<div class="feedback" role="alert"><strong>目前無法執行</strong><ul>{reasons}</ul></div>'
        if reasons
        else '<p class="note">預覽檢查通過。確認時及執行前會再檢查額度與設定。</p>'
    )
    q = check["local_quota"]
    details = [
        ("操作", "立即採集" if spec.kind == "collect" else "由現有資料產生累計報表"),
        ("來源", "真實 Amazon 資料" if spec.source == "rapidapi" else "模擬資料"),
        (
            "內容",
            spec.category + " / " + spec.query
            if spec.kind == "collect"
            else "截至批次 #" + str(spec.run_id),
        ),
        ("預計 API 請求", f"最多 {check['planned_requests']} 次"),
        (
            "目前本機可用",
            f"今日 {max(0, q['daily_limit'] - q['daily_used'])} 次；試點 {max(0, q['total_limit'] - q['total_used'])} 次",
        ),
        ("保留給原排程", f"{check['future_reserved']} 次"),
        ("其中當天排程須預留", f"{check['today_reserved']} 次"),
        ("排隊任務已預留", f"{check['queued_reserved']} 次"),
        ("每日限制下次重置", taipei_time(check["daily_reset_at"]) + "（台灣）"),
    ]
    facts = "".join(
        f"<div><dt>{label}</dt><dd>{cell(value)}</dd></div>" for label, value in details
    )
    action = (
        f'<form method="post" action="/tasks/confirm"><input type="hidden" name="token" value="{escape(token, quote=True)}"><button type="submit">確認建立背景任務（最多 {check["planned_requests"]} 次）</button></form>'
        if check["allowed"]
        else "<button disabled>目前無法建立任務</button>"
    )
    body = f'''<section class="card"><h2>確認內容與額度</h2>{gate}<dl class="facts">{facts}</dl><p>完成後可查看來源資料與下載已驗證 Excel。關閉網頁後，已建立的任務仍由背景執行器處理。</p>{action}<div class="filter-actions"><a class="button secondary" href="{url("/tasks/new", source=spec.source, kind=spec.kind, run_id=spec.run_id, search_query=spec.query, category=spec.category, max_products=spec.max_products)}">返回調整</a><a class="table-link" href="{url("/tasks", source=spec.source)}">查看任務與排程</a></div><p class="hint">本預覽 5 分鐘內有效。採集失敗或逾時仍可能消耗次數，未完成任務不會自動重抓。</p></section>'''
    return shell("執行預覽", "/tasks", spec.source, body)


def task_page(task, csrf, outcomes):
    src, tid = task["source"], task["id"]
    active = task["status"] in {"QUEUED", "RUNNING"}
    steps = (
        ["queued", "report", "done"]
        if task["kind"] == "report"
        else ["queued", "search", "details", "verify", "report", "done"]
    )
    visited = {e["phase"] for e in task["events"]}
    progress = "".join(
        f'<li class="{"current" if p == task["phase"] else "visited" if p in visited else ""}"><span>{i + 1}</span>{PHASES[p]}<small>{"目前步驟" if p == task["phase"] and active else "已回報" if p in visited else "尚未回報"}</small></li>'
        for i, p in enumerate(steps)
    )
    error = (
        f'<p class="feedback" role="alert">{cell(ERRORS.get(task["error_code"], "任務需要處理，請查看執行紀錄。"))}</p>'
        if task["error_code"]
        else ""
    )
    cancel = (
        f'<form method="post" action="/tasks/{tid}/cancel"><input type="hidden" name="csrf" value="{escape(csrf, quote=True)}"><button class="secondary" type="submit">{"取消排隊" if task["status"] == "QUEUED" else "停止後續工作"}</button></form><p class="hint">已送出的請求及正在產生的報表可能先完成；已記帳次數不退回。</p>'
        if active and not task["cancel_requested"]
        else "<p>已要求停止，執行器會在下一個安全步驟處理。</p>"
        if active
        else ""
    )
    run_links = ""
    if task["run_id"]:
        run_links = (
            f'<a class="button secondary" href="{url("/snapshot", source=src, run_id=task["run_id"])}">查看批次 #{task["run_id"]} 資料</a>'
            if task["status"] in {"SUCCEEDED", "DATA_READY"}
            else ""
        )
        if task["status"] == "DATA_READY":
            run_links += f'<a class="button" href="{url("/tasks/new", source=src, kind="report", run_id=task["run_id"])}">重產報表（0 次 API）</a>'
    report = task["report"]
    fixed_scope = task["request"].get("report_scope")
    scope_note = (
        "此版本只包含保存時固定的期間、篩選條件與成功批次，後續採集不加入。"
        if fixed_scope
        else "累計報表包含此來源截至本批次的成功觀測，並非全部都是這次新取得的商品。"
    )
    if fixed_scope:
        run_links += f'<a class="button secondary" href="{url("/reports/" + tid, source=src)}">查看固定範圍與重產版本</a>'
    download = (
        f'<a class="button" href="{url("/v1/operations/reports/" + tid + ".xlsx", source=src)}">下載已驗證 Excel</a>'
        if report and report["verified"]
        else "<p>尚無可下載的已驗證報表。</p>"
    )
    events = "".join(
        f"<tr><td>{taipei_time(e['occurred_at'])}</td><td>{cell(PHASES.get(e['phase'], e['phase']))}</td><td>{cell(e['detail'])}</td></tr>"
        for e in task["events"]
    )
    requests = "".join(f"<li>{cell(x)}</li>" for x in outcomes) or "<li>尚無供應商請求紀錄。</li>"
    body = f'''<p><a class="table-link" href="{url("/tasks", source=src)}">← 返回任務與排程</a></p><section class="card"><div class="toolbar"><h2>{"立即採集" if task["kind"] == "collect" else "報表重產"}</h2>{badge(task["status"])}</div>{error}<p>任務建立：{taipei_time(task["created_at"])}（台灣） · 來源：{cell(src)}</p><p>{cell(task["request"]["category"])} · {cell(task["request"]["query"])}</p><p><strong>已記帳 {task["actual_requests"]} 次</strong>；尚預留 {task["reserved_requests"]} 次。這是請求記帳，不是商品數。</p><ol class="task-steps">{progress}</ol>{cancel}<div class="filter-actions">{run_links}</div></section>
<section class="card"><h2>本任務產出的報表</h2><p>{scope_note}</p>{download}</section>
<section class="card"><h2>執行紀錄</h2><div class="scroll" role="region" tabindex="0" aria-label="任務執行紀錄，可左右捲動"><table><thead><tr><th>台灣時間</th><th>步驟</th><th>回報</th></tr></thead><tbody>{events}</tbody></table></div><details><summary>查看 API 請求結果</summary><ul>{requests}</ul></details><p class="hint">{"執行中每 5 秒更新，關閉此頁不會取消任務。" if active else "此任務已停止或完成，不會自動重試。"}</p></section>'''
    return shell("任務明細", "/tasks", src, body, refresh_seconds=5 if active else None)
