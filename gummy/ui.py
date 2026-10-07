from datetime import datetime
from html import escape
from pathlib import Path
from shlex import quote as shell_quote
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from gummy.classification import title_measurement_candidates
from gummy.models import SPEC_KEYS, normalize_text, valid_asin


def cell(value) -> str:
    return escape(str(value)) if value is not None and value != "" else "—"


def taipei_time(value) -> str:
    if not value:
        return "—"
    try:
        return (
            datetime.fromisoformat(str(value))
            .astimezone(ZoneInfo("Asia/Taipei"))
            .strftime("%Y/%m/%d %H:%M")
        )
    except ValueError:
        return cell(value)


def source_label(source: str) -> str:
    return "真實資料" if source == "rapidapi" else "模擬資料"


def quality_warning_text(warnings: list[str]) -> str:
    labels = {
        "missing_price": "來源未提供價格",
        "unparsed_price": "價格格式無法解析",
        "missing_specs": "缺少結構化規格",
    }
    return "；".join(labels.get(warning, warning) for warning in warnings)


def run_link(item: dict) -> str:
    if item["status"] != "SUCCEEDED":
        return "—"
    query = urlencode({"source": item["source"], "run_id": item["id"]})
    return f'<a href="/?{escape(query, quote=True)}">檢視</a>'


def audit_anchor(asin: str) -> str:
    return f"audit-{quote(str(asin), safe='')}"


def current_amazon_link(source: str, asin: str) -> str:
    if source != "rapidapi" or not valid_asin(asin):
        return ""
    return (
        f'<a href="https://www.amazon.com/dp/{asin}" target="_blank" '
        'rel="noopener noreferrer">查看目前 Amazon 商品頁</a>'
    )


def portfolio_audit_link(source: str, item: dict) -> str:
    query = escape(urlencode({"source": source, "run_id": item["run_id"]}), quote=True)
    anchor = escape(audit_anchor(item["asin"]), quote=True)
    return f'<a href="/?{query}#{anchor}">查看此商品對帳</a>'


def audit_details(
    audit_rows: list[dict], issues: list[dict], source: str, evidence: dict[str, dict]
) -> str:
    fields = (
        ("商品標題", "source_title", "stored_title", {"title"}),
        ("價格原文", "source_price", "stored_price_raw", {"price_raw", "price"}),
        ("品牌", "source_brand", "stored_brand", {"brand"}),
        ("材質", "source_material", "stored_material", {"material"}),
        (
            "公制／英制",
            "source_measurement_system",
            "stored_measurement_system",
            {"measurement_system"},
        ),
        ("尺寸", "source_size", "stored_size", {"size"}),
        ("顏色", "source_color", "stored_color", {"color"}),
        ("評分", "source_rating", "stored_rating", {"rating"}),
        ("評價數", "source_rating_count", "stored_rating_count", {"rating_count"}),
    )
    sections = []
    for row in audit_rows:
        asin = row["asin"]
        anchor = escape(audit_anchor(asin), quote=True)
        amazon_link = current_amazon_link(source, asin)
        current_page = f"<p>{amazon_link}</p>" if amazon_link else ""
        if row["evidence_status"] != "captured_selected_fields":
            sections.append(
                f'<details class="audit-item" id="{anchor}"><summary>{cell(asin)}｜缺少來源證據</summary>'
                "<p>本批次採集時未留存來源欄位，無法事後逐欄對帳。</p>"
                f"{current_page}</details>"
            )
            continue
        problems = {issue["field"] for issue in issues if issue["asin"] == asin}
        differences = sum(issue["asin"] == asin for issue in issues)
        result = f"{differences} 項待查" if differences else "程式對帳一致"
        comparisons = "".join(
            f'<tr class="{"mismatch" if problems & issue_fields else ""}">'
            f'<th scope="row">{label}</th>'
            f"<td>{cell(row[source_key])}</td>"
            f"<td>{cell(row[stored_key])}</td>"
            "</tr>"
            for label, source_key, stored_key, issue_fields in fields
        )
        source_fields = evidence.get(asin, {})
        hint_rows = "".join(
            f"<tr><td>{label}</td><th scope='row'>sales_volume（非精確購買提示）</th>"
            f"<td>{cell(source_fields[key])}</td></tr>"
            for key, label in (
                ("search_sales_volume", "搜尋結果"),
                ("detail_sales_volume", "商品明細"),
            )
            if source_fields.get(key)
        )
        additional_rows = hint_rows + "".join(
            f"<tr><td>{cell(section)}</td><th scope='row'>{cell(key)}</th><td>{cell(value)}</td></tr>"
            for section in ("product_information", "product_details")
            for key, value in source_fields.get(section, {}).items()
            if normalize_text(key) not in {*SPEC_KEYS, "brand"}
        )
        additional_html = (
            "<details><summary>查看供應商其他商品欄位（未分類）</summary>"
            "<p>這些欄位僅供評估後續規格映射；尚未納入分類或組合統計。</p>"
            '<div class="scroll" tabindex="0" role="region" aria-label="詳細資料，可左右捲動"><table><thead><tr><th>來源區塊</th><th>欄名</th><th>來源值</th></tr></thead>'
            f"<tbody>{additional_rows}</tbody></table></div></details>"
            if additional_rows
            else ""
        )
        sections.append(
            f'<details class="audit-item" id="{anchor}"><summary>{cell(asin)}｜{result}</summary>'
            "<p>顯示採集當時留存的選定欄位；大小寫與價格格式會正規化後核對。"
            f"入庫解析價格：{cell(row['stored_price_usd'])} USD。</p>"
            f"{current_page}"
            '<div class="scroll" tabindex="0" role="region" aria-label="來源與入庫欄位，可左右捲動"><table class="audit-table"><thead><tr><th>欄位</th><th>來源留存值</th><th>入庫值</th></tr></thead>'
            f"<tbody>{comparisons}</tbody></table></div>{additional_html}</details>"
        )
    return "".join(sections)


