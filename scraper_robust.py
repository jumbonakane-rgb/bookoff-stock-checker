import requests
from bs4 import BeautifulSoup
import re
import time
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

# ブックオフオンラインのベースURL
BASE_URL = "https://shopping.bookoff.co.jp"
# アニメジャンル、高い順、120件表示の検索URL
SEARCH_URL_TEMPLATE = "https://shopping.bookoff.co.jp/search/genre/71?sort=51&per-page=120&p={page}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def parse_price(price_str):
    nums = re.findall(r'\d+', price_str.replace(',', ''))
    if nums:
        return int(nums[0])
    return 0

def fetch_store_stock(detail_url, max_retries=3):
    """
    商品の詳細ページから、在庫のある店舗の一覧を取得する。
    1. 商品自身のモーダル (id="modalStoreInformation"の最初の要素) のみを対象にする (ダミー除去)。
    2. サーバー側の一時規制・タイムアウト等に対処するため、最大3回のリトライ処理を行う。
    """
    if not detail_url:
        return []
        
    for attempt in range(1, max_retries + 1):
        try:
            # スレッドごとの実行タイミングを分散し、アクセス規制を防止
            import random
            time.sleep(random.uniform(0.3, 1.0))
            
            response = requests.get(detail_url, headers=HEADERS, timeout=15)
            
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                modal = soup.find(id="modalStoreInformation")
                
                all_stores = []
                if modal:
                    s_lists = modal.find_all(class_="modalStoreInformation__list")
                    for s_list in s_lists:
                        items = s_list.find_all(class_="modalStoreInformation__item")
                        for item in items:
                            link = item.find("a", class_="modalStoreInformation__link")
                            if link:
                                address_el = link.find("small", class_="modalStoreInformation__address")
                                address = address_el.text.strip() if address_el else ""
                                full_text = link.text.strip()
                                store_name = full_text.replace(address, "").strip()
                                store_name = re.sub(r'\s+', ' ', store_name)
                                if store_name:
                                    all_stores.append((store_name, address))
                                    
                unique_stores = sorted(list(set(all_stores)), key=lambda x: (x[1], x[0]))
                return unique_stores
                
            elif response.status_code in [429, 503]:
                # アクセス制限や過負荷時は徐々にスリープ時間を増やしてリトライ
                wait_time = attempt * 4.0
                print(f"  [Retry Warning] Status {response.status_code} for {detail_url}. Attempt {attempt}/{max_retries}. Sleeping {wait_time}s...")
                time.sleep(wait_time)
            else:
                # その他の一時的エラー
                print(f"  [Retry Warning] Status {response.status_code} for {detail_url}. Attempt {attempt}/{max_retries}. Retrying in 2s...")
                time.sleep(2.0)
                
        except Exception as e:
            wait_time = attempt * 3.0
            print(f"  [Retry Warning] Connection Error ({e}) for {detail_url}. Attempt {attempt}/{max_retries}. Retrying in {wait_time}s...")
            time.sleep(wait_time)
            
    print(f"  [ERROR] Failed to fetch stock for {detail_url} after {max_retries} attempts.")
    return []

