import re
import os

# 都道府県の標準的な並び順
PREFECTURES_ORDER = [
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県",
    "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県",
    "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県",
    "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県"
]

def main():
    input_path = "/Users/jumbo1/.gemini/antigravity/scratch/bookoff_scraper/high_price_dvd_stock.md"
    output_path = "/Users/jumbo1/.gemini/antigravity/scratch/bookoff_scraper/high_price_dvd_stock_by_prefecture.md"
    
    if not os.path.exists(input_path):
        print(f"[Error] Input file not found: {input_path}")
        return
        
    print(f"Reading generated Markdown: {input_path}")
    
    # 都道府県ごとの店舗辞書を初期化
    by_pref = {pref: {} for pref in PREFECTURES_ORDER}
    by_pref["その他・不明"] = {}
    
    with open(input_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
        
    data_lines_count = 0
    
    for line in lines:
        line = line.strip()
        if not line.startswith("|") or "商品タイトル" in line or ":---" in line:
            continue
            
        # パイプで分割
        # 例: | No | 商品タイトル | 価格 (中古) | 店舗在庫状況 | 詳細リンク |
        cols = [c.strip() for c in line.split("|")]
        if len(cols) < 6:
            continue
            
        no = cols[1]
        title = cols[2]
        price = cols[3]
        stock_str = cols[4]
        link = cols[5]
        
        data_lines_count += 1
        
        # 在庫状況を解析
        if not stock_str.startswith("入荷店舗:"):
            continue
            
        # <br> で店舗ごとに分割
        parts = stock_str.split("<br>")
        
        for part in parts:
            part = part.strip()
            if "入荷店舗:" in part or not part:
                continue
                
            # 店舗名と都道府県の抽出
            # 例: BOOKOFF 札幌南2条店 (北海道)
            m = re.search(r'\((.+?)\)$', part)
            if m:
                pref_name = m.group(1).strip()
                # 都道府県の表記揺れ吸収
                matched_pref = None
                for p in PREFECTURES_ORDER:
                    if p.startswith(pref_name) or pref_name.startswith(p):
                        matched_pref = p
                        break
                        
                store_clean = part.replace(f" ({m.group(1)})", "").strip()
                store_clean = re.sub(r'\s+', ' ', store_clean)
                
                if matched_pref:
                    if store_clean not in by_pref[matched_pref]:
                        by_pref[matched_pref][store_clean] = []
                    by_pref[matched_pref][store_clean].append({
                        "no": no,
                        "title": title,
                        "price": price,
                        "link": link
                    })
                else:
                    if store_clean not in by_pref["その他・不明"]:
                        by_pref["その他・不明"][store_clean] = []
                    by_pref["その他・不明"][store_clean].append({
                        "no": no,
                        "title": title,
                        "price": price,
                        "link": link
                    })
            else:
                # 括弧がない場合
                store_clean = part
                if store_clean not in by_pref["その他・不明"]:
                    by_pref["その他・不明"][store_clean] = []
                by_pref["その他・不明"][store_clean].append({
                    "no": no,
                    "title": title,
                    "price": price,
                    "link": link
                })
                
    print(f"Parsed {data_lines_count} products.")
    
    # 新しいMarkdownファイルを書き出す
    print(f"Writing sorted by prefecture and store to: {output_path}")
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("# アニメDVD・Blu-ray 都道府県別・店舗別在庫リスト（25,000円以上）\n\n")
        f.write("元の高額商品リストから、在庫が存在する都道府県・店舗ごとに再構成したリストです。（各都道府県内では店舗ごとの在庫数が多い順に並んでいます）\n\n")
        
        # クイックリンク（目次）
        f.write("### 都道府県クイックリンク\n")
        active_prefs = [p for p in PREFECTURES_ORDER if len(by_pref[p]) > 0]
        if len(by_pref["その他・不明"]) > 0:
            active_prefs.append("その他・不明")
            
        links_str = " | ".join([f"[{p}](#{p})" for p in active_prefs])
        f.write(links_str + "\n\n---\n\n")
        
        for pref in PREFECTURES_ORDER + ["その他・不明"]:
            stores_dict = by_pref[pref]
            if not stores_dict:
                continue
                
            f.write(f"## {pref}\n")
            
            # 総在庫数と店舗数
            total_items = sum(len(prods) for prods in stores_dict.values())
            f.write(f"総在庫数: **{total_items}** 件 / 稼働店舗数: **{len(stores_dict)}** 店\n\n")
            
            # 店舗を在庫数の多い順（同数なら店名順）にソート
            sorted_stores = sorted(
                stores_dict.items(),
                key=lambda x: (-len(x[1]), x[0])
            )
            
            for store_name, products in sorted_stores:
                f.write(f"### 🏪 {store_name} （在庫数: {len(products)} 点）\n\n")
                
                f.write("| No | 元リストNo | 商品タイトル | 価格 (中古) | 詳細リンク |\n")
                f.write("| :--- | :--- | :--- | :--- | :--- |\n")
                
                for idx, prod in enumerate(products, 1):
                    no = prod["no"]
                    title = prod["title"]
                    price = prod["price"]
                    link = prod["link"]
                    
                    f.write(f"| {idx} | {no} | {title} | {price} | {link} |\n")
                f.write("\n")
                
            f.write("\n---\n\n")
            
    print("Conversion complete!")

if __name__ == "__main__":
    main()
