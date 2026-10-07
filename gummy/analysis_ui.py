"""Period sample analysis with traceable aggregates, filters and matching exports."""

from datetime import datetime, timedelta

from gummy.analysis import GROUP_LABELS, PRICE_BANDS
from gummy.catalog import TAIPEI
from gummy.ui import cell
from gummy.workspace_ui import (
    input_field,
    period_fields,
    scope_note,
    select_field,
    shell,
    table,
    url,
)


def number(value):
    return f"{value:,}" if value is not None else "未提供"


def analysis_page(data):
    query, summary = data["query"], data["summary"]
    today = datetime.now(TAIPEI).date()
    presets = "".join(
        f'<a class="button secondary" href="{url("/analysis", query, date_from=start, date_to=end, snapshot=None, review_snapshot=None, page=1)}">{label}</a>'
        for label, start, end in (
            ("最近 7 天", today - timedelta(days=6), today),
            ("本月", today.replace(day=1), today),
            ("全部期間", None, None),
        )
    )
    controls = period_fields(query)
    for key, label in (("category", "搜尋分類"), ("brand", "品牌")):
        options = sorted(
            set(data["facets"][key]) | ({getattr(query, key)} if getattr(query, key) else set())
        )
        controls += select_field(
            key, label, getattr(query, key), [("", "全部"), *((x, x) for x in options)]
        )
    controls += select_field(
        "price_band", "價格帶 USD", query.price_band, [("", "全部價格"), *PRICE_BANDS.items()]
    )
    controls += select_field("group_by", "統計方式", query.group_by, GROUP_LABELS.items())
    controls += input_field("q", "搜尋商品、ASIN 或品牌", query.q, extra='maxlength="120"')
    advanced = input_field(
        "min_price", "最低價格 USD", query.min_price, "number", 'min="0" step="0.01"'
    ) + input_field("max_price", "最高價格 USD", query.max_price, "number", 'min="0" step="0.01"')
    exports = (
        f'<a class="button" href="{url("/v1/exports/analysis.csv", query)}">下載統計 CSV</a>'
        f'<a class="button secondary" href="{url("/v1/exports/analysis-products.csv", query)}">下載商品明細 CSV</a>'
        if data["total"]
        else '<p class="hint">目前沒有符合資料，調整條件後即可匯出。</p>'
    )
    rows = ""
    for group in data["groups"]:
        label = group["group_value"] or "品牌未提供"
        updates = (
            {"category": group["group_value"], "price_band": group["price_band"]}
            if query.group_by == "category_price_band"
            else {query.group_by: group["group_value"]}
        )
        drill = (
            f'<a class="table-link" href="{url("/analysis", query, page=1, **updates)}#selected-products">查看 {group["product_count"]} 件商品</a>'
            if group["group_value"]
            else "品牌尚未提供，可於下方明細查看"
        )
        rows += f"""<tr><th scope="row">{cell(label)}</th><td>{cell(group["price_band_label"] or "全部符合價格")}</td><td class="nowrap">{number(group["product_count"])}</td><td class="nowrap">{number(group["known_rating_count_sum"])}<span class="metadata">{group["rating_count_product_count"]} / {group["product_count"]} 件有值</span></td><td class="nowrap">{number(group["average_price"])}<span class="metadata">{group["priced_product_count"]} 件有價格</span></td><td>{number(group["retained_review_sample_count"])}</td><td>{drill}</td></tr>"""
    grouped = (
        '<div class="scroll" tabindex="0" role="region" aria-label="取樣統計，可左右捲動"><table>'
        '<caption class="hint">每件商品只計選定期間最新一次；價格與評分數依擷取時資料。</caption>'
        "<thead><tr><th>搜尋分類／品牌</th><th>價格帶 USD</th><th>取樣商品</th>"
        "<th>已知累計評分數合計</th><th>取樣均價 USD</th><th>已存評論樣本</th><th>查看明細</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
        if rows
        else '<div class="empty"><h3>這個範圍還沒有可統計的商品</h3><p>請放寬日期、分類或價格條件。查詢不會自動抓取新資料。</p></div>'
    )
    pager = f"<span>第 {data['page']} / {data['pages']} 頁</span>"
    if data["page"] > 1:
        pager = (
            f'<a class="button secondary" href="{url("/analysis", query, page=data["page"] - 1)}#selected-products">上一頁</a>'
            + pager
        )
    if data["page"] < data["pages"]:
        pager += f'<a class="button secondary" href="{url("/analysis", query, page=data["page"] + 1)}#selected-products">下一頁</a>'
    body = f'''<p class="muted">選一期資料，比較已抓取商品的價格與評價，再下載同一範圍。</p>
<section class="card"><h2>1. 選擇研究範圍</h2><form method="get" action="/analysis"><div class="filters">{controls}</div><details {"open" if query.min_price is not None or query.max_price is not None else ""}><summary>自訂價格範圍</summary><div class="filters">{advanced}</div></details>{f'<input type="hidden" name="run_id" value="{query.run_id}">' if query.run_id else ""}<div class="filter-actions"><button type="submit">套用範圍</button><a class="button secondary" href="{url("/analysis", source=query.source)}">清除篩選</a></div></form><div class="filter-actions">{presets}</div><p class="hint">日期依批次完成的台灣日期計算。先選每個 ASIN 在期間內最新一筆，再篩選分類、品牌與價格；不會取回舊價格或舊分類湊入統計。</p>{f'<p class="note">目前限定批次 #{query.run_id}。<a href="{url("/analysis", query, run_id=None, snapshot=None, review_snapshot=None, page=1)}">查看同期間全部批次</a></p>' if query.run_id else ""}</section>
<section aria-label="取樣範圍摘要" class="stats analysis-stats"><div class="stat"><span>取樣商品</span><strong>{number(summary["product_count"])}<span>件</span></strong><small>每件商品只計一次</small></div><div class="stat"><span>取樣均價 USD</span><strong>{number(summary["average_price"])}</strong><small>{summary["priced_product_count"]} 件有價格；缺值不當成零</small></div><div class="stat"><span>取樣累計評分數合計</span><strong>{number(summary["known_rating_count_sum"])}</strong><small>{summary["rating_count_product_count"]} / {summary["product_count"]} 件有值；非本期新增</small></div><div class="stat"><span>已存評論樣本</span><strong>{number(summary["retained_review_sample_count"])}<span>則</span></strong><small>只計最新觀測留存的精選樣本</small></div></section>
<section class="card"><div class="toolbar"><h2>2. {GROUP_LABELS[query.group_by]}取樣統計</h2><div class="filter-actions">{exports}</div></div>{scope_note(data)}<p class="hint"><a class="table-link" href="{url("/analysis", query, snapshot=None, review_snapshot=None, page=1)}">更新至最新已存資料</a></p><p class="note">這是已取樣商品的累計評分數，不是本期新增評論、完整市場總數或銷售額。評論樣本也不是所有有文字的評論；未做主題或正負向分類。</p><p class="hint">表格可左右滑動查看完整欄位；也可下載 CSV 閱讀。</p>{grouped}<details><summary>統計口徑與資料缺口</summary><p>未選分類時，同一商品曾出現在多個搜尋分類，也只保留期間內最新一次的分類標籤。搜尋分類尚待人工確認。{summary["evidence_product_count"]} / {summary["product_count"]} 件留存來源證據；缺證紀錄仍可查看，尚不能完整對帳。</p><p>價格帶依 USD 分為 0–未滿 10、10–未滿 20、20–未滿 50、50 以上及未提供；這是本站取樣區間，與範例檔的各分類價格帶不同。統計與明細匯出均固定本次成功批次及網站核對版本。</p></details></section>
<section class="card" id="selected-products"><h2>3. 這份統計包含的商品</h2><p>{data["total"]} 件商品；下方顯示第 {data["page"]} 頁。明細 CSV 會包含全部符合的商品。</p><p class="hint">表格可左右滑動查看完整欄位。</p>{table(data["page_rows"], query.source)}<nav class="pagination" aria-label="統計商品分頁">{pager}</nav></section>'''
    return shell("取樣分析", "/periods", query.source, body)
