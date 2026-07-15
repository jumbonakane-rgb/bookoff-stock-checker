import json
import random
import re
import time
import os
import sys
import tempfile
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ブックオフオンラインのベースURL
BASE_URL = "https://shopping.bookoff.co.jp"
# DVD・ブルーレイ全体、ソートなし、120件表示、価格帯指定の検索URL
SEARCH_URL_TEMPLATE = "https://shopping.bookoff.co.jp/search/genre/71?per-page=120&p={page}&price={price_range}"

OUTPUT_PATH = Path("/Users/jumbo1/.gemini/antigravity/scratch/bookoff_scraper/high_price_dvd_stock.md")
PRICE_RANGES = [
    "25000-26000",
    "26001-27000",
    "27001-28000",
    "28001-29000",
    "29001-30000",
    "30001-32000",
    "32001-35000",
    "35001-40000",
    "40001-50000",
    "50001-70000",
    "70001-",
]

STATUS_AVAILABLE = "available"
STATUS_NO_STOCK = "no_stock"
STATUS_AGE_VERIFICATION = "age_verification"
STATUS_IDENTITY_MISMATCH = "identity_mismatch"
STATUS_MODAL_INVALID = "modal_invalid"
STATUS_FETCH_ERROR = "fetch_error"

UNVERIFIED_STATUSES = {
    STATUS_AGE_VERIFICATION,
    STATUS_IDENTITY_MISMATCH,
    STATUS_MODAL_INVALID,
}

MIN_ABSOLUTE_PRODUCT_COUNT = 100
MIN_PREVIOUS_COUNT_RATIO = 0.75
MIN_PREVIOUS_RANGE_RATIO = 0.50
MAX_UNVERIFIED_RATIO = 0.05

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
    "Cache-Control": "no-cache, no-store, max-age=0",
    "Pragma": "no-cache",
}

def parse_price(price_str):
    nums = re.findall(r'\d+', price_str.replace(',', ''))
    if nums:
        return int(nums[0])
    return 0

def normalize_text(value):
    normalized = unicodedata.normalize("NFKC", value or "")
    return re.sub(r"\s+", "", normalized).casefold()


def item_id_from_url(value):
    match = re.search(r"/(?:used|new)/(\d+)", value or "")
    return match.group(1) if match else ""


def iter_json_objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_json_objects(child)


def find_product_json_ld(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, ValueError):
            continue

        for item in iter_json_objects(payload):
            item_type = item.get("@type")
            if item_type == "Product" or (isinstance(item_type, list) and "Product" in item_type):
                return item
    return None


def table_value(soup, label):
    wanted = normalize_text(label)
    for th in soup.select("th"):
        if normalize_text(th.get_text(" ", strip=True)) == wanted:
            td = th.find_next_sibling("td")
            return td.get_text(" ", strip=True) if td else ""
    return ""


def stock_result(status, stores=None, reason="", jan=""):
    return {
        "stock_status": status,
        "stores": stores or [],
        "status_reason": reason,
        "jan": jan,
    }