def coverage_panel(coverage: dict) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{cell(row['category'])}</td>"
        f"<td>{'已取樣' if row['run_count'] else '未取樣'}</td>"
        f"<td>{cell(row['distinct_product_count'])}</td>"
        f"<td>{cell(row['run_count'])}</td>"
        "</tr>"
        for row in coverage["categories"]
    )
    outside = ", ".join(cell(row["category"]) for row in coverage["outside_reference"])
    outside_note = (
        f"<p>參考表以外的已取樣分類：{outside}。這些批次不計入 22 類覆蓋數。</p>" if outside else ""
    )
    return (
        '<section class="card" id="coverage"><h2>參考分類覆蓋（真實資料）</h2>'
        f"<p>真實來源已取樣 {coverage['sampled_count']} / {coverage['reference_total']} 類。"
        "只計成功快照；每類取樣數不是市場覆蓋率。</p>"
        "<details><summary>查看 22 類明細</summary>"
        '<div class="scroll" tabindex="0" role="region" aria-label="詳細資料，可左右捲動"><table><thead><tr><th>原表分類</th><th>狀態</th><th>不重複 ASIN</th><th>成功批次</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div></details>{outside_note}</section>"
    )


def portfolio_panel(portfolio: dict | None) -> str:
    if portfolio is None:
        return ""
    rows = "".join(
        "<tr>"
        f"<td>{cell(item['run_id'])}</td>"
        f"<td>{cell(item['category'])}</td>"
        f"<td>{cell(item['asin'])}</td>"
        f"<td class='title'>{cell(item['title'])}</td>"
        f"<td>{cell(item['price'])}</td>"
        f"<td>{'已留存' if item['evidence_status'] == 'captured_selected_fields' else '缺來源證據'}</td>"
        f"<td>{cell({'PASS': '來源與入庫一致', 'INCOMPLETE': '來源證據不足', 'FAIL': '來源有差異'}.get(item['run_verification'], item['run_verification']))}</td>"
        f"<td>{portfolio_audit_link(portfolio['source'], item)}</td>"
        "</tr>"
        for item in portfolio["observations"]
    )
    return (
        '<section class="card"><h2>跨批次商品查驗</h2>'
        f"<p>資料來源：{cell(portfolio['source'])}；截至批次 #{cell(portfolio['through_run_id'])}；"
        f"{cell(portfolio['run_count'])} 個成功批次、{cell(portfolio['observation_count'])} 筆觀測、"
        f"{cell(portfolio['distinct_asin_count'])} 個不重複 ASIN。"
        "搜尋分類是任務標籤，逐件歸類仍待人工確認；同一 ASIN 可在不同批次重複出現。</p>"
        '<p class="muted">展開後可左右滑動表格；按「查看此商品對帳」會定位到該批次商品，再展開來源欄位。</p>'
        "<details><summary>展開跨批次商品與證據狀態</summary>"
        '<div class="scroll" tabindex="0" role="region" aria-label="詳細資料，可左右捲動"><table><thead><tr><th>批次</th><th>搜尋分類</th><th>ASIN</th>'
        "<th>商品標題</th><th>價格 USD</th><th>來源證據</th><th>批次對帳</th><th>詳細</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div></details></section>"
    )


