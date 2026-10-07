"""Guided local review screens; all provider/user text is escaped."""

from html import escape, unescape

from gummy.catalog import DEFAULT_COLUMNS
from gummy.quality import SPEC_LABELS
from gummy.reports import source_audit_rows
from gummy.reviews import JUDGMENTS, REVIEW_FIELDS
from gummy.ui import audit_details, cell, taipei_time
from gummy.workspace_ui import input_field, select_field, shell, url


def queue_page(data):
    query = data["query"]
    rows = data["rows"]
    captured = sum(row["evidence_status"] == "captured" for row in rows)
    sample_count = sum(row["review_sample_count"] for row in rows)
    controls = select_field(
        "source", "資料來源", query.source, [("rapidapi", "真實資料"), ("demo", "模擬資料")]
    )
    controls += input_field("q", "搜尋商品、ASIN 或品牌", query.q, extra='maxlength="120"')
    controls += select_field(
        "category",
        "搜尋分類",
        query.category,
        [("", "全部分類"), *[(x, x) for x in data["facets"]["category"]]],
    )
    controls += select_field(
        "review_status",
        "可選核對紀錄",
        query.review_status,
        [
            ("all", "全部紀錄"),
            ("pending", "待核對、未完成或需重核"),
            ("entered", "兩項判斷已填（含不符合）"),
            ("stale", "資料變更需重核"),
        ],
    )
    items = ""
    for row in data["page_rows"]:
        tone = "neutral" if row["review_state"] == "已填寫" else "amber"
        link = url(f"/reviews/{row['run_id']}/{row['asin']}", source=query.source)
        sample_note = (
            f"留存評論：{row['review_sample_count']} 則"
            if row["review_sample_count"]
            else "本批次無留存評論"
        )
        items += f'''<tr><td class="title"><span class="product-name">{cell(row["title"])}</span><span class="metadata">{cell(row["asin"])} · 批次 #{row["run_id"]} · {cell(row["category"])}</span></td><td><span class="badge {tone}">{cell(row["review_state"])}</span><span class="metadata">分類：{cell(row["review_classification"])}<br>來源：{cell(row["review_source"])}<br>{sample_note}</span></td><td>{cell(row["reviewer"])}<span class="metadata">{("版本 " + str(row["review_revision"])) if row["review_revision"] else "未記錄差異"}</span></td><td><a class="button secondary" href="{link}">查看來源資料</a></td></tr>'''
    listing = (
        f'<div class="scroll" role="region" tabindex="0" aria-label="待核對商品，可左右捲動"><table><thead><tr><th>商品與採集批次</th><th>網站核對狀態</th><th>填寫人（自填）</th><th>操作</th></tr></thead><tbody>{items}</tbody></table></div>'
        if items
        else '<p class="empty">沒有符合條件的採集紀錄，請調整篩選。</p>'
    )
    pager = f"<span>第 {data['page']} / {data['pages']} 頁</span>"
    if data["page"] > 1:
        pager = f'<a href="{url("/reviews", query, page=data["page"] - 1)}">上一頁</a>' + pager
    if data["page"] < data["pages"]:
        pager += f'<a href="{url("/reviews", query, page=data["page"] + 1)}">下一頁</a>'
    body = f'''<section class="hero"><div><p class="eyebrow">採集資料</p><h2>直接查看來源規格、評分與評論</h2><p>保留採集當時的基準資料。評論原文、星等與日期直接來自來源，不需要自行評價或標註。</p></div><a class="button" href="#review-list">選擇商品</a></section>
<div class="stats"><div class="stat"><span>此範圍採集紀錄</span><strong>{data["total"]}<span>筆</span></strong></div><div class="stat"><span>已留存來源資料</span><strong>{captured}<span>筆</span></strong></div><div class="stat"><span>留存評論樣本</span><strong>{sample_count}<span>則</span></strong><small>按批次計算，不代表全部評論</small></div></div>
<section class="card"><h2>選擇核對範圍</h2><form method="get" action="/reviews"><div class="filters">{controls}</div><button type="submit">套用篩選</button></form><p class="hint">顯示各批次的獨立觀測。網站結果存於本機資料庫；Excel 裡填過的結論尚未匯入網站。</p></section>
<section class="card" id="review-list"><div class="toolbar"><h2>商品核對清單</h2><a class="button secondary" href="{url("/exports/preview", query, columns=[*DEFAULT_COLUMNS, *REVIEW_FIELDS])}">匯出含核對結果的 CSV</a></div>{listing}<nav class="pagination" aria-label="核對清單分頁">{pager}</nav></section>'''
    return shell("來源查驗", "/reviews", query.source, body)