def parse_store_stock_html(html, detail_url, expected_title):
    """商品詳細HTMLを商品ID・商品名・JAN・店舗モーダルの順に照合する。"""
    soup = BeautifulSoup(html, "html.parser")
    target_id = item_id_from_url(detail_url)

    if "/age-verification" in html and "window.location.href" in html:
        return stock_result(
            STATUS_AGE_VERIFICATION,
            reason="年齢確認ページのため対象商品の店舗情報を照合できません",
        )

    product = find_product_json_ld(soup)
    if not product:
        return stock_result(STATUS_IDENTITY_MISMATCH, reason="商品JSON-LDが見つかりません")

    page_title = str(product.get("name") or "").strip()
    product_urls = [str(product.get("@id") or ""), str(product.get("url") or "")]
    canonical_el = soup.select_one('link[rel="canonical"]')
    canonical_url = canonical_el.get("href", "") if canonical_el else ""
    json_jan = str(product.get("gtin13") or "").strip()
    displayed_jan = table_value(soup, "JAN").strip()

    identity_errors = []
    if not target_id:
        identity_errors.append("URLの商品IDを抽出できません")
    if target_id and item_id_from_url(canonical_url) != target_id:
        identity_errors.append("canonical URLの商品IDが一致しません")
    if target_id and not any(item_id_from_url(url) == target_id for url in product_urls):
        identity_errors.append("JSON-LDの商品IDが一致しません")
    if expected_title and normalize_text(page_title) != normalize_text(expected_title):
        identity_errors.append("検索結果と商品ページの商品名が一致しません")
    if not json_jan or not displayed_jan:
        identity_errors.append("JANを二重確認できません")
    elif json_jan != displayed_jan:
        identity_errors.append("JSON-LDと表示欄のJANが一致しません")

    if identity_errors:
        return stock_result(
            STATUS_IDENTITY_MISMATCH,
            reason=" / ".join(identity_errors),
            jan=json_jan or displayed_jan,
        )

    matching_modals = []
    for modal in soup.find_all(id="modalStoreInformation"):
        title_el = modal.select_one(".modalStoreInformation__title")
        modal_title = title_el.get_text(" ", strip=True) if title_el else ""
        if normalize_text(modal_title) == normalize_text(page_title):
            matching_modals.append(modal)

    if len(matching_modals) != 1:
        return stock_result(
            STATUS_MODAL_INVALID,
            reason=f"対象商品と一致する店舗情報欄が{len(matching_modals)}件です",
            jan=json_jan,
        )

    modal = matching_modals[0]
    all_stores = []
    for link in modal.select("a.modalStoreInformation__link"):
        address_el = link.select_one("small.modalStoreInformation__address")
        address = address_el.get_text(" ", strip=True) if address_el else ""
        direct_text = " ".join(
            str(node).strip()
            for node in link.find_all(string=True, recursive=False)
            if str(node).strip()
        )
        store_name = re.sub(r"^(?:ブックオフ|BOOK\s*OFF)\s+", "", direct_text, flags=re.IGNORECASE).strip()
        store_name = re.sub(r"\s+", " ", store_name)
        if store_name:
            all_stores.append((store_name, address))

    unique_stores = sorted(set(all_stores), key=lambda value: (value[1], value[0]))
    heading_el = modal.select_one(".modalStoreInformation__heading")
    heading = heading_el.get_text(" ", strip=True) if heading_el else ""
    count_match = re.search(r"([\d,]+)店", heading)
    if not count_match:
        return stock_result(
            STATUS_MODAL_INVALID,
            reason="店舗情報欄の店舗数を確認できません",
            jan=json_jan,
        )

    declared_count = int(count_match.group(1).replace(",", ""))
    if declared_count != len(unique_stores):
        return stock_result(
            STATUS_MODAL_INVALID,
            reason=f"店舗数表記{declared_count}件と店舗一覧{len(unique_stores)}件が一致しません",
            jan=json_jan,
        )

    status = STATUS_AVAILABLE if unique_stores else STATUS_NO_STOCK
    return stock_result(status, stores=unique_stores, jan=json_jan)


def fetch_store_stock(detail_url, expected_title, max_retries=4):
    """詳細ページを取得し、照合済みの店舗情報と状態を返す。"""
    if not detail_url:
        return stock_result(STATUS_IDENTITY_MISMATCH, reason="商品URLがありません")

    last_parsed_result = None
    last_error = ""
    for attempt in range(1, max_retries + 1):
        try:
            time.sleep(random.uniform(0.3, 1.0))
            response = requests.get(detail_url, headers=HEADERS, timeout=20)

            if response.status_code == 200:
                parsed = parse_store_stock_html(response.text, detail_url, expected_title)
                if parsed["stock_status"] in {
                    STATUS_AVAILABLE,
                    STATUS_NO_STOCK,
                    STATUS_AGE_VERIFICATION,
                }:
                    return parsed

                last_parsed_result = parsed
                if attempt < max_retries:
                    wait_time = attempt * 2.0
                    print(
                        f"  [Retry Warning] Validation failed for {detail_url}: "
                        f"{parsed['status_reason']}. Attempt {attempt}/{max_retries}. "
                        f"Sleeping {wait_time}s..."
                    )
                    time.sleep(wait_time)
                continue

            last_error = f"HTTP {response.status_code}"
            wait_time = attempt * (4.0 if response.status_code in [429, 503] else 2.0)
            print(
                f"  [Retry Warning] Status {response.status_code} for {detail_url}. "
                f"Attempt {attempt}/{max_retries}. Sleeping {wait_time}s..."
            )
            time.sleep(wait_time)
        except Exception as exc:
            last_error = str(exc)
            wait_time = attempt * 3.0
            print(
                f"  [Retry Warning] Connection Error ({exc}) for {detail_url}. "
                f"Attempt {attempt}/{max_retries}. Retrying in {wait_time}s..."
            )
            time.sleep(wait_time)

    if last_parsed_result:
        return last_parsed_result

    print(f"  [ERROR] Failed to fetch stock for {detail_url} after {max_retries} attempts.")
    return stock_result(STATUS_FETCH_ERROR, reason=last_error or "取得に失敗しました")