def plan_panel(plan: dict, source: str) -> str:
    if plan["status"] == "NOT_CONFIGURED":
        return '<section class="card" id="reports"><h2>報表與人工核對</h2><p>尚無每日計畫報表。可先查看下方商品，或下載該批次的 CSV。</p></section><section class="card" id="schedule"><h2>採集排程</h2><p>尚未指定本機計畫。</p></section>'
    if plan["status"] == "PLAN_UNAVAILABLE":
        return '<section class="card" id="reports"><h2>報表與人工核對</h2><p>計畫檔無法讀取，暫時無法確認每日報表。</p></section><section class="card" id="schedule"><h2>採集排程</h2><p class="note">計畫檔無法讀取，請檢查設定。</p></section>'
    schedule_labels = {
        "LOADED_AND_MATCHED": "已載入，設定一致",
        "NOT_RUNNING": "Python 排程器未運作",
        "NOT_LOADED": "未載入",
        "CONFIG_MISMATCH": "設定不一致，需檢查",
        "UNKNOWN": "無法確認是否載入",
    }
    schedule_label = schedule_labels[plan["schedule_status"]]
    report_schedule_label = schedule_labels[plan["report_schedule_status"]]
    plan_label = {
        "READY": "本機條件檢查通過，實際執行請看各日狀態",
        "CONFLICTING_RUN_KEYS": "批次衝突，已停止",
        "MISSED_BATCH": "有日期漏跑，已停止",
        "INSUFFICIENT_LOCAL_QUOTA": "本機額度不足，已停止",
        "PROVIDER_QUOTA_LOW": "上次觀測的供應商額度不足，已停止",
        "FINISHED_OR_EXPIRED": "已完成或過期",
    }[plan["status"]]
    batch_labels = {
        "PENDING": "待執行",
        "SUCCEEDED": "完成並通過來源對帳",
        "MISSED": "漏跑，後續停止",
        "CONFLICT": "批次失敗或參數衝突",
        "VERIFICATION_FAILED": "來源對帳未通過",
    }

    def daily_report_label(batch: dict) -> str:
        report = batch["report"]
        if report is None:
            if batch["status"] == "SUCCEEDED":
                return "報表路徑未設定"
            return "待批次完成" if batch["status"] == "PENDING" else "批次未通過"
        if not report["exists"]:
            return "未產生"
        return "已產生、匯出驗證通過" if report["content_verified"] else "已產生、內容需核對"

    rows = "".join(
        "<tr>"
        f"<td>{cell(batch['date'])}</td>"
        f"<td>{cell(batch['category'])}</td>"
        f"<td>{cell(batch['query'])}</td>"
        f"<td>{cell(batch_labels[batch['status']])}</td>"
        f"<td>{cell(batch['run_id'])}</td>"
        f"<td>{cell(daily_report_label(batch))}</td>"
        "</tr>"
        for batch in plan["batches"]
    )
    latest = plan["latest_report"]
    if latest:
        latest_batch = next(
            (
                batch
                for batch in reversed(plan["batches"])
                if batch["report"] is not None and batch["report"]["path"] == latest["path"]
            ),
            None,
        )
        verify_command = (
            f"{shell_quote(str(Path(__file__).resolve().parents[1] / 'scripts/verify_portfolio_local.sh'))} "
            f"{shell_quote(plan['source'])} {latest_batch['run_id']} "
            f"{shell_quote(latest['path'])}"
            if latest_batch is not None
            else None
        )
        latest_status = (
            "匯出讀回驗證通過且檔案未變；人工核對仍待完成"
            if latest["content_verified"]
            else "已見檔案，內容仍須核對"
            if latest["exists"]
            else "尚未產生檔案"
        )
        latest_note = (
            f'<p class="muted">最新計畫報表：<code class="path">{cell(latest["path"])}</code>；'
            f"{latest_status}。</p>"
        )
        review_guide = (
            (
                "<details><summary>Excel／Numbers 存檔與進階驗證說明</summary><ol>"
                "<li>在本機開啟上方 XLSX，切換到「對帳索引」；先查看 E 欄批次對帳與 N 欄擷取時間。</li>"
                "<li>用 O 欄網址查看目前商品頁，先核對 ASIN、型號及商品類別，再對照「來源欄位對照」中採集時留存的值。商品頁會依收貨地顯示不同幣別（寄臺灣可能顯示 TWD），價格也會變動；不要直接拿目前 TWD 售價與快照 USD 價格判定不一致。</li>"
                "<li>在 K 欄填分類判斷、L 欄填來源核對結果、M 欄記下差異與查看時間，然後儲存上方路徑的 XLSX 原檔。</li>"
                "</ol><p>修改前先備份原 XLSX。使用 Numbers 時，儲存 .numbers 不會更新原 XLSX，排程也不會讀取該副本。請用「檔案 → 輸出至 → Excel」，選「每張工作表一頁」，把 XLSX 存回上方路徑；存檔後回到此頁確認人工欄位進度。隔離副本已驗證此轉存及下一份報表承接，但正式檔仍須逐筆人工核對。</p>"
                "<p>人工編輯後，原匯出的檔案雜湊會變更，畫面顯示「內容需核對」；這不表示人工結果正確或資料一定有錯。"
                "明天採集及產報表前都會自動重驗；若想現在檢查，請在終端機執行：</p>"
                f"<p><code class='path'>{cell(verify_command)}</code></p>"
                "<p>指令回報 PASS 只代表報表仍符合已存快照；商品分類與目前頁面仍須人工判斷。</p></details>"
            )
            if latest["exists"]
            else ""
        )
    else:
        latest_note = '<p class="muted">計畫尚無成功批次，因此沒有每日報表。</p>'
        review_guide = ""
    review = plan["latest_review_progress"]
    if review is None:
        review_note = ""
    elif review["status"] == "READ":
        review_title = (
            "第一天人工核對承接檔"
            if plan["review_progress_source"] == "initial_review_workbook"
            else "最新累計報表"
        )
        review_note = (
            f'<p class="muted">{review_title}的人工欄位填寫進度：'
            f"分類 {cell(review['classification_entered'])} / {cell(review['total'])}；"
            f"來源 {cell(review['source_entered'])} / {cell(review['total'])}；"
            f"兩項皆填 {cell(review['both_entered'])} / {cell(review['total'])}。"
            "這只統計填寫，不表示人工結論正確。</p>"
        )
    else:
        review_note = '<p class="note">人工欄位進度無法安全讀取，請檢查最新報表。</p>'
    provider_note = (
        f'<p class="muted">RapidAPI 上次回應的剩餘額度估計：'
        f"{cell(plan['provider_remaining_estimate'])} 次；最後收到額度標頭 "
        f"{cell(plan['provider_quota_observed_at'])}。其他程式可能已消耗額度，仍須核對帳號後台。</p>"
        if plan["provider_quota_checked"]
        else '<p class="muted">尚未取得這把金鑰的 RapidAPI 額度標頭；帳號剩餘額度須到後台確認。</p>'
    )
    report_actions = ""
    if latest and latest["exists"]:
        report_actions = (
            f'<a class="button secondary" href="/v1/exports/latest-workbook.xlsx?source={cell(plan["source"])}">下載 Excel 副本</a>'
            '<p class="muted">下載的是副本。若要讓明天的報表延續你的核對結果，編輯後請存回下方「工作檔位置」。</p>'
        )
    report_status = (
        "匯出檢查通過"
        if latest and latest["content_verified"]
        else "報表內容待核對"
        if latest and latest["exists"]
        else "尚無每日報表"
    )
    report_tone = "green" if latest and latest["content_verified"] else "amber"
    report_section = (
        '<section class="card" id="reports"><div class="section-heading"><div><p class="eyebrow">02 / 人工確認</p>'
        f'<h2>報表與人工核對</h2></div><span class="badge {report_tone}">{report_status}</span></div>'
        "<p>程式已比對的，是「採集時留存的值」與「資料庫」。商品是否分類正確，仍需要你看過再確認。</p>"
        '<ol class="review-steps"><li><b>開啟報表</b><span>使用最新 XLSX，切到「對帳索引」。</span></li>'
        "<li><b>逐件比對</b><span>由 O 欄打開 Amazon，核對 ASIN、型號與類別；採集時的值看「來源欄位對照」。</span></li>"
        "<li><b>留下結論</b><span>K 欄填分類、L 欄填來源核對、M 欄記原因與查看時間。</span></li></ol>"
        f"{review_note}{report_actions}<details><summary>工作檔位置與匯出檢查狀態</summary>{latest_note}</details>{review_guide}"
        '<p class="muted">商品頁價格會隨時間、收貨地與幣別變動，不能直接與採集時的 USD 售價判定為相同或不同。</p></section>'
        if plan["source"] == source
        else f'<section class="card" id="reports"><h2>報表與人工核對</h2><p>目前每日報表屬於{source_label(plan["source"])}。請切換資料來源後查看，或使用目前批次的 CSV。</p></section>'
    )
    next_batch = next((batch for batch in plan["batches"] if batch["status"] == "PENDING"), None)
    ready = (
        plan["status"] == "READY"
        and plan["schedule_status"] == "LOADED_AND_MATCHED"
        and plan["report_schedule_status"] == "LOADED_AND_MATCHED"
    )
    next_note = (
        f'<p class="schedule-next"><b>下一個預定項目</b><br>{cell(next_batch["date"])} · {cell(next_batch["category"])}</p>'
        if next_batch
        else "<p>本計畫沒有尚待執行的項目。</p>"
    )
    return report_section + (
        '<section class="card" id="schedule"><div class="section-heading"><div><p class="eyebrow">03 / 後續安排</p><h2>採集排程</h2></div>'
        f'<span class="badge {"green" if ready else "amber"}">{"已設定自動執行" if ready else "請查看排程狀態"}</span></div>'
        f"<p>排程資料來源：{source_label(plan['source'])}。採集時間 <b>{cell(plan['scheduled_time_local'])}</b>，報表時間 <b>{cell(plan['report_scheduled_time_local'])}</b>（本機時間）。</p>{next_note}"
        '<p class="muted">Mac 需要開機且登入。睡眠可能延後，關機可能漏跑；漏跑後會停止，不自動補抓。</p>'
        "<details><summary>排程狀態、額度與執行條件</summary>"
        f"<p>本機計畫：{cell(plan['plan_id'])}；{schedule_label}；"
        f"每天本機時間 {cell(plan['scheduled_time_local'])}。"
        f"計畫狀態：{plan_label}；尚待執行 {cell(plan['remaining_days'])} 天，"
        f"預估最多 {cell(plan['remaining_planned_requests'])} 次請求。</p>"
        f"<p>報表排程：{report_schedule_label}；每天本機時間 "
        f"{cell(plan['report_scheduled_time_local'])}。報表只從已驗證快照產生，不發外部請求。</p>"
        '<p class="muted">本機排程需要電腦開機、排程程序持續運作；睡眠可能延後執行，'
        "關機或未登入可能漏跑。漏跑後後續計畫會停止，不會自動補抓。</p>"
        f"{provider_note}"
        '<p class="muted">本機額度與排程狀態不代表 RapidAPI 帳號剩餘額度。'
        "分類是搜尋任務標籤，逐件商品仍須人工確認。</p></details>"
        '<details><summary>查看每天的採集與報表結果</summary><div class="scroll" tabindex="0" role="region" aria-label="每日排程，可左右捲動"><table><thead><tr><th>台灣日期</th><th>分類</th><th>搜尋詞</th>'
        f"<th>執行狀態</th><th>批次</th><th>每日報表</th></tr></thead><tbody>{rows}</tbody></table></div></details></section>"
    )


