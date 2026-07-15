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

from markdown_table import escape_markdown_cell, split_markdown_row

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
MIN_PREVIOUS_COUNT_RATIO = 0.90
MIN_PREVIOUS_RANGE_RATIO = 0.80
MAX_UNVERIFIED_RATIO = 0.05

STORE_DETAIL_URL_PATTERN = re.compile(
    r"^https://www\.bookoff\.co\.jp/shop/shop\d+\.html(?:[?#].*)?$"
)
SEARCH_EMPTY_MESSAGE = "ご指定いただいた検索条件に該当する商品が見つかりませんでした"

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

    if re.search(r"(?:https?://shopping\.bookoff\.co\.jp)?/age-verification(?:[?'\"/]|$)", html):
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
    store_rows = modal.select("li.modalStoreInformation__item")
    store_links = modal.select("a.modalStoreInformation__link")
    if len(store_rows) != len(store_links):
        return stock_result(
            STATUS_MODAL_INVALID,
            reason=f"店舗行{len(store_rows)}件と店舗リンク{len(store_links)}件が一致しません",
            jan=json_jan,
        )

    all_stores = []
    invalid_store_urls = []
    for link in store_links:
        store_url = str(link.get("href") or "").strip()
        if not STORE_DETAIL_URL_PATTERN.fullmatch(store_url):
            invalid_store_urls.append(store_url or "(空欄)")

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

    if invalid_store_urls:
        return stock_result(
            STATUS_MODAL_INVALID,
            reason=f"正規の店舗詳細URLでない店舗行が{len(invalid_store_urls)}件あります",
            jan=json_jan,
        )

    if len(all_stores) != len(store_links):
        return stock_result(
            STATUS_MODAL_INVALID,
            reason=f"店舗リンク{len(store_links)}件のうち店名を確認できたのは{len(all_stores)}件です",
            jan=json_jan,
        )

    unique_stores = sorted(set(all_stores), key=lambda value: (value[1], value[0]))
    if len(unique_stores) != len(all_stores):
        return stock_result(
            STATUS_MODAL_INVALID,
            reason=f"店舗一覧{len(all_stores)}件に重複行があります",
            jan=json_jan,
        )
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
    if declared_count != len(all_stores):
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
    parse_errors = []
    seen_urls = set()
    for item_number, item in enumerate(items, 1):
        title_el = item.find(class_="productItem__title")
        price_el = item.find(class_="productItem__price")
        title = title_el.get_text(" ", strip=True) if title_el else ""
        price_str = price_el.get_text(" ", strip=True) if price_el else ""
        price_val = parse_price(price_str)

        relative_urls = set()
        for link in item.find_all("a", href=True):
            if re.match(r"^/(?:used|new)/\d+", link["href"]):
                relative_urls.add(link["href"].split("?", 1)[0].split("#", 1)[0])

        item_errors = []
        if not title:
            item_errors.append("商品名なし")
        if price_val < 25000:
            item_errors.append(f"価格不正({price_str or '空欄'})")
        if len(relative_urls) != 1:
            item_errors.append(f"商品URLが{len(relative_urls)}件")

        if item_errors:
            parse_errors.append(f"card {item_number}: {' / '.join(item_errors)}")
            continue

        detail_url = BASE_URL + next(iter(relative_urls))
        if detail_url in seen_urls:
            parse_errors.append(f"card {item_number}: 商品URL重複({detail_url})")
            continue
        seen_urls.add(detail_url)
        products.append({
            "title": title,
            "price_str": price_str,
            "price_val": price_val,
            "detail_url": detail_url,
        })

    summary_el = soup.select_one(".productSearch__num")
    summary_text = summary_el.get_text(" ", strip=True) if summary_el else ""
    total_match = re.search(r"全\s*([\d,]+)\s*件", summary_text)
    total_count = int(total_match.group(1).replace(",", "")) if total_match else None
    confirmed_empty = SEARCH_EMPTY_MESSAGE in soup.get_text(" ", strip=True)
    return {
        "products": products,
        "raw_item_count": len(items),
        "parse_errors": parse_errors,
        "total_count": total_count,
        "confirmed_empty": confirmed_empty,
    }