def parse_search_products(html):
    soup = BeautifulSoup(html, "html.parser")
    items = soup.find_all(
        class_=lambda classes: classes
        and "productItem" in classes.split()
        and "js-hoverItem" in classes.split()
    )
    products = []
    for item in items:
        title_el = item.find(class_="productItem__title")
        price_el = item.find(class_="productItem__price")
        title = title_el.get_text(" ", strip=True) if title_el else "不明なタイトル"
        price_str = price_el.get_text(" ", strip=True) if price_el else "¥0"
        price_val = parse_price(price_str)

        relative_url = ""
        for link in item.find_all("a", href=True):
            if re.match(r"^/(?:used|new)/\d+", link["href"]):
                relative_url = link["href"]
                break

        if price_val >= 25000 and relative_url:
            products.append({
                "title": title,
                "price_str": price_str,
                "price_val": price_val,
                "detail_url": BASE_URL + relative_url,
            })
    return products, len(items)


def fetch_search_page_with_retry(page, price_range, max_retries=5):
    search_url = SEARCH_URL_TEMPLATE.format(page=page, price_range=price_range)
    saw_empty_page = False

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(search_url, headers=HEADERS, timeout=30)
            if response.status_code == 200:
                products, raw_item_count = parse_search_products(response.text)
                if raw_item_count > 0:
                    return {
                        "status": "ok",
                        "products": products,
                        "raw_item_count": raw_item_count,
                    }

                saw_empty_page = True
                wait_time = attempt * 2.0
                print(
                    f"  [Retry Warning] HTTP 200 but no product cards for range "
                    f"{price_range} page {page}. Attempt {attempt}/{max_retries}. "
                    f"Sleeping {wait_time}s..."
                )
                time.sleep(wait_time)
                continue

            wait_time = attempt * (5.0 if response.status_code in [429, 503] else 3.0)
            print(
                f"  [Retry Warning] Status {response.status_code} for range "
                f"{price_range} page {page}. Attempt {attempt}/{max_retries}. "
                f"Sleeping {wait_time}s..."
            )
            time.sleep(wait_time)
        except Exception as exc:
            wait_time = attempt * 5.0
            print(
                f"  [Retry Warning] Connection Error ({exc}) for range {price_range} "
                f"page {page}. Attempt {attempt}/{max_retries}. Retrying in {wait_time}s..."
            )
            time.sleep(wait_time)

    if saw_empty_page and page > 1:
        return {"status": "confirmed_empty", "products": [], "raw_item_count": 0}
    return {"status": "error", "products": [], "raw_item_count": 0}


def price_range_for_value(price):
    for price_range in PRICE_RANGES:
        lower_text, upper_text = price_range.split("-", 1)
        lower = int(lower_text)
        upper = int(upper_text) if upper_text else None
        if price >= lower and (upper is None or price <= upper):
            return price_range
    return ""