def product_cards(rows: list[dict], source: str, audit_rows: list[dict], issues: list[dict]) -> str:
    captured = {
        row["asin"] for row in audit_rows if row["evidence_status"] == "captured_selected_fields"
    }
    problematic = {item["asin"] for item in issues}
    cards = []
    for row in rows:
        asin = row["asin"]
        if asin not in captured:
            badge, tone = "缺少來源證據", "amber"
        elif asin in problematic:
            badge, tone = "來源資料有差異", "red"
        else:
            badge, tone = "已通過程式對帳", "green"
        price = (
            f"{cell(row['price'])} <small>USD</small>" if row["price"] is not None else "未提供價格"
        )
        candidates = ", ".join(title_measurement_candidates(row["title"]))
        specification = row["specs"].get("measurement_system")
        measurement = (
            cell(specification)
            if specification
            else f"{cell(candidates)}（標題推測，未驗證）"
            if candidates
            else "未提供"
        )
        warning = (
            f'<p class="note">{cell(quality_warning_text(row["warnings"]))}</p>'
            if row["warnings"]
            else ""
        )
        cards.append(f"""
        <article class="product-card">
          <div class="section-heading"><span class="brand">{cell(row["brand"])}</span><span class="badge {tone}">{badge}</span></div>
          <h3>{cell(row["title"])}</h3>
          <p class="asin">ASIN（商品識別碼） {cell(asin)}</p>
          <div class="price">{price}<span>採集時售價</span></div>
          <dl class="specs"><div><dt>材質</dt><dd>{cell(row["specs"].get("material"))}</dd></div><div><dt>公制／英制</dt><dd>{measurement}</dd></div></dl>
          {warning}<div class="product-actions"><a class="button secondary" href="#{escape(audit_anchor(asin), quote=True)}">比對此商品的來源</a>{current_amazon_link(source, asin)}</div>
        </article>""")
    return "".join(cards)