def fetch_search_page_with_retry(page, price_range, max_retries=5):
    search_url = SEARCH_URL_TEMPLATE.format(page=page, price_range=price_range)

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(search_url, headers=HEADERS, timeout=30)
            if response.status_code == 200:
                parsed = parse_search_products(response.text)
                if parsed["raw_item_count"] > 0 and not parsed["parse_errors"] and parsed["total_count"] is not None:
                    return {
                        "status": "ok",
                        **parsed,
                    }

                if parsed["confirmed_empty"] and parsed["raw_item_count"] == 0:
                    return {"status": "confirmed_empty", **parsed}

                wait_time = attempt * 2.0
                details = []
                if parsed["parse_errors"]:
                    details.append("; ".join(parsed["parse_errors"][:3]))
                if parsed["total_count"] is None:
                    details.append("検索総件数なし")
                detail_text = f" ({' / '.join(details)})" if details else ""
                print(
                    f"  [Retry Warning] HTTP 200 but search page validation failed for range "
                    f"{price_range} page {page}{detail_text}. Attempt {attempt}/{max_retries}. "
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

    return {
        "status": "error",
        "products": [],
        "raw_item_count": 0,
        "parse_errors": [],
        "total_count": None,
        "confirmed_empty": False,
    }


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
        try:
            columns = split_markdown_row(line)
        except ValueError:
            continue
        if len(columns) != 5:
            continue
        count += 1
        price_range = price_range_for_value(parse_price(columns[2]))
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
        expected_total = None
        range_urls = set()
        while True:
            print(f"  Fetching price range {price_range}, page {page}...")
            page_result = fetch_search_page_with_retry(page, price_range)

            if page_result["status"] == "error":
                search_failures.append(f"価格帯{price_range} page {page}")
                break
            if page_result["status"] == "confirmed_empty":
                if page != 1:
                    search_failures.append(
                        f"価格帯{price_range} page {page}が総件数を満たす前に空ページになりました"
                    )
                else:
                    expected_total = 0
                    print(f"    Confirmed 0 products in range {price_range}.")
                break

            page_total = page_result["total_count"]
            if expected_total is None:
                expected_total = page_total
            elif page_total != expected_total:
                search_failures.append(
                    f"価格帯{price_range}の総件数が取得中に{expected_total}件から{page_total}件へ変化しました"
                )
                break

            expected_page_count = min(120, max(expected_total - ((page - 1) * 120), 0))
            raw_item_count = page_result["raw_item_count"]
            if raw_item_count != expected_page_count:
                search_failures.append(
                    f"価格帯{price_range} page {page}は{expected_page_count}件の想定に対して"
                    f"{raw_item_count}件でした"
                )
                break

            items_added = 0
            for product in page_result["products"]:
                detail_url = product["detail_url"]
                if detail_url in range_urls:
                    search_failures.append(
                        f"価格帯{price_range}で商品URLがページ間重複しました: {detail_url}"
                    )
                    break
                range_urls.add(detail_url)
                if detail_url in collected_dict:
                    search_failures.append(
                        f"商品URLが複数の価格帯に重複しました: {detail_url}"
                    )
                    break
                product["stores"] = []
                product["stock_status"] = "pending"
                product["source_range"] = price_range
                collected_dict[detail_url] = product
                items_added += 1

            if search_failures:
                break

            print(
                f"    Verified {raw_item_count}/{expected_total} cards on range {price_range} page {page}. "
                f"(Added {items_added} new items)"
            )
            if len(range_urls) == expected_total:
                break
            if len(range_urls) > expected_total:
                search_failures.append(
                    f"価格帯{price_range}の取得件数が総件数{expected_total}件を超えました"
                )
                break

            page += 1
            time.sleep(1.0)

        if search_failures:
            break

        if expected_total is None or len(range_urls) != expected_total:
            search_failures.append(
                f"価格帯{price_range}は総件数{expected_total}件に対して{len(range_urls)}件しか照合できませんでした"
            )
            break

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
    if status in UNVERIFIED_STATUSES:
        reason = product.get("status_reason") or "商品ページを安全に照合できません"
        return f"確認保留 [{status}]: {reason}"
    if status == STATUS_FETCH_ERROR:
        reason = product.get("status_reason") or "通信エラー"
        return f"取得失敗 [{status}]: {reason}"

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
        title = escape_markdown_cell(product["title"])
        price_text = escape_markdown_cell(product["price_str"])
        stock_text = escape_markdown_cell(stock_cell_text(product))
        lines.append(
            f"| {index} | {title} | {price_text} | {stock_text} | "
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
