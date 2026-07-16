import argparse
import html
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from markdown_table import split_markdown_row


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_MARKDOWN_PATH = BASE_DIR / "high_price_dvd_stock.md"
DEFAULT_HTML_PATH = BASE_DIR / "index.html"

EXPECTED_HEADERS = (
    "No",
    "商品タイトル",
    "価格 (中古)",
    "店舗在庫状況",
    "詳細リンク",
)

PREFECTURES_ORDER = [
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県",
    "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県",
    "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県",
    "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
]
DEFAULT_PREFECTURE = "神奈川県"

UNVERIFIED_STATUSES = {
    "age_verification",
    "identity_mismatch",
    "modal_invalid",
}
ATTENTION_STATUSES = UNVERIFIED_STATUSES | {"fetch_error"}

DETAIL_LINK_RE = re.compile(r"^\[[^\]\r\n]+\]\(([^()\s]+)\)$")
DETAIL_URL_RE = re.compile(
    r"^https://shopping\.bookoff\.co\.jp/(used|new)/(\d+)$"
)
SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")
AVAILABLE_RE = re.compile(r"^入荷店舗: ([1-9]\d*)店$")
UNVERIFIED_RE = re.compile(
    r"^確認保留 \[(age_verification|identity_mismatch|modal_invalid)\]:\s*(.+)$"
)
FETCH_ERROR_RE = re.compile(r"^取得失敗 \[fetch_error\]:\s*(.+)$")
PREFECTURE_SUFFIX_RE = re.compile(
    r"^(?P<name>.+?)\s+\((?P<pref>" + "|".join(map(re.escape, PREFECTURES_ORDER)) + r")\)$"
)


class MarkdownDataError(ValueError):
    """Raised when the generated Markdown cannot be converted without data loss."""


def parse_price_val(price_str):
    """Return the first comma-formatted integer in a Bookoff price string."""
    match = re.search(r"\d[\d,]*", price_str or "")
    if not match:
        return 0
    return int(match.group(0).replace(",", ""))


def extract_detail_url(link_cell):
    """Extract and validate a supported Bookoff product URL from a Markdown link."""
    link_match = DETAIL_LINK_RE.fullmatch(link_cell.strip())
    if not link_match:
        raise MarkdownDataError("detail link must be a non-empty Markdown link")

    detail_url = link_match.group(1)
    url_match = DETAIL_URL_RE.fullmatch(detail_url)
    if not url_match:
        raise MarkdownDataError(
            "detail URL must match "
            "https://shopping.bookoff.co.jp/(used|new)/digits"
        )
    return detail_url, url_match.group(2)


def parse_store(store_text):
    """Parse a store and its optional prefecture suffix."""
    store_text = store_text.strip()
    if not store_text:
        raise MarkdownDataError("stock entry contains an empty store")

    match = PREFECTURE_SUFFIX_RE.fullmatch(store_text)
    if not match:
        return {"store_name": store_text, "prefecture": "不明"}

    store_name = match.group("name").strip()
    if not store_name:
        raise MarkdownDataError("stock entry contains an empty store name")
    return {"store_name": store_name, "prefecture": match.group("pref")}


def parse_stock_cell(stock_text):
    """Parse the exact stock/verification state emitted by the parent scraper."""
    stock_text = stock_text.strip()
    if stock_text == "在庫なし (入荷店舗: 0店)":
        return {
            "stock_status": "no_stock",
            "status_reason": "",
            "stores": [],
        }

    parts = stock_text.split("<br>")
    available_match = AVAILABLE_RE.fullmatch(parts[0])
    if available_match:
        expected_count = int(available_match.group(1))
        store_parts = parts[1:]
        if len(store_parts) != expected_count:
            raise MarkdownDataError(
                f"stock count says {expected_count}, but {len(store_parts)} stores were listed"
            )
        stores = [parse_store(store) for store in store_parts]
        return {
            "stock_status": "available",
            "status_reason": "",
            "stores": stores,
        }

    unverified_match = UNVERIFIED_RE.fullmatch(stock_text)
    if unverified_match:
        reason = unverified_match.group(2).strip()
        if not reason:
            raise MarkdownDataError("confirmation hold is missing its reason")
        return {
            "stock_status": unverified_match.group(1),
            "status_reason": reason,
            "stores": [],
        }

    fetch_match = FETCH_ERROR_RE.fullmatch(stock_text)
    if fetch_match:
        reason = fetch_match.group(1).strip()
        if not reason:
            raise MarkdownDataError("fetch failure is missing its reason")
        return {
            "stock_status": "fetch_error",
            "status_reason": reason,
            "stores": [],
        }

    raise MarkdownDataError(f"unsupported stock status: {stock_text!r}")