UI_STYLES = """
:root {color-scheme:light;font-family:system-ui,-apple-system,"PingFang TC","Microsoft JhengHei",sans-serif;color:#20332f;background:#f5f7f6;font-size:16px;line-height:1.65}
* {box-sizing:border-box} body {margin:0} a {color:#17634f;text-underline-offset:4px} a:hover {color:#104736} button,a,summary,select {touch-action:manipulation} :focus-visible {outline:3px solid #265dcc;outline-offset:4px;border-radius:4px}
h1,h2,h3,p {margin:0} h1 {font-size:clamp(26px,3vw,36px);line-height:1.3;letter-spacing:-.6px} h2 {font-size:22px;line-height:1.4} h3 {font-size:18px;line-height:1.6} p+p {margin-top:12px} small,.muted {color:#586861} small {font-size:14px} b,strong {font-weight:650}
.topbar {background:#fff;border-bottom:1px solid #dce4df} .topbar-inner {max-width:1248px;margin:auto;padding:18px 32px;display:flex;align-items:center;justify-content:space-between;gap:20px} .brandmark {display:flex;align-items:center;gap:12px;font-size:25px;font-weight:800;letter-spacing:-1px;text-decoration:none} .brand-symbol {display:grid;place-items:center;width:36px;height:36px;color:white;background:#17634f;border-radius:11px;font-size:25px} .brandmark small {border-left:1px solid #dce4df;padding-left:16px;margin-left:5px;font-size:14px;font-weight:500;letter-spacing:0} .local-label {font-size:14px;color:#586861}
.layout {display:grid;grid-template-columns:180px minmax(0,1fr);gap:36px;max-width:1248px;margin:auto;padding:32px} .sidebar {align-self:start;position:sticky;top:24px} .sidebar p {font-size:12px;letter-spacing:1.5px;color:#697970;margin:6px 14px 14px} .sidebar a {display:flex;align-items:center;gap:12px;min-height:48px;border-radius:8px;padding:10px 14px;text-decoration:none;color:#44574f;font-size:15px;font-weight:600;margin:3px 0} .sidebar a:hover {background:#e8eeea;color:#154d3c} .sidebar a:first-of-type {background:#e5efea;color:#154d3c}
body:has(:target) .sidebar a:first-of-type {background:transparent;color:#44574f}
body:has(#overview:target) .sidebar a[href="#overview"],body:has(#products:target,#audit:target,[id^="audit-"]:target) .sidebar a[href="#products"],body:has(#reports:target) .sidebar a[href="#reports"],body:has(#schedule:target) .sidebar a[href="#schedule"],body:has(#history:target,#coverage:target) .sidebar a[href="#history"] {background:#e5efea;color:#154d3c} .nav-number {font-size:12px;color:#6a7a72} .sidebar-note {border-top:1px solid #dce4df;margin-top:24px;padding-top:18px;font-size:13px;color:#586861} main {min-width:0}
.page-heading {display:flex;justify-content:space-between;align-items:end;gap:20px;flex-wrap:wrap;margin-bottom:24px} .eyebrow {font-size:12px;letter-spacing:1.5px;color:#60736a;font-weight:700;margin-bottom:6px} .page-heading .muted {margin-top:8px} .source-form {display:flex;gap:8px;align-items:center;flex-wrap:wrap} .source-form label {font-size:14px;width:100%;color:#586861} select,button,.button {font:inherit;min-height:44px;border-radius:8px;padding:10px 16px;display:inline-flex;justify-content:center;align-items:center;gap:8px;line-height:1.4} select {border:1px solid #bbc9c0;background:white;max-width:100%} button,.button {cursor:pointer;border:1px solid #17634f;color:white;background:#17634f;text-decoration:none;font-weight:600} button:hover,.button:hover {background:#104c3c;color:#fff} .secondary {background:#fff;color:#17634f;border-color:#c4d5ca} .secondary:hover {background:#eaf2ed;color:#154d3c} .source-form button {background:white;color:#17634f;border-color:#c4d5ca}
.hero {display:grid;grid-template-columns:1fr auto;gap:24px;align-items:center;background:#e8f0eb;border:1px solid #cbdcd0;border-radius:14px;padding:26px 28px;margin-bottom:20px} .hero h2 {font-size:24px;margin:6px 0 10px} .hero p {max-width:600px;color:#405e4e} .hero .button {white-space:nowrap} .badge {display:inline-flex;align-items:center;gap:6px;width:fit-content;max-width:100%;padding:4px 10px;border-radius:6px;font-size:12px;font-weight:650;line-height:1.5;white-space:normal} .green {background:#e3f1e8;color:#23583d} .amber {background:#fff1d5;color:#815414} .red {background:#fbe9e6;color:#933928} .neutral {background:#edf0ed;color:#586861}
.stats {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-bottom:26px} .stat {background:white;border:1px solid #dce4df;border-radius:12px;padding:20px} .stat>span {font-size:14px;color:#586861} .stat strong {display:block;font-size:34px;line-height:1.5;letter-spacing:-1px} .stat strong span {font-size:15px;font-weight:500;letter-spacing:0;margin-left:6px}.stat small {display:block;font-size:13px}
.workflow {display:flex;list-style:none;padding:18px 0 24px;margin:0;gap:16px;align-items:center;font-size:14px;border-bottom:1px solid #dce4df;margin-bottom:28px} .workflow li {display:flex;align-items:center;gap:8px;color:#586861} .workflow li+li:before {content:'→';color:#829088;margin-right:8px} .workflow b {background:#e4ece6;border-radius:50%;width:25px;height:25px;display:grid;place-items:center;color:#31543e;font-size:12px}.workflow .current {color:#815414;font-weight:650}.workflow .current b {background:#fff1d5}
.card {background:#fff;border:1px solid #dce4df;border-radius:12px;padding:24px;margin-bottom:20px;min-width:0} .card>p,.card>nav {margin-top:14px} .section-heading {display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:14px} .section-heading .eyebrow {margin-bottom:5px} .section-heading p {margin:0} .section-subtitle {color:#586861;font-size:14px} section[id],details[id] {scroll-margin-top:24px}.batch-links {display:flex;flex-wrap:wrap;gap:8px;margin:18px 0} .batch-links a {min-height:44px;display:inline-flex;align-items:center;border:1px solid #d8e2db;border-radius:8px;padding:7px 12px;text-decoration:none;font-size:14px;color:#415b4c} .batch-links a[aria-current] {background:#e9f2ec;border-color:#538667;color:#234f35;font-weight:650} .batch-links a:hover {background:#edf3ef} .batch-meta {display:flex;gap:12px;flex-wrap:wrap;color:#586861;font-size:14px;margin:12px 0 18px} .batch-meta span+span {border-left:1px solid #dce4df;padding-left:12px}
.product-grid {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin:20px 0} .product-card {border:1px solid #dce4df;border-radius:10px;padding:20px;display:flex;flex-direction:column;min-width:0} .product-card .section-heading {gap:8px} .brand {font-size:13px;font-weight:650;color:#566e60;letter-spacing:.2px} .product-card h3 {overflow-wrap:anywhere} .asin {color:#66746e;font-size:12px;margin-top:10px;overflow-wrap:anywhere}.price {font-size:30px;font-weight:650;line-height:1.4;margin:20px 0 12px;letter-spacing:-.5px}.price small {font-size:14px;font-weight:500}.price>span {display:block;font-size:12px;font-weight:400;color:#66746e;letter-spacing:0}.specs {font-size:14px;margin:0 0 18px}.specs>div {display:grid;grid-template-columns:90px minmax(0,1fr);gap:8px;margin:6px 0}.specs dt {color:#66746e}.specs dd {margin:0;overflow-wrap:anywhere}.product-actions {display:flex;flex-direction:column;align-items:start;gap:8px;margin-top:auto;font-size:14px}.product-actions>a:not(.button) {min-height:44px;display:inline-flex;align-items:center}.product-actions .button {width:100%}
.note {padding:12px 16px;border-left:3px solid #b7842f;background:#fff8e8;border-radius:4px;font-size:14px;color:#775321}.notice {margin:16px 0}.exports {display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}.exports a {min-height:44px;display:inline-flex;align-items:center;border:1px solid #d6e1da;border-radius:8px;padding:8px 14px;background:#f8faf8;text-decoration:none;font-size:14px}
details {border-top:1px solid #e1e8e3;margin-top:16px;padding-top:6px} summary {padding:14px 2px;min-height:48px;cursor:pointer;font-weight:600;line-height:1.5;color:#345b45} details[open]>summary {margin-bottom:10px} details>p {margin:10px 0;font-size:14px;color:#586861} details>ol {font-size:14px;color:#586861;padding-left:24px} details li {margin:12px 0} .path {overflow-wrap:anywhere;font-size:13px;background:#f2f5f3;padding:3px 5px} .scroll {max-width:100%;overflow-x:auto;border:1px solid #e0e7e2;border-radius:8px;margin:14px 0} table {width:100%;border-collapse:collapse;min-width:760px;font-size:14px} th,td {padding:12px 14px;border-bottom:1px solid #e5eae7;text-align:left;vertical-align:top} th {background:#f1f5f2;color:#48604f;font-weight:600;white-space:nowrap} td {overflow-wrap:anywhere;max-width:380px} td.title {min-width:250px} tr:hover td {background:#f8faf8} .audit-table {min-width:630px}.audit-table tr.mismatch th,.audit-table tr.mismatch td {background:#fff0ed}.audit-item:target {outline:2px solid #17634f;outline-offset:8px;border-radius:4px} .audit-item[open] {padding-bottom:12px}
.review-steps {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px;padding:0;list-style:none;counter-reset:steps;margin:24px 0}.review-steps li {counter-increment:steps;border-top:2px solid #d4e1d7;padding-top:14px}.review-steps b {display:block;font-size:16px;margin-bottom:8px}.review-steps b:before {content:counter(steps,decimal-leading-zero);color:#78907e;font-size:13px;margin-right:10px}.review-steps span {display:block;font-size:14px;color:#586861}.schedule-next {padding:14px 18px;background:#f3f7f4;border-radius:8px}.schedule-next b {font-size:13px;color:#597060}.footer {font-size:13px;color:#66746e;padding:16px 0 24px}.skip {position:absolute;top:-100px;left:16px;background:white;padding:12px;z-index:2}.skip:focus {top:8px}
@media(max-width:1050px) {.layout {grid-template-columns:150px minmax(0,1fr);gap:24px;padding:24px}.topbar-inner {padding:18px 24px}.hero {grid-template-columns:1fr}.hero .button {justify-self:start}.product-grid {grid-template-columns:1fr}.workflow {flex-wrap:wrap}}
@media(max-width:760px) {.topbar-inner {padding:14px 20px}.brandmark small {display:none}.layout {display:block;padding:20px}.sidebar {position:static;display:flex;flex-wrap:wrap;gap:3px;margin:-4px 0 22px;border-bottom:1px solid #dce4df;padding-bottom:12px}.sidebar p,.sidebar-note,.nav-number {display:none}.sidebar a {font-size:14px;padding:9px 12px;min-height:44px;margin:0}.page-heading {align-items:start}.source-form label {width:auto}.hero {padding:22px}.stats {gap:10px}.stat {padding:14px}.stat strong {font-size:27px}.stat small {font-size:12px}.card {padding:20px}.review-steps {grid-template-columns:1fr;gap:8px}.review-steps li {padding:12px 0}.workflow {gap:10px;font-size:13px}.workflow li+li:before {margin-right:0}.section-heading {align-items:start}}
@media(max-width:420px) {.layout {padding:16px}.stats {grid-template-columns:1fr}.stat {display:grid;grid-template-columns:1fr auto;align-items:center;padding:12px 16px}.stat strong {grid-column:2;grid-row:1 / span 2;font-size:28px;padding-left:8px}.stat small {grid-column:1}.stat>span {font-size:13px}.card {padding:18px}.hero {padding:20px}.hero h2 {font-size:22px}.product-card {padding:16px}.workflow {display:grid;grid-template-columns:1fr 1fr}.workflow li+li:before {display:none}.source-form {width:100%}.local-label {font-size:12px}}
@media(prefers-reduced-motion:no-preference) {a,button {transition:background-color .15s,color .15s}}
"""