def load_previous_snapshot_stats(path=OUTPUT_PATH):
    if not path.exists():
        return 0, Counter()

    count = 0
    range_counts = Counter()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or "商品タイトル" in line or ":---" in line:
            continue
        columns = [column.strip() for column in line.split("|")]
        if len(columns) < 6:
            continue
        count += 1
        price_range = price_range_for_value(parse_price(columns[3]))
        if price_range:
            range_counts[price_range] += 1
    return count, range_counts


def validate_collection(products, previous_count, previous_range_counts):
    errors = []
    total = len(products)
    if total < MIN_ABSOLUTE_PRODUCT_COUNT:
        errors.append(f"商品数が安全下限を下回りました: {total}件")

    if previous_count and total < previous_count * MIN_PREVIOUS_COUNT_RATIO:
        errors.append(f"商品数が前回{previous_count}件から{total}件へ急減しました")

    current_range_counts = Counter(
        price_range_for_value(product["price_val"])
        for product in products
        if price_range_for_value(product["price_val"])
    )
    for price_range, previous in previous_range_counts.items():
        current = current_range_counts[price_range]
        if previous >= 10 and current < previous * MIN_PREVIOUS_RANGE_RATIO:
            errors.append(
                f"価格帯{price_range}の商品数が前回{previous}件から{current}件へ急減しました"
            )
    return errors

def main():
    print("=" * 60)
    print(" 高精度スクレイピング (商品ID・商品名・JAN・店舗情報照合付)")
    print(" 条件: 価格が25,000円以上")
    print("=" * 60)

    collected_dict = {}
    search_failures = []

    print("[Step 1] Listing all products above 25,000 yen...")
    for price_range in PRICE_RANGES:
        page = 1
        while True:
            print(f"  Fetching price range {price_range}, page {page}...")
            page_result = fetch_search_page_with_retry(page, price_range)

            if page_result["status"] == "error":
                search_failures.append(f"価格帯{price_range} page {page}")
                break
            if page_result["status"] == "confirmed_empty":
                print(f"    Confirmed end of range {price_range} at page {page}.")
                break

            items_added = 0
            for product in page_result["products"]:
                detail_url = product["detail_url"]
                if detail_url not in collected_dict:
                    product["stores"] = []
                    product["stock_status"] = "pending"
                    product["source_range"] = price_range
                    collected_dict[detail_url] = product
                    items_added += 1

            raw_item_count = page_result["raw_item_count"]
            print(
                f"    Found {raw_item_count} cards on range {price_range} page {page}. "
                f"(Added {items_added} new items)"
            )
            if raw_item_count < 120:
                break

            page += 1
            time.sleep(1.0)

    if search_failures:
        print("\n[SAFETY STOP] Search results were incomplete. Existing data was not changed.")
        for failure in search_failures:
            print(f"  - {failure}")
        return 1

    collected_products = list(collected_dict.values())
    collected_products.sort(key=lambda product: product["price_val"], reverse=True)
    previous_count, previous_range_counts = load_previous_snapshot_stats()
    collection_errors = validate_collection(
        collected_products,
        previous_count,
        previous_range_counts,
    )
    if collection_errors:
        print("\n[SAFETY STOP] Product-list validation failed. Existing data was not changed.")
        for error in collection_errors:
            print(f"  - {error}")
        return 1

    total_products = len(collected_products)
    print(f"\n[Step 1 Completed] Total products to verify: {total_products} items.")
    print("\n[Step 2] Fetching and validating store stock (Max 5 threads)...")

    max_workers = 5
    completed_count = 0
    start_time = time.time()

    def process_item(item_index, product):
        result = fetch_store_stock(product["detail_url"], product["title"])
        return item_index, result

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(process_item, index, product): index
            for index, product in enumerate(collected_products)
        }

        for future in as_completed(futures):
            index, result = future.result()
            collected_products[index].update(result)
            completed_count += 1

            if completed_count % 20 == 0 or completed_count == total_products:
                elapsed = time.time() - start_time
                speed = completed_count / elapsed if elapsed > 0 else 0
                eta = (total_products - completed_count) / speed if speed > 0 else 0
                print(
                    f"  Progress: {completed_count}/{total_products} "
                    f"({completed_count / total_products * 100:.1f}%) | "
                    f"Speed: {speed:.1f} items/s | ETA: {eta:.0f}s"
                )

    elapsed = time.time() - start_time
    status_counts = Counter(product["stock_status"] for product in collected_products)
    print(f"\n[Step 2 Completed] All details checked in {elapsed:.1f} seconds.")
    print(f"  Status summary: {dict(status_counts)}")

    failed_indexes = [
        index
        for index, product in enumerate(collected_products)
        if product["stock_status"] == STATUS_FETCH_ERROR
    ]
    if failed_indexes:
        print(
            f"\n[Step 2 Retry] Retrying {len(failed_indexes)} failed detail pages "
            "sequentially with a slower retry policy..."
        )
        time.sleep(5.0)
        for retry_number, index in enumerate(failed_indexes, 1):
            product = collected_products[index]
            result = fetch_store_stock(
                product["detail_url"],
                product["title"],
                max_retries=6,
            )
            product.update(result)
            print(
                f"  Retry progress: {retry_number}/{len(failed_indexes)} "
                f"-> {result['stock_status']}"
            )
            time.sleep(1.0)

        status_counts = Counter(product["stock_status"] for product in collected_products)
        print(f"  Status summary after retry: {dict(status_counts)}")

    fetch_error_count = status_counts[STATUS_FETCH_ERROR]
    if fetch_error_count:
        print(
            f"\n[SAFETY STOP] {fetch_error_count} detail pages could not be fetched. "
            "Existing data was not changed."
        )
        return 1

    unverified_count = sum(status_counts[status] for status in UNVERIFIED_STATUSES)
    if total_products and unverified_count / total_products > MAX_UNVERIFIED_RATIO:
        print(
            f"\n[SAFETY STOP] Unverified products exceeded the safety limit: "
            f"{unverified_count}/{total_products}. Existing data was not changed."
        )
        return 1

    output_md(collected_products)
    print("Execution Finished Successfully!")
    return 0