def parse_product_row(columns, line_number=None):
    """Convert one logical five-column Markdown row into a product dictionary."""
    prefix = f"line {line_number}: " if line_number is not None else ""
    if len(columns) != len(EXPECTED_HEADERS):
        raise MarkdownDataError(
            f"{prefix}expected {len(EXPECTED_HEADERS)} columns, got {len(columns)}"
        )

    no_text, title, price_str, stock_text, link_cell = (cell.strip() for cell in columns)
    if not no_text.isdigit() or int(no_text) < 1:
        raise MarkdownDataError(f"{prefix}No must be a positive integer")
    if not title:
        raise MarkdownDataError(f"{prefix}product title is empty")
    if not price_str or parse_price_val(price_str) <= 0:
        raise MarkdownDataError(f"{prefix}price does not contain a positive integer")

    try:
        detail_url, product_id = extract_detail_url(link_cell)
        stock = parse_stock_cell(stock_text)
    except MarkdownDataError as exc:
        raise MarkdownDataError(f"{prefix}{exc}") from exc

    return {
        "no": int(no_text),
        "product_id": product_id,
        "title": title,
        "price_str": price_str,
        "price_val": parse_price_val(price_str),
        "detail_url": detail_url,
        "stock_status": stock["stock_status"],
        "status_reason": stock["status_reason"],
        "stores": stock["stores"],
    }


def _is_separator_row(columns):
    return (
        len(columns) == len(EXPECTED_HEADERS)
        and all(SEPARATOR_RE.fullmatch(column.strip()) for column in columns)
    )


def parse_markdown(markdown_text):
    """Parse the generated product table, failing instead of returning partial data."""
    if not isinstance(markdown_text, str):
        raise TypeError("markdown_text must be a string")

    lines = markdown_text.splitlines()
    header_index = None
    for index, line in enumerate(lines):
        if not line.strip().startswith("|"):
            continue
        try:
            columns = split_markdown_row(line)
        except ValueError:
            continue
        if tuple(columns) == EXPECTED_HEADERS:
            header_index = index
            break

    if header_index is None:
        raise MarkdownDataError("required five-column product table header was not found")
    if header_index + 1 >= len(lines):
        raise MarkdownDataError("product table separator row is missing")

    separator_line = lines[header_index + 1]
    try:
        separator_columns = split_markdown_row(separator_line)
    except ValueError as exc:
        raise MarkdownDataError(
            f"line {header_index + 2}: malformed product table separator"
        ) from exc
    if not _is_separator_row(separator_columns):
        raise MarkdownDataError(
            f"line {header_index + 2}: malformed product table separator"
        )

    products = []
    seen_urls = set()
    for index in range(header_index + 2, len(lines)):
        line_number = index + 1
        line = lines[index]
        if not line.strip():
            continue
        if not line.strip().startswith("|"):
            raise MarkdownDataError(
                f"line {line_number}: malformed data row must start and end with a pipe"
            )
        try:
            columns = split_markdown_row(line)
        except ValueError as exc:
            raise MarkdownDataError(f"line {line_number}: malformed data row: {exc}") from exc

        product = parse_product_row(columns, line_number=line_number)
        detail_url = product["detail_url"]
        if detail_url in seen_urls:
            raise MarkdownDataError(
                f"line {line_number}: duplicate detail URL: {detail_url}"
            )
        seen_urls.add(detail_url)
        products.append(product)

    if not products:
        raise MarkdownDataError("product table contains zero products")
    return products


def parse_markdown_file(markdown_path):
    """Read and parse a UTF-8 Markdown file."""
    path = Path(markdown_path)
    with path.open("r", encoding="utf-8") as source:
        return parse_markdown(source.read())