def dashboard(
    source: str,
    run: dict | None,
    rows: list[dict],
    quota: dict,
    grouped: dict | None,
    evidence_count: int,
    verification: dict | None,
    recent_runs: list[dict],
    audit_rows: list[dict],
    coverage: dict,
    portfolio: dict | None,
    plan: dict,
    evidence: dict[str, dict],
) -> str:
    source_options = "".join(
        f'<option value="{name}" {"selected" if name == source else ""}>{label}</option>'
        for name, label in (("rapidapi", "真實資料"), ("demo", "模擬資料"))
    )
    observations = portfolio["observations"] if portfolio else []
    distinct = portfolio["distinct_asin_count"] if portfolio else 0
    captured = sum(item["evidence_status"] == "captured_selected_fields" for item in observations)
    successful = portfolio["run_count"] if portfolio else 0
    review = plan.get("latest_review_progress") if plan.get("source") == source else None
    review_value = (
        f"{review['both_entered']} / {review['total']}"
        if review and review["status"] == "READ"
        else "尚未確認"
    )
    report_exists = (
        bool(plan.get("latest_report", {}).get("exists")) if plan.get("latest_report") else False
    )
    report_available = report_exists and plan.get("source") == source
    if run:
        hero_title = "資料已抓到，接下來由你確認商品"
        hero_description = "先看商品與採集時售價，再用報表記下分類和來源的核對結果。"
        hero_badge = "目前階段：小量取樣與人工驗證"
        cta = (
            '<a class="button" href="#reports">開始核對報表 <span aria-hidden="true">→</span></a>'
            if report_available
            else '<a class="button" href="#products">查看已抓到的商品 <span aria-hidden="true">→</span></a>'
        )
    else:
        hero_title = "這個資料來源還沒有可顯示的商品"
        hero_description = "可先切換真實／模擬資料，或查看排程。首頁只讀取已儲存的資料。"
        hero_badge = "等待首批資料"
        cta = (
            '<a class="button" href="#schedule">查看採集安排 <span aria-hidden="true">→</span></a>'
        )
    notice = ""
    if source == "demo":
        notice = '<p class="note notice">目前是模擬資料，用來測試操作流程；不可當成真實 Amazon 商品。</p>'
    if captured < len(observations):
        notice += f'<p class="note notice">有 {len(observations) - captured} 筆觀測缺少當時的來源證據，無法完整對帳。可到「全部採集紀錄」查看是哪一筆。</p>'
    if plan["status"] not in {"NOT_CONFIGURED", "READY", "FINISHED_OR_EXPIRED"} or (
        plan.get("schedule_status") not in {None, "LOADED_AND_MATCHED"}
        or plan.get("report_schedule_status") not in {None, "LOADED_AND_MATCHED"}
    ):
        notice += '<p class="note notice">排程有需要確認的狀態，請查看<a href="#schedule">採集排程</a>。目前已有的商品仍可查閱。</p>'
    batch_links = "".join(
        f'<a href="/?{escape(urlencode({"source": source, "run_id": item["id"]}), quote=True)}#products" {'aria-current="true"' if run and item["id"] == run["id"] else ""}>'
        f"{cell(item['parameters'].get('category'))} · #{item['id']}</a>"
        for item in recent_runs
        if item["source"] == source and item["status"] == "SUCCEEDED"
    )
    if run is None:
        body = '<section class="card" id="products"><h2>尚無完成的批次</h2><p>請切換資料來源或查看採集排程；這裡不會自動發起新的採集。</p></section>'
    else:
        report_id = run["id"]
        query = escape(urlencode({"source": source, "run_id": report_id}), quote=True)
        candidate_query = escape(
            urlencode({"source": source, "run_id": report_id, "include_title_candidates": "true"}),
            quote=True,
        )
        verification_label = {
            "PASS": "來源與入庫一致",
            "INCOMPLETE": "來源證據不足",
            "FAIL": "來源與入庫不一致",
        }[verification["status"]]
        tone = (
            "green"
            if verification["status"] == "PASS"
            else "red"
            if verification["status"] == "FAIL"
            else "amber"
        )
        no_price = sum(
            row["price"] is None
            or "missing_price" in row["warnings"]
            or "unparsed_price" in row["warnings"]
            for row in rows
        )
        no_structured_measurement = sum(not row["specs"].get("measurement_system") for row in rows)
        title_only_measurement = sum(
            not row["specs"].get("measurement_system")
            and bool(title_measurement_candidates(row["title"]))
            for row in rows
        )
        group_rows = "".join(
            "<tr>"
            + "".join(
                f"<td>{cell(value)}</td>"
                for value in (
                    group["specifications"].get("material"),
                    group["specifications"].get("measurement_system"),
                    group["product_count"],
                    group["average_price"],
                    ", ".join(group["title_candidate_asins"]),
                    ", ".join(group["asins"]),
                )
            )
            + "</tr>"
            for group in grouped["groups"]
        )
        body = f"""
        <section class="card" id="products">
          <div class="section-heading"><div><p class="eyebrow">01 / 看資料</p><h2>已抓到的商品</h2></div><span class="badge {tone}">資料對帳：{verification_label}</span></div>
          <p class="section-subtitle">選一個採集批次，查看當時保存的商品；切換批次不會重新抓資料。</p>
          <nav class="batch-links" aria-label="選擇採集批次">{batch_links}</nav>
          <p>分類：{cell(run["parameters"]["category"])}</p>
          <div class="batch-meta"><span>批次 #{report_id} · {len(rows)} 件</span><span>{taipei_time(run["completed_at"])} 台灣時間</span><span>市場 {cell(run["parameters"].get("country", "US"))}</span></div>
          <p class="section-subtitle">這是搜尋結果取樣，分類尚待人工確認。以下為採集時售價，商品數不是銷量。</p>
          <div class="product-grid">{product_cards(rows, source, audit_rows, verification["issues"])}</div>
          <details><summary>本批資料可用性與下載 CSV</summary>
            <p>來源證據 {evidence_count} / {len(rows)}；資料對帳 {len(verification["issues"])} 項待查。關鍵字：{cell(run["parameters"]["query"])}。</p>
            <p>無可用價格 {no_price} / {len(rows)} 件；缺結構化公制／英制 {no_structured_measurement} / {len(rows)} 件，其中 {title_only_measurement} 件僅有未驗證的標題候選。</p>
            <p>缺值不補造，未提供的規格也可能不適用於該商品。標題候選不等於已確認規格。</p>
            <nav class="exports" aria-label="報表下載"><a href="/v1/exports/products.csv?{query}">商品 CSV</a><a href="/v1/exports/combinations.csv?{candidate_query}">規格組合 CSV</a><a href="/v1/exports/audit.csv?{query}">來源對帳 CSV</a></nav>
          </details>
          <details><summary>規格組合 · 材質 × 公制／英制</summary>
            <p>不重複商品 {grouped["matched_product_count"]} 件；各群件數不可直接相加。Metric 是公制，SAE 是英制；標題候選尚未驗證，同一 ASIN 可出現在兩組。</p>
            <div class="scroll" tabindex="0" role="region" aria-label="規格組合，可左右捲動"><table><thead><tr><th>材質</th><th>公制／英制</th><th>商品數</th><th>平均價 USD</th><th>標題候選 ASIN</th><th>ASIN</th></tr></thead><tbody>{group_rows}</tbody></table></div>
          </details>
        </section>
        <section class="card" id="audit"><div class="section-heading"><div><p class="eyebrow">01 / 追溯來源</p><h2>來源對帳</h2></div><span class="badge neutral">批次 #{report_id}</span></div>
          <p>點開商品識別碼，左右比對採集時的來源值與入庫值。「程式對帳一致」不等於人工確認分類正確。</p>
          {audit_details(audit_rows, verification["issues"], source, evidence)}
        </section>"""
    run_rows = "".join(
        "<tr>"
        f"<td>#{cell(item['id'])}</td><td>{source_label(item['source'])}</td><td>{cell(item['parameters'].get('category'))}</td>"
        f"<td>{cell({'SUCCEEDED': '採集完成', 'FAILED': '採集失敗', 'RUNNING': '執行中'}.get(item['status'], item['status']))}</td>"
        f"<td>{taipei_time(item['completed_at'] or item['started_at'])}</td><td>{cell(item['error_code'])}</td><td>{run_link(item)}</td></tr>"
        for item in recent_runs
    )
    return f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>gummy 商品資料工作台</title><style>{UI_STYLES}</style></head><body>