def detail_page(subject, token, draft=None, message="", saved=False):
    run, product = subject["run"], subject["product"]
    source, run_id, asin = run["source"], run["id"], product["asin"]
    current = subject["latest"]
    values = dict(
        current
        or {"classification": "待確認", "source_check": "待確認", "note": "", "reviewer": ""}
    )
    if subject["values"]["review_state"] == "資料變更，需重核":
        values.update(classification="待確認", source_check="待確認")
    if draft is not None:
        values.update(
            {
                key: draft.get(key, "")
                for key in ("classification", "source_check", "note", "reviewer")
            }
        )
    revision = current["revision"] if current else 0
    latest_note = (
        f'<p>目前版本 {revision}：分類「{cell(current["classification"])}」、來源「{cell(current["source_check"])}」；{cell(current["reviewer"])} 於 {taipei_time(current["updated_at"])} 填寫。</p><p style="white-space:pre-wrap;overflow-wrap:anywhere">目前備註：{cell(current["note"])}</p>'
        if current
        else "<p>這筆採集紀錄尚無網站核對結果。</p>"
    )
    feedback = (
        f'<div class="feedback" role="alert"><strong>尚未儲存</strong><p>{cell(message)}</p><p>下方保留你的輸入。比較本頁最新結果後，才能再次按下儲存。</p></div>'
        if message
        else ""
    )
    if saved:
        feedback += '<p class="note" role="status">已儲存核對結果。需要修正時可再次編輯，先前版本會保留。</p>'
    audits = source_audit_rows(
        run, [product], {asin: subject["evidence"]} if subject["evidence"] else {}
    )
    comparison = audit_details(
        audits,
        subject["verification"]["issues"],
        source,
        {asin: subject["evidence"]} if subject["evidence"] else {},
    ).replace('<details class="audit-item"', '<details open class="audit-item"', 1)
    evidence = subject["evidence"] or {}
    rating_count = product["rating_count"]
    rating_text = "未提供" if rating_count is None else f"{rating_count:,}"
    distribution = evidence.get("rating_distribution_percent", {})
    distribution_text = (
        "、".join(f"{star} 星 {distribution[star]}%" for star in ("5", "4", "3", "2", "1"))
        if distribution
        else "本批次未留存星級分布"
    )
    samples = evidence.get("top_review_samples", [])
    sample_items = "".join(
        f'<article class="source-review"><h3>{cell(unescape(item["review_title"]))}</h3>'
        f'<p class="metadata">{cell(item["review_star_rating"])} 星 · {cell(item["review_date"])}<br>來源評論 ID：{cell(item["review_id"])}</p>'
        f'<p class="sample-text">{cell(unescape(item["review_comment"]))}</p></article>'
        for item in samples
    )
    sample_note = (
        f"<p>本批次留存 {len(samples)} 則精選評論樣本；不代表全部評論。以下直接顯示採集到的原文。</p>{sample_items}"
        if samples
        else "<p>本批次未留存逐則評論樣本。</p>"
    )
    controls = select_field(
        "classification", "分類判斷", values["classification"], [(x, x) for x in JUDGMENTS]
    )
    controls += select_field(
        "source_check", "來源與入庫核對", values["source_check"], [(x, x) for x in JUDGMENTS]
    )
    controls += input_field(
        "reviewer",
        "填寫人名稱（自填）",
        values["reviewer"],
        extra='required maxlength="80" autocomplete="name"',
    )
    mapped = "".join(
        f'<tr><td>{cell(row["source_section"])}<span class="metadata">{cell(row["field_name"])}</span></td><td>{cell(row["source_value"])}</td><td>{cell(SPEC_LABELS[row["dimension"]])}<span class="metadata">候選：{cell(row["candidate_value"])}</span></td><td>{cell(row["stored_value"])}</td><td>{cell(row["status"])}</td></tr>'
        for row in subject["specification_mapping"]
    )
    mapping = (
        '<div class="scroll" role="region" tabindex="0" aria-label="規格來源對照，可左右捲動"><table><thead><tr><th>來源區塊與欄名</th><th>來源原值</th><th>標準欄位／候選值</th><th>既有入庫值</th><th>狀態</th></tr></thead><tbody>'
        + mapped
        + "</tbody></table></div>"
        if mapped
        else '<p class="empty">此批次沒有可對應的留存規格。不能從商品標題補成已確認規格。</p>'
    )
    history = "".join(
        f'<tr><td>v{row["revision"]}<span class="metadata">{taipei_time(row["updated_at"])}</span></td><td>{cell(row["reviewer"])}</td><td>{cell(row["classification"])}／{cell(row["source_check"])}</td><td style="white-space:pre-wrap;overflow-wrap:anywhere">{cell(row["note"])}</td><td>{"目前來源" if row["fingerprint"] == subject["fingerprint"] else "舊來源，需重核"}</td></tr>'
        for row in subject["history"]
    )
    history_html = (
        f'<div class="scroll" role="region" tabindex="0" aria-label="核對修改歷程，可左右捲動"><table><thead><tr><th>版本／台灣時間</th><th>填寫人（自填）</th><th>分類／來源</th><th>備註</th><th>資料依據</th></tr></thead><tbody>{history}</tbody></table></div>'
        if history
        else "<p>儲存第一筆結果後，這裡會顯示修改歷程。</p>"
    )
    action = url(f"/reviews/{run_id}/{asin}", source=source)
    body = f'''<p><a class="table-link" href="{url("/reviews", source=source)}">← 返回來源清單</a> · <a class="table-link" href="#source-reviews">查看來源評論</a></p>{feedback}
<section class="card"><span class="badge neutral">批次 #{run_id} · {cell(asin)}</span><h2>{cell(product["title"])}</h2><p>搜尋分類：<strong>{cell(run["parameters"]["category"])}</strong> · 採集價 {cell(product["price"])} {cell(product["currency"])}</p><p class="hint">採集時間：{taipei_time(product["observed_at"])}（台灣）。本頁以已保存來源為基準。</p></section>
<section class="card" id="source-reviews"><span id="sample-labels" aria-hidden="true"></span><h2>來源評價與評論</h2><p>評分：{cell(product["rating"])}；商品累計評價數：{rating_text}。評價數不是逐則評論筆數。</p><p>星級分布：{distribution_text}。</p><p class="hint">資料來源：{cell(source)} 商品明細。評論星等、日期與文字保留來源內容，不新增人工主題或正負向判斷。</p>{sample_note}</section>
<section class="card"><h2>1. 比對採集當時的資料</h2><p>先確認標題、價格與規格。Amazon 現在的頁面可能已改價，不能取代留存來源。</p>{comparison}</section>
<section class="card"><h2>來源規格對照</h2><p>依來源欄名精確對應材質、制式、尺寸與顏色。候選值與原文分開保留，規格衝突須再核對。這裡的候選尚未套用到商品或規格組合統計。</p>{mapping}<p class="hint">手機可左右滑動查看各欄。</p></section>
<section class="card" id="review-form"><details {"open" if message or saved else ""}><summary>資料核對紀錄（選用）</summary><p>此區只記錄來源或搜尋分類的資料差異，不評價商品或評論。閱讀來源資料不需要填寫。</p>{latest_note}<form method="post" action="{action}"><input type="hidden" name="csrf" value="{escape(token, quote=True)}"><input type="hidden" name="revision" value="{revision}"><input type="hidden" name="fingerprint" value="{subject["fingerprint"]}"><div class="filters">{controls}</div><div class="field"><label for="note">核對備註（可記錄差異與查看日期）</label><textarea id="note" name="note" maxlength="2000" rows="5" style="width:100%;font:inherit;padding:12px;border:1px solid #bbc9c0;border-radius:8px">{escape(str(values["note"]))}</textarea></div><p class="hint">填寫人為自填名稱。儲存只記錄資料核對差異，不修改採集資料或呼叫供應商。</p><div class="filter-actions"><button type="submit">儲存資料核對紀錄</button><a class="button secondary" href="{url("/reviews", source=source)}">返回清單</a></div></form><h3>資料核對修改歷程</h3>{history_html}</details></section>'''
    return shell("來源資料", "/reviews", source, body)