def stock_cell_text(product):
    status = product.get("stock_status")
    stores = product.get("stores", [])

    if status == STATUS_NO_STOCK:
        return "在庫なし (入荷店舗: 0店)"
    if status == STATUS_AGE_VERIFICATION:
        return "確認保留 (年齢確認ページ・商品照合不可)"
    if status == STATUS_IDENTITY_MISMATCH:
        return "確認保留 (商品ID・商品名・JANの照合不一致)"
    if status == STATUS_MODAL_INVALID:
        return "確認保留 (店舗情報欄の照合不一致)"
    if status == STATUS_FETCH_ERROR:
        return "取得失敗 (通信エラー)"

    stock_parts = []
    for name, address in stores:
        prefecture = ""
        match = re.match(r"^(北海道|東京都|大阪府|京都府|.+?県)", address)
        if match:
            prefecture = f" ({match.group(1)})"
        stock_parts.append(f"{name}{prefecture}")
    return f"入荷店舗: {len(stores)}店<br>" + "<br>".join(stock_parts)


def render_markdown(products):
    current_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    lines = [
        "# アニメDVD・Blu-ray 高額商品在庫リスト（25,000円以上）\n\n",
        f"データ取得日時: {current_time} (JST)  \n",
        f"対象件数: {len(products)} 件\n\n",
        "| No | 商品タイトル | 価格 (中古) | 店舗在庫状況 | 詳細リンク |\n",
        "| :--- | :--- | :--- | :--- | :--- |\n",
    ]

    for index, product in enumerate(products, 1):
        title = product["title"].replace("|", "\\|")
        stock_text = stock_cell_text(product)
        lines.append(
            f"| {index} | {title} | {product['price_str']} | {stock_text} | "
            f"[詳細ページ]({product['detail_url']}) |\n"
        )
    return "".join(lines)


def output_md(products, output_path=OUTPUT_PATH):
    print(f"\n[Markdown] Generating output markdown at: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(output_path.parent),
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_file.write(render_markdown(products))
            temp_path = Path(temp_file.name)
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, output_path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()

    print("[Markdown] Completed atomically.")
    print("=" * 60)


if __name__ == "__main__":
    sys.exit(main())