<a class="skip" href="#overview">跳至主要內容</a>
<header class="topbar"><div class="topbar-inner"><a class="brandmark" href="/?source={source}"><span class="brand-symbol" aria-hidden="true">g</span>gummy <small>Amazon 商品研究</small></a><span class="local-label">本機測試 · 唯讀工作台</span></div></header>
<div class="layout"><nav class="sidebar" aria-label="頁面導覽"><p>商品研究工作台</p><a href="#overview">成果總覽</a><a href="/products?source={source}">商品資料</a><a href="/periods?source={source}">每期報表</a><a href="/reviews?source={source}">來源查驗</a><a href="/settings">設定中心</a><div class="sidebar-note">先取樣，再驗證。<br>每個數字都能追溯至保存的批次。</div></nav>
<main id="overview"><div class="page-heading"><div><p class="eyebrow">AMAZON / 商品資料</p><h1>商品資料工作台</h1><p class="muted">看懂抓取成果，完成你的下一步。</p></div><form class="source-form" method="get" action="/"><label for="source">資料來源</label><select id="source" name="source">{source_options}</select><button type="submit">切換</button></form></div>
<section class="hero" aria-label="目前階段與下一步"><div><span class="badge green">{hero_badge}</span><h2>{hero_title}</h2><p>{hero_description}</p></div>{cta}</section>
<div class="stats" aria-label="目前資料來源的累計成果"><div class="stat"><span>已取得的不重複商品</span><strong>{distinct}<span>件</span></strong><small>{successful} 個成功批次 · {len(observations)} 筆觀測（含重複商品）</small></div><div class="stat"><span>已保存來源證據</span><strong>{captured}<span>/ {len(observations)} 筆</span></strong><small>可追溯採集時的選定欄位</small></div><div class="stat"><span>Excel 人工核對欄位已填</span><strong>{cell(review_value)}</strong><small>分類與來源兩項皆填；非正確率</small></div></div>
{notice}
<div class="exports"><a href="/products?source={source}">搜尋全部商品</a><a href="/periods?source={source}">依日期查詢與匯出</a><a href="/analysis?source={source}">比較分類、品牌與價格帶</a><a href="/settings">資料來源與金鑰設定</a></div>
<ol class="workflow" aria-label="資料處理流程"><li class="{"current" if not run else ""}"><b>1</b>自動採集</li><li><b>2</b>保存與程式對帳</li><li><b>3</b>整理報表</li><li class="{"current" if run else ""}"><b>4</b>人工確認</li></ol>
{body}
{plan_panel(plan, source)}
<section id="history" aria-label="全部採集紀錄">{portfolio_panel(portfolio)}{coverage_panel(coverage)}
<section class="card"><h2>最近批次</h2><p class="muted">採集完成只代表資料已儲存；人工核對進度請看報表區。時間已換算為台灣時間。</p><details><summary>查看最近 10 筆執行紀錄</summary><div class="scroll" tabindex="0" role="region" aria-label="最近批次，可左右捲動"><table><thead><tr><th>批次</th><th>來源</th><th>搜尋分類</th><th>狀態</th><th>台灣時間</th><th>錯誤類型</th><th>商品</th></tr></thead><tbody>{run_rows}</tbody></table></div></details></section>
<section class="card"><h2>本機用量與名詞說明</h2><details><summary>查看用量、ASIN、批次與來源證據的意思</summary><p>UTC 帳本日 {cell(quota["day"])}：{quota["daily_used"]} / {quota["daily_limit"]} 次；累計 {quota["total_used"]} / {quota["total_limit"]} 次。本機帳本不代表 RapidAPI 帳戶其他用量。</p><p>ASIN 是 Amazon 商品識別碼。批次是一次採集任務；同一商品在不同批次出現，會形成多筆觀測。來源證據是採集時另外留存的選定原始欄位。</p><p>空白或「—」代表未提供。商品數與評價數都不是銷量，目前沒有可用的精確月銷量。</p></details></section></section>
<footer class="footer">重新整理不會抓取資料，也不會使用 RapidAPI 額度。人工確認在 Excel 工作檔完成。</footer></main></div></body></html>"""