def main():
    print("="*60)
    print(" ブックオフ堅牢スクレイピング (リトライ機能 ＆ 流量制御付)")
    print(" 条件: 価格が25,000円以上（25,000円以下に下がるまで全件取得）")
    print("="*60)
    
    collected_products = []
    page = 1
    stop_scraping = False
    
    # 1. 検索結果ページから商品を同期的にリストアップ
    print("[Step 1] listing all products above 25,000 yen...")
    while not stop_scraping:
        search_url = SEARCH_URL_TEMPLATE.format(page=page)
        print(f"  Fetching search page {page}: {search_url}")
        
        try:
            response = requests.get(search_url, headers=HEADERS, timeout=15)
            if response.status_code != 200:
                print(f"  [Error] Failed to fetch page {page}")
                break
                
            soup = BeautifulSoup(response.text, "html.parser")
            items = soup.find_all(class_=lambda c: c and 'productItem' in c.split() and 'js-hoverItem' in c.split())
            
            if not items:
                print("  No more items found.")
                break
                
            print(f"  Found {len(items)} items on page {page}.")
            
            for idx, item in enumerate(items):
                title_el = item.find(class_="productItem__title")
                title = title_el.text.strip() if title_el else "不明なタイトル"
                
                price_el = item.find(class_="productItem__price")
                price_str = price_el.text.strip() if price_el else "¥0"
                price_val = parse_price(price_str)
                
                link_el = item.find("a", href=True)
                relative_url = link_el["href"] if link_el else ""
                if not relative_url.startswith("/used/") and not relative_url.startswith("/new/"):
                    title_link = title_el.find("a", href=True) if title_el else None
                    if title_link:
                        relative_url = title_link["href"]
                
                detail_url = BASE_URL + relative_url if relative_url else ""
                
                if price_val < 25000:
                    print(f"  [Stop Condition] Price {price_val}円 is under 25,000円. Stop listing.")
                    stop_scraping = True
                    break
                
                collected_products.append({
                    "title": title,
                    "price_str": price_str,
                    "price_val": price_val,
                    "detail_url": detail_url,
                    "stores": []
                })
                
            if stop_scraping:
                break
                
            page += 1
            time.sleep(1.0)
            
        except Exception as e:
            print(f"  [Error] Exception on page {page}: {e}")
            break
            
    total_products = len(collected_products)
    print(f"\n[Step 1 Completed] Total products to fetch: {total_products} items.")
    
    # 2. ThreadPoolExecutorによる流量制御並行スクレイピング
    # 同時スレッド数を 5 に減らしてアクセス頻度をマイルドに保ち、規制を回避
    print("\n[Step 2] Fetching store stock via multi-threading (Max 5 threads for high safety)...")
    
    max_workers = 5
    completed_count = 0
    start_time = time.time()
    
    def process_item(item_idx, prod):
        stores = fetch_store_stock(prod["detail_url"])
        return item_idx, stores

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_item, i, prod): i for i, prod in enumerate(collected_products)}
        
        for future in as_completed(futures):
            idx, stores = future.result()
            collected_products[idx]["stores"] = stores
            completed_count += 1
            
            if completed_count % 20 == 0 or completed_count == total_products:
                elapsed = time.time() - start_time
                speed = completed_count / elapsed if elapsed > 0 else 0
                eta = (total_products - completed_count) / speed if speed > 0 else 0
                print(f"  Progress: {completed_count}/{total_products} ({completed_count/total_products*100:.1f}%) | Speed: {speed:.1f} items/s | ETA: {eta:.0f}s")
                
    print(f"\n[Step 2 Completed] All details fetched in {time.time() - start_time:.1f} seconds.")
    
    # Markdownファイルの書き出し
    output_md(collected_products)
    print("Execution Finished Successfully!")

def output_md(products):
    output_path = "/Users/jumbo1/.gemini/antigravity/scratch/bookoff_scraper/high_price_dvd_stock.md"
    print(f"\n[Markdown] Generating output markdown at: {output_path}")
    
    current_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("# ブックオフオンライン アニメDVD・Blu-ray 高額商品在庫リスト（25,000円以上）\n\n")
        f.write(f"データ取得日時: {current_time} (JST)  \n")
        f.write(f"対象件数: {len(products)} 件\n\n")
        
        f.write("| No | 商品タイトル | 価格 (中古) | 店舗在庫状況 | 詳細リンク |\n")
        f.write("| :--- | :--- | :--- | :--- | :--- |\n")
        
        for i, p in enumerate(products, 1):
            title = p["title"]
            price = p["price_str"]
            url = p["detail_url"]
            stores = p["stores"]
            
            if not stores:
                stock_str = "在庫なし (入荷店舗: 0店)"
            else:
                stock_parts = []
                for name, addr in stores:
                    pref = ""
                    m = re.match(r'^(北海道|東京都|大阪府|京都府|.+?県)', addr)
                    if m:
                        pref = f" ({m.group(1)})"
                    stock_parts.append(f"{name}{pref}")
                stock_str = f"入荷店舗: {len(stores)}店<br>" + "<br>".join(stock_parts)
            
            title_escaped = title.replace('|', '\\|')
            f.write(f"| {i} | {title_escaped} | {price} | {stock_str} | [詳細ページ]({url}) |\n")
            
    print("[Markdown] Completed!")
    print("="*60)

if __name__ == "__main__":
    main()