def json_for_html(value):
    """Serialize JSON so source data cannot terminate an HTML script element."""
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return (
        payload.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def pref_options_html(prefectures, selected=""):
    """Build trusted static options with normal HTML escaping."""
    options = ['<option value="">全国</option>']
    for prefecture in prefectures:
        escaped = html.escape(prefecture, quote=True)
        selected_attr = " selected" if prefecture == selected else ""
        options.append(f'<option value="{escaped}"{selected_attr}>{escaped}</option>')
    return "\n".join(options)


def time_str_placeholder():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


HTML_TEMPLATE = """<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <link rel="icon" href="data:,">
  <title>高額ソフト公式掲載チェッカー</title>
  <style>
    :root {
      --bg: #0b0e14;
      --surface: #171b24;
      --surface-strong: #202631;
      --border: #343c49;
      --text: #f5f7fa;
      --muted: #a7b0bd;
      --primary: #7dd3fc;
      --primary-bg: #163444;
      --success: #6ee7a8;
      --warning: #fbbf5a;
      --danger: #fb8c8c;
      --focus: #f8d36b;
    }

    * { box-sizing: border-box; }
    html { background: var(--bg); }
    body {
      margin: 0;
      min-width: 280px;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans JP", sans-serif;
      line-height: 1.55;
    }
    button, input, select { font: inherit; }
    button, select { cursor: pointer; }
    button:focus-visible, input:focus-visible, select:focus-visible, a:focus-visible {
      outline: 3px solid var(--focus);
      outline-offset: 2px;
    }
    [hidden] { display: none !important; }

    .shell {
      width: min(100% - 24px, 1100px);
      margin: 0 auto;
      padding: 24px 0 48px;
    }
    .app-header { margin-bottom: 18px; }
    .eyebrow {
      color: var(--primary);
      font-size: .76rem;
      font-weight: 800;
      letter-spacing: 0;
    }
    h1 {
      margin: 4px 0 6px;
      font-size: 2.15rem;
      line-height: 1.25;
      letter-spacing: 0;
    }
    .updated { margin: 0; color: var(--muted); font-size: .85rem; }

    .tabs {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 6px;
      padding: 6px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
    }
    .tab {
      min-width: 0;
      min-height: 44px;
      padding: 8px 6px;
      border: 1px solid transparent;
      border-radius: 6px;
      background: transparent;
      color: var(--muted);
      font-size: .9rem;
      font-weight: 750;
      letter-spacing: 0;
      overflow-wrap: anywhere;
    }
    .tab[aria-selected="true"] {
      border-color: #397391;
      background: var(--primary-bg);
      color: #dff5ff;
    }

    .filters {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(180px, .38fr);
      gap: 12px;
      margin: 12px 0 16px;
      padding: 14px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
    }
    .field { min-width: 0; }
    .field label {
      display: block;
      margin-bottom: 5px;
      color: var(--muted);
      font-size: .78rem;
      font-weight: 700;
    }
    input, select {
      width: 100%;
      min-height: 44px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: #0f131a;
      color: var(--text);
      padding: 9px 11px;
    }
    input::placeholder { color: #747e8c; }

    .summary {
      min-height: 28px;
      margin: 0 2px 10px;
      color: var(--muted);
      font-size: .9rem;
    }
    .summary strong { color: var(--warning); }
    .results { display: grid; gap: 10px; }

    .card {
      min-width: 0;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      overflow: hidden;
    }
    .card-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      padding: 14px;
      border-bottom: 1px solid var(--border);
      background: var(--surface-strong);
    }
    .card-title {
      min-width: 0;
      margin: 0;
      font-size: 1.03rem;
      line-height: 1.4;
      overflow-wrap: anywhere;
      letter-spacing: 0;
    }
    .badge {
      flex: 0 0 auto;
      display: inline-flex;
      align-items: center;
      min-height: 25px;
      max-width: 100%;
      padding: 3px 8px;
      border: 1px solid #456274;
      border-radius: 999px;
      background: #162b37;
      color: #cdefff;
      font-size: .75rem;
      font-weight: 750;
      overflow-wrap: anywhere;
    }
    .badge.available { border-color: #316c50; background: #142d22; color: var(--success); }
    .badge.no-stock { border-color: #505966; background: #242a33; color: #c6ccd4; }
    .badge.pending { border-color: #806028; background: #342711; color: var(--warning); }
    .badge.error { border-color: #7d3f45; background: #371b1f; color: var(--danger); }

    .rows { display: grid; }
    .product-row, .product-card-body {
      min-width: 0;
      padding: 13px 14px;
    }
    .product-row + .product-row { border-top: 1px solid var(--border); }
    .product-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: center;
      gap: 12px;
    }
    .product-info { min-width: 0; }
    .product-title {
      margin: 0 0 7px;
      color: var(--text);
      font-size: .94rem;
      font-weight: 700;
      line-height: 1.45;
      overflow-wrap: anywhere;
      letter-spacing: 0;
    }
    .meta {
      display: flex;
      flex-wrap: wrap;
      gap: 6px 12px;
      color: var(--muted);
      font-size: .8rem;
    }
    .price { color: var(--warning); font-weight: 800; }
    .detail-link {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 40px;
      padding: 8px 12px;
      border: 1px solid #397391;
      border-radius: 6px;
      background: var(--primary-bg);
      color: #e3f6ff;
      font-size: .84rem;
      font-weight: 750;
      text-decoration: none;
      white-space: nowrap;
    }
    .status-line { margin: 10px 0 0; color: var(--muted); font-size: .86rem; overflow-wrap: anywhere; }
    .reason {
      margin: 10px 0 0;
      padding: 9px 10px;
      border-left: 3px solid var(--warning);
      background: #211d15;
      color: #f4dfb5;
      font-size: .86rem;
      overflow-wrap: anywhere;
    }
    .reason.error { border-color: var(--danger); background: #241719; color: #f7c6c6; }
    .stores-text { margin: 10px 0 0; color: #cbd2dc; font-size: .82rem; overflow-wrap: anywhere; }
    .empty, .no-js {
      padding: 28px 18px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      color: var(--muted);
      text-align: center;
    }

    @media (max-width: 640px) {
      .shell { width: min(100% - 16px, 1100px); padding-top: 16px; }
      h1 { font-size: 1.45rem; }
      .filters { grid-template-columns: 1fr; }
      .card-head { flex-direction: column; }
      .product-row { grid-template-columns: minmax(0, 1fr); }
      .detail-link { justify-self: start; }
      .tab { font-size: .82rem; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header class="app-header">
      <div class="eyebrow">BOOKOFF STOCK</div>
      <h1>高額ソフト公式掲載チェッカー</h1>
      <p class="updated">最終更新: __GENERATED_AT__ / 全 __TOTAL_COUNT__ 商品 / 確認保留 __PENDING_COUNT__ 件 / 取得失敗 __FETCH_ERROR_COUNT__ 件</p>
    </header>

    <main>
      <nav class="tabs" role="tablist" aria-label="表示モード">
        <button class="tab" type="button" role="tab" data-mode="stores" aria-selected="true">公式掲載店舗</button>
        <button class="tab" type="button" role="tab" data-mode="pending" aria-selected="false">確認保留</button>
        <button class="tab" type="button" role="tab" data-mode="all" aria-selected="false">全商品</button>
      </nav>

      <section class="filters" aria-label="絞り込み">
        <div class="field">
          <label for="search-input" id="search-label">商品名・掲載店舗名で検索</label>
          <input id="search-input" type="search" autocomplete="off" placeholder="商品名または掲載店舗名">
        </div>
        <div class="field" id="prefecture-field">
          <label for="prefecture-select">都道府県</label>
          <select id="prefecture-select">__PREFECTURE_OPTIONS__</select>
        </div>
      </section>

      <p class="summary" id="result-summary" aria-live="polite"></p>
      <section class="results" id="results" role="tabpanel" aria-live="polite"></section>

      <noscript>
        <div class="no-js">JavaScriptを有効にすると、全 __TOTAL_COUNT__ 商品の公式掲載店舗・確認保留・掲載0店を検索できます。</div>
      </noscript>
    </main>
  </div>

  <script type="application/json" id="product-data">__PRODUCT_DATA__</script>
  <script>
    "use strict";

    const products = JSON.parse(document.getElementById("product-data").textContent);
    const pendingStatuses = new Set(["age_verification", "identity_mismatch", "modal_invalid", "fetch_error"]);
    const statusLabels = {
      available: "公式掲載あり",
      no_stock: "公式掲載0店",
      age_verification: "年齢確認のため保留",
      identity_mismatch: "商品照合を保留",
      modal_invalid: "店舗情報の照合を保留",
      fetch_error: "取得失敗"
    };

    const searchInput = document.getElementById("search-input");
    const searchLabel = document.getElementById("search-label");
    const prefectureField = document.getElementById("prefecture-field");
    const prefectureSelect = document.getElementById("prefecture-select");
    const prefectureOrder = new Map(
      Array.from(prefectureSelect.options)
        .filter((option) => option.value)
        .map((option, index) => [option.value, index])
    );
    const results = document.getElementById("results");
    const resultSummary = document.getElementById("result-summary");
    const tabs = Array.from(document.querySelectorAll("[data-mode]"));
    let currentMode = "stores";

    function element(tagName, className, text) {
      const node = document.createElement(tagName);
      if (className) node.className = className;
      if (text !== undefined) node.textContent = text;
      return node;
    }

    function normalized(value) {
      return String(value || "").toLocaleLowerCase("ja-JP");
    }

    function productMatches(product, query, includeReason) {
      if (!query) return true;
      const values = [product.title];
      for (const store of product.stores) {
        values.push(store.store_name, store.prefecture);
      }
      if (includeReason) values.push(product.status_reason);
      return values.some((value) => normalized(value).includes(query));
    }

    function detailLink(product) {
      const link = element("a", "detail-link", "詳細を見る");
      link.href = product.detail_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      return link;
    }

    function productInfo(product) {
      const info = element("div", "product-info");
      info.appendChild(element("h3", "product-title", product.title));
      const meta = element("div", "meta");
      meta.appendChild(element("span", "", "No. " + product.no));
      meta.appendChild(element("span", "price", product.price_str));
      info.appendChild(meta);
      return info;
    }

    function badgeFor(product) {
      let badgeClass = "badge";
      if (product.stock_status === "available") badgeClass += " available";
      else if (product.stock_status === "no_stock") badgeClass += " no-stock";
      else if (product.stock_status === "fetch_error") badgeClass += " error";
      else badgeClass += " pending";
      return element("span", badgeClass, statusLabels[product.stock_status] || product.stock_status);
    }

    function statusLine(product) {
      if (product.stock_status === "available") {
        return "公式ページ掲載: " + product.stores.length + "店";
      }
      if (product.stock_status === "no_stock") {
        return "公式ページ掲載: 0店";
      }
      return statusLabels[product.stock_status] || product.stock_status;
    }

    function productCard(product) {
      const card = element("article", "card");
      const head = element("div", "card-head");
      head.appendChild(productInfo(product));
      head.appendChild(badgeFor(product));
      card.appendChild(head);

      const body = element("div", "product-card-body");
      if (product.stock_status === "available" || product.stock_status === "no_stock") {
        body.appendChild(element("p", "status-line", statusLine(product)));
      }
      if (product.status_reason) {
        const reasonClass = product.stock_status === "fetch_error" ? "reason error" : "reason";
        body.appendChild(element("p", reasonClass, product.status_reason));
      }
      if (product.stores.length) {
        const storeNames = product.stores.map((store) => {
          if (store.prefecture === "不明") return store.store_name;
          return store.store_name + " (" + store.prefecture + ")";
        });
        body.appendChild(element("p", "stores-text", storeNames.join(" / ")));
      }
      body.appendChild(detailLink(product));
      card.appendChild(body);
      return card;
    }

    function storeProductRow(product) {
      const row = element("div", "product-row");
      row.appendChild(productInfo(product));
      row.appendChild(detailLink(product));
      return row;
    }

    function emptyState(message) {
      results.replaceChildren(element("div", "empty", message));
    }

    function renderStores(query) {
      const selectedPrefecture = prefectureSelect.value;
      const storeMap = new Map();

      for (const product of products) {
        if (product.stock_status !== "available") continue;
        for (const store of product.stores) {
          if (selectedPrefecture && store.prefecture !== selectedPrefecture) continue;
          const matches = !query
            || normalized(product.title).includes(query)
            || normalized(store.store_name).includes(query)
            || normalized(store.prefecture).includes(query);
          if (!matches) continue;

          const key = store.prefecture + "\u0000" + store.store_name;
          if (!storeMap.has(key)) {
            storeMap.set(key, {
              store_name: store.store_name,
              prefecture: store.prefecture,
              products: [],
              product_urls: new Set()
            });
          }
          const storeEntry = storeMap.get(key);
          if (!storeEntry.product_urls.has(product.detail_url)) {
            storeEntry.product_urls.add(product.detail_url);
            storeEntry.products.push(product);
          }
        }
      }

      const stores = Array.from(storeMap.values());
      for (const store of stores) {
        store.products.sort((a, b) => b.price_val - a.price_val || a.no - b.no);
      }
      stores.sort((a, b) => {
        if (!selectedPrefecture) {
          const aPrefectureOrder = prefectureOrder.get(a.prefecture) ?? Number.MAX_SAFE_INTEGER;
          const bPrefectureOrder = prefectureOrder.get(b.prefecture) ?? Number.MAX_SAFE_INTEGER;
          if (aPrefectureOrder !== bPrefectureOrder) {
            return aPrefectureOrder - bPrefectureOrder;
          }
        }
        if (b.products.length !== a.products.length) return b.products.length - a.products.length;
        const priceDifference = b.products[0].price_val - a.products[0].price_val;
        if (priceDifference) return priceDifference;
        return a.store_name.localeCompare(b.store_name, "ja");
      });

      if (!stores.length) {
        resultSummary.textContent = "該当掲載店舗: 0店";
        emptyState("条件に一致する公式掲載店舗はありません。");
        return;
      }

      const fragment = document.createDocumentFragment();
      const visibleProductUrls = new Set();
      for (const store of stores) {
        const card = element("article", "card");
        const head = element("div", "card-head");
        const heading = element("h2", "card-title", store.store_name);
        head.appendChild(heading);
        const label = store.prefecture + " / 公式掲載 " + store.products.length + "点";
        head.appendChild(element("span", "badge available", label));
        card.appendChild(head);
        const rows = element("div", "rows");
        for (const product of store.products) {
          visibleProductUrls.add(product.detail_url);
          rows.appendChild(storeProductRow(product));
        }
        card.appendChild(rows);
        fragment.appendChild(card);
      }
      resultSummary.textContent = "該当掲載店舗: " + stores.length + "店 / 商品: " + visibleProductUrls.size + "件";
      results.replaceChildren(fragment);
    }

    function renderProductMode(query, pendingOnly) {
      const filtered = products.filter((product) => {
        if (pendingOnly && !pendingStatuses.has(product.stock_status)) return false;
        return productMatches(product, query, true);
      });
      filtered.sort((a, b) => b.price_val - a.price_val || a.no - b.no);

      if (!filtered.length) {
        resultSummary.textContent = "該当商品: 0件";
        emptyState("条件に一致する商品はありません。");
        return;
      }

      const fragment = document.createDocumentFragment();
      for (const product of filtered) fragment.appendChild(productCard(product));
      const prefix = pendingOnly ? "確認保留・取得失敗" : "該当商品";
      resultSummary.textContent = prefix + ": " + filtered.length + "件";
      results.replaceChildren(fragment);
    }

    function render() {
      const query = normalized(searchInput.value.trim());
      if (currentMode === "stores") renderStores(query);
      else if (currentMode === "pending") renderProductMode(query, true);
      else renderProductMode(query, false);
    }

    function setMode(mode) {
      currentMode = mode;
      for (const tab of tabs) {
        const selected = tab.dataset.mode === mode;
        tab.setAttribute("aria-selected", String(selected));
        tab.tabIndex = selected ? 0 : -1;
      }
      const storeMode = mode === "stores";
      prefectureField.hidden = !storeMode;
      prefectureSelect.disabled = !storeMode;
      if (storeMode) {
        searchLabel.textContent = "商品名・掲載店舗名で検索";
        searchInput.placeholder = "商品名または掲載店舗名";
      } else if (mode === "pending") {
        searchLabel.textContent = "商品名・理由で検索";
        searchInput.placeholder = "商品名または確認理由";
      } else {
        searchLabel.textContent = "商品名・掲載店舗名・理由で検索";
        searchInput.placeholder = "商品名、掲載店舗名、または確認理由";
      }
      results.setAttribute("aria-label", tabs.find((tab) => tab.dataset.mode === mode).textContent);
      render();
    }

    for (const tab of tabs) {
      tab.addEventListener("click", () => setMode(tab.dataset.mode));
      tab.addEventListener("keydown", (event) => {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
        event.preventDefault();
        const offset = event.key === "ArrowRight" ? 1 : -1;
        const nextIndex = (tabs.indexOf(tab) + offset + tabs.length) % tabs.length;
        tabs[nextIndex].focus();
        setMode(tabs[nextIndex].dataset.mode);
      });
    }
    searchInput.addEventListener("input", render);
    prefectureSelect.addEventListener("change", render);
    setMode("stores");
  </script>
</body>
</html>
"""


def build_html(products, generated_at=None):
    """Build a compact app document with a single safely embedded product payload."""
    products = list(products)
    if not products:
        raise MarkdownDataError("cannot build HTML with zero products")

    seen_urls = set()
    for index, product in enumerate(products, 1):
        detail_url = product.get("detail_url", "")
        if not DETAIL_URL_RE.fullmatch(detail_url):
            raise MarkdownDataError(f"product {index} has an invalid detail URL")
        if detail_url in seen_urls:
            raise MarkdownDataError(f"product {index} has a duplicate detail URL")
        seen_urls.add(detail_url)

    generated_at = generated_at or time_str_placeholder()
    pending_count = sum(
        product.get("stock_status") in UNVERIFIED_STATUSES for product in products
    )
    fetch_error_count = sum(
        product.get("stock_status") == "fetch_error" for product in products
    )
    replacements = {
        "__GENERATED_AT__": html.escape(str(generated_at), quote=True),
        "__TOTAL_COUNT__": str(len(products)),
        "__PENDING_COUNT__": str(pending_count),
        "__FETCH_ERROR_COUNT__": str(fetch_error_count),
        "__PREFECTURE_OPTIONS__": pref_options_html(
            PREFECTURES_ORDER,
            selected=DEFAULT_PREFECTURE,
        ),
        "__PRODUCT_DATA__": json_for_html(products),
    }
    document = HTML_TEMPLATE
    for placeholder, value in replacements.items():
        document = document.replace(placeholder, value)
    return document


def write_html_atomic(html_path, document):
    """Durably write a complete HTML document and atomically replace the target."""
    requested_target = Path(html_path).expanduser()
    target_dir = requested_target.parent.resolve()
    if not target_dir.is_dir():
        raise FileNotFoundError(f"output directory does not exist: {target_dir}")
    target = target_dir / requested_target.name

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(target_dir),
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_file.write(document)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_path = Path(temp_file.name)
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, target)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def generate_html(markdown_path=DEFAULT_MARKDOWN_PATH, html_path=DEFAULT_HTML_PATH):
    """Parse Markdown, build HTML, and atomically publish it."""
    products = parse_markdown_file(markdown_path)
    document = build_html(products)
    write_html_atomic(html_path, document)
    return products


def _argument_parser():
    parser = argparse.ArgumentParser(description="Generate the Bookoff stock HTML app")
    parser.add_argument("--input", default=str(DEFAULT_MARKDOWN_PATH), help="input Markdown path")
    parser.add_argument("--output", default=str(DEFAULT_HTML_PATH), help="output HTML path")
    return parser


def main(argv=None):
    args = _argument_parser().parse_args(argv)
    try:
        products = generate_html(args.input, args.output)
    except Exception as exc:
        print(f"[Error] HTML generation failed: {exc}", file=sys.stderr)
        return 1

    print(f"Successfully parsed {len(products)} products.")
    print(f"Interactive HTML successfully generated at: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
