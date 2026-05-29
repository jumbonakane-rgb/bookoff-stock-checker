import re
import os
import json

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

def parse_price_val(price_str):
    """
    価格文字列から数値のみを抽出
    """
    nums = re.findall(r'\d+', price_str.replace(',', ''))
    if nums:
        return int(nums[0])
    return 0

def main():
    md_path = "/Users/jumbo1/.gemini/antigravity/scratch/bookoff_scraper/high_price_dvd_stock.md"
    html_path = "/Users/jumbo1/.gemini/antigravity/scratch/bookoff_scraper/index.html"
    
    if not os.path.exists(md_path):
        print(f"[Error] Markdown file not found: {md_path}")
        return
        
    print(f"Reading Markdown data from: {md_path}")
    
    products = []
    
    with open(md_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
        
    for line in lines:
        line = line.strip()
        if not line.startswith("|") or "商品タイトル" in line or ":---" in line:
            continue
            
        cols = [c.strip() for c in line.split("|")]
        if len(cols) < 6:
            continue
            
        no = cols[1]
        title = cols[2]
        price_str = cols[3]
        stock_str = cols[4]
        link_match = re.search(r'\((https://.+?)\)', cols[5])
        link = link_match.group(1) if link_match else ""
        
        title = title.replace('\\|', '|')
        
        stores = []
        if "在庫なし" in stock_str or "入荷店舗: 0店" in stock_str:
            pass
        else:
            parts = stock_str.split("<br>")
            for part in parts:
                part = part.strip()
                if "入荷店舗:" in part or not part:
                    continue
                
                m = re.search(r'\(([^()]+?)\)$', part)
                if m:
                    pref_candidate = m.group(1).strip()
                    matched_pref = None
                    for p in PREFECTURES_ORDER:
                        if p.startswith(pref_candidate) or pref_candidate.startswith(p):
                            matched_pref = p
                            break
                    
                    if matched_pref:
                        store_name = part.replace(f" ({m.group(1)})", "").strip()
                        store_name = re.sub(r'\s+', ' ', store_name)
                        stores.append({
                            "store_name": store_name,
                            "prefecture": matched_pref
                        })
                    else:
                        stores.append({
                            "store_name": part,
                            "prefecture": "不明"
                        })
                else:
                    stores.append({
                        "store_name": part,
                        "prefecture": "不明"
                    })
                
        price_val = parse_price_val(price_str)
        
        products.append({
            "no": int(no) if no.isdigit() else len(products) + 1,
            "title": title,
            "price_str": price_str,
            "price_val": price_val,
            "detail_url": link,
            "stores": stores
        })
        
    print(f"Successfully parsed {len(products)} products.")
    
    # 日本の標準的な都道府県順（北海道〜沖縄）をそのまま使用する
    sorted_prefs = PREFECTURES_ORDER
    
    # 初期表示として「全国」の店舗データをプリレンダリングする（JavaScript無効/iOSクイックルック対策）
    initial_stores_map = {}
    for p in products:
        for s in p["stores"]:
            store_name = s["store_name"]
            if store_name not in initial_stores_map:
                initial_stores_map[store_name] = {
                    "store_name": store_name,
                    "prefecture": s["prefecture"],
                    "products": []
                }
            # 商品重複防止
            if not any(prod["title"] == p["title"] for prod in initial_stores_map[store_name]["products"]):
                initial_stores_map[store_name]["products"].append({
                    "no": p["no"],
                    "title": p["title"],
                    "price_val": p["price_val"],
                    "price_str": p["price_str"],
                    "detail_url": p["detail_url"]
                })
    
    # データをソート (店舗は全国で在庫数が多い順、店舗内の商品は価格が高い順)
    initial_stores = list(initial_stores_map.values())
    for s in initial_stores:
        s["products"].sort(key=lambda x: x["price_val"], reverse=True)
    initial_stores.sort(key=lambda x: (-len(x["products"]), -x["products"][0]["price_val"] if x["products"] else 0))
    
    # 静的なHTMLパーツを組み立て
    initial_html_parts = []
    for s in initial_stores:
        prod_items_html = []
        for prod in s["products"]:
            # タイトルをHTMLエスケープ
            title_esc = prod['title'].replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
            prod_items_html.append(f"""
                    <div class="store-product-item">
                        <div class="store-product-info">
                            <h4 class="store-product-title">{title_esc}</h4>
                            <div class="store-product-meta">
                                <span class="badge-no">No. {prod['no']}</span>
                                <span class="store-product-price">{prod['price_str']}</span>
                            </div>
                        </div>
                        <a href="{prod['detail_url']}" class="store-product-btn" target="_blank">
                            <span>詳細を見る</span>
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path><polyline points="15 3 21 3 21 9"></polyline><line x1="10" y1="14" x2="21" y2="3"></line></svg>
                        </a>
                    </div>""")
        
        initial_html_parts.append(f"""
            <div class="store-card">
                <div class="store-card-header">
                    <div class="store-title-group">
                        <h3 class="store-card-name">{s['store_name']}</h3>
                        <span class="store-card-pref">{s['prefecture']}</span>
                    </div>
                    <span class="stock-count-badge">在庫: {len(s['products'])}点</span>
                </div>
                <div class="store-products-wrapper">
                    {"".join(prod_items_html)}
                </div>
            </div>""")
            
    initial_results_html = "".join(initial_html_parts)
    initial_match_count = len(initial_stores)
    
    # HTMLの生成
    print("Generating Simplified Prefecture-First Store Aggregation HTML...")
    
    html_template = f"""<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>ブックオフ店舗別高額ソフト在庫チェッカー</title>
    <!-- Premium Fonts -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Outfit:wght@400;600;800&family=Noto+Sans+JP:wght@400;500;700&display=swap" rel="stylesheet">
    
    <style>
        :root {{
            --bg-color: #0b0f19;
            --card-bg: rgba(22, 28, 45, 0.7);
            --card-border: rgba(255, 255, 255, 0.08);
            --primary: #4f46e5;
            --primary-glow: rgba(79, 70, 229, 0.4);
            --accent: #f59e0b;
            --text-main: #f3f4f6;
            --text-muted: #9ca3af;
            --table-header-bg: #1e293b;
            --shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.37);
        }}

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            -webkit-tap-highlight-color: transparent;
        }}

        body {{
            background-color: var(--bg-color);
            color: var(--text-main);
            font-family: 'Inter', 'Noto Sans JP', sans-serif;
            line-height: 1.6;
            padding-bottom: 50px;
            overflow-x: hidden;
            background-image: 
                radial-gradient(at 0% 0%, rgba(79, 70, 229, 0.15) 0px, transparent 50%),
                radial-gradient(at 100% 100%, rgba(245, 158, 11) 0.08) 0px, transparent 50%);
            background-attachment: fixed;
        }}

        /* Container & Header */
        .container {{
            width: 100%;
            max-width: 1200px;
            margin: 0 auto;
            padding: 16px;
        }}

        header {{
            text-align: center;
            margin-top: 20px;
            margin-bottom: 20px;
        }}

        h1 {{
            font-family: 'Outfit', sans-serif;
            font-size: 1.8rem;
            font-weight: 800;
            background: linear-gradient(135deg, #a5b4fc 0%, #6366f1 50%, #f59e0b 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 8px;
            letter-spacing: -0.5px;
        }}

        .subtitle {{
            color: var(--text-muted);
            font-size: 0.9rem;
            margin-bottom: 4px;
        }}

        /* Search & Filter Card */
        .filter-card {{
            background: var(--card-bg);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border: 2px solid var(--primary); /* Highlight the filters */
            border-radius: 20px;
            padding: 20px;
            box-shadow: 0 8px 32px 0 rgba(79, 70, 229, 0.2);
            margin-bottom: 24px;
        }}

        .filter-grid {{
            display: grid;
            grid-template-columns: 1fr;
            gap: 16px;
        }}

        @media (min-width: 768px) {{
            .filter-grid {{
                grid-template-columns: 1fr 1fr;
            }}
            h1 {{
                font-size: 2.5rem;
            }}
        }}

        .form-group {{
            display: flex;
            flex-direction: column;
            gap: 6px;
        }}

        label {{
            font-size: 0.85rem;
            font-weight: 700;
            color: #a5b4fc;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}

        input[type="text"], select {{
            background-color: rgba(15, 23, 42, 0.8);
            border: 1px solid var(--card-border);
            border-radius: 12px;
            padding: 14px 16px;
            color: white;
            font-size: 1rem;
            outline: none;
            transition: all 0.3s ease;
            width: 100%;
        }}

        input[type="text"]:focus, select:focus {{
            border-color: var(--primary);
            box-shadow: 0 0 0 3px var(--primary-glow);
        }}

        select {{
            cursor: pointer;
            font-weight: 600;
            background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24' fill='none' stroke='white' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E");
            background-repeat: no-repeat;
            background-position: right 12px center;
            background-size: 16px;
            -webkit-appearance: none;
            -moz-appearance: none;
            appearance: none;
            padding-right: 40px;
        }}

        /* Stats Row */
        .controls-row {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
            padding: 0 4px;
        }}

        .stats {{
            font-size: 0.95rem;
            color: var(--text-muted);
        }}

        .stats span {{
            color: var(--accent);
            font-weight: bold;
        }}

        /* Results List */
        .product-list {{
            display: flex;
            flex-direction: column;
            gap: 20px;
        }}

        /* Store-centric Card Design */
        .store-card {{
            background: var(--card-bg);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border: 1px solid var(--card-border);
            border-radius: 20px;
            padding: 20px;
            box-shadow: var(--shadow);
            display: flex;
            flex-direction: column;
            gap: 14px;
        }}

        .store-card-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
            padding-bottom: 12px;
        }}

        .store-title-group {{
            display: flex;
            flex-direction: column;
            gap: 4px;
        }}

        .store-card-name {{
            font-size: 1.2rem;
            font-weight: 700;
            color: white;
            letter-spacing: -0.3px;
        }}

        .store-card-pref {{
            background: rgba(79, 70, 229, 0.2);
            color: #c7d2fe;
            padding: 2px 10px;
            border-radius: 6px;
            font-size: 0.75rem;
            font-weight: 700;
            align-self: flex-start;
        }}

        .stock-count-badge {{
            background: rgba(16, 185, 129, 0.15);
            color: #10b981;
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 0.8rem;
            font-weight: bold;
            white-space: nowrap;
        }}

        /* Inside Store Product List */
        .store-products-wrapper {{
            display: flex;
            flex-direction: column;
            gap: 8px;
        }}

        .store-product-item {{
            background: rgba(15, 23, 42, 0.5);
            border: 1px solid rgba(255, 255, 255, 0.03);
            border-radius: 14px;
            padding: 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
            transition: all 0.2s ease;
        }}

        .store-product-item:active {{
            background: rgba(255, 255, 255, 0.05);
        }}

        .store-product-info {{
            display: flex;
            flex-direction: column;
            gap: 6px;
            flex-grow: 1;
        }}

        .store-product-title {{
            font-size: 0.98rem;
            font-weight: 600;
            color: #f3f4f6;
            line-height: 1.4;
        }}

        .store-product-meta {{
            display: flex;
            align-items: center;
            gap: 10px;
        }}

        .badge-no {{
            background: rgba(255, 255, 255, 0.08);
            color: var(--text-muted);
            font-size: 0.75rem;
            padding: 2px 8px;
            border-radius: 6px;
            font-family: 'Outfit', sans-serif;
        }}

        .store-product-price {{
            font-family: 'Outfit', sans-serif;
            font-size: 1.2rem;
            font-weight: 800;
            color: var(--accent);
        }}

        .store-product-btn {{
            background: linear-gradient(135deg, #4f46e5 0%, #4338ca 100%);
            color: white;
            text-decoration: none;
            padding: 10px 18px;
            border-radius: 10px;
            font-size: 0.85rem;
            font-weight: 600;
            white-space: nowrap;
            display: inline-flex;
            align-items: center;
            gap: 4px;
            box-shadow: 0 4px 12px rgba(79, 70, 229, 0.3);
            transition: all 0.2s;
        }}

        .store-product-btn:active {{
            transform: scale(0.97);
        }}

        /* Empty State */
        .no-results {{
            text-align: center;
            padding: 40px 20px;
            color: var(--text-muted);
            background: var(--card-bg);
            border-radius: 20px;
            border: 1px solid var(--card-border);
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <p class="subtitle">BOOKOFF ONLINE SCRAPER</p>
            <h1>高額アニメソフト店舗別チェッカー</h1>
            <p class="subtitle" style="font-size: 0.8rem; margin-top: 4px;">価格: 25,000円以上 | 最終更新: {time_str_placeholder()}</p>
        </header>

        <!-- Filters Section -->
        <div class="filter-card">
            <div class="filter-grid">
                <div class="form-group">
                    <label for="pref-select">① 都道府県を選択（最優先）</label>
                    <select id="pref-select">
                        <option value="" selected>すべての都道府県</option>
                        {pref_options_html(sorted_prefs)}
                    </select>
                </div>
                <div class="form-group">
                    <label for="search-input">② 店舗名・商品名で絞り込み</label>
                    <input type="text" id="search-input" placeholder="店舗名、または商品名の一部を入力...">
                </div>
            </div>
        </div>

        <!-- Controls / Stats -->
        <div class="controls-row">
            <div class="stats">
                該当店舗数: <span id="match-count">{initial_match_count}</span> 店舗
            </div>
        </div>

        <!-- Store Cards Container -->
        <div class="product-list" id="results-container">
            {initial_results_html}
        </div>
    </div>

    <!-- Inject data and logic -->
    <script>
        const products = {json.dumps(products, ensure_ascii=False)};

        const searchInput = document.getElementById('search-input');
        const prefSelect = document.getElementById('pref-select');
        const resultsContainer = document.getElementById('results-container');
        const matchCountEl = document.getElementById('match-count');

        // デフォルトで「神奈川県」を選択状態にする (JSが有効なブラウザ環境のみ)
        window.addEventListener('DOMContentLoaded', () => {{
            const hasKanagawa = Array.from(prefSelect.options).some(opt => opt.value === '神奈川県');
            if (hasKanagawa) {{
                prefSelect.value = '神奈川県';
            }}
            // すでに初期表示がHTMLにプリレンダリングされていますが、
            // JSが有効な環境では神奈川県への自動絞り込みを走らせます。
            filterAndRender();
        }});

        function renderStores(filteredStores) {{
            resultsContainer.innerHTML = '';

            if (filteredStores.length === 0) {{
                resultsContainer.innerHTML = `
                    <div class="no-results">
                        <p style="font-size: 1.1rem; font-weight: bold; margin-bottom: 8px;">該当する店舗が見つかりません</p>
                        <p style="font-size: 0.9rem;">選択する都道府県やキーワードを変更してください。</p>
                    </div>
                `;
                matchCountEl.textContent = 0;
                return;
            }}

            matchCountEl.textContent = filteredStores.length;

            filteredStores.forEach(s => {{
                const card = document.createElement('div');
                card.className = 'store-card';

                // 店舗内の高額商品をレンダリング
                const productListHtml = s.products.map(p => `
                    <div class="store-product-item">
                        <div class="store-product-info">
                            <h4 class="store-product-title">${{p.title}}</h4>
                            <div class="store-product-meta">
                                <span class="badge-no">No. ${{p.no}}</span>
                                <span class="store-product-price">¥${{p.price_val.toLocaleString()}}</span>
                            </div>
                        </div>
                        <a href="${{p.detail_url}}" class="store-product-btn" target="_blank">
                            <span>詳細を見る</span>
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path><polyline points="15 3 21 3 21 9"></polyline><line x1="10" y1="14" x2="21" y2="3"></line></svg>
                        </a>
                    </div>
                `).join('');

                card.innerHTML = `
                    <div class="store-card-header">
                        <div class="store-title-group">
                            <h3 class="store-card-name">${{s.store_name}}</h3>
                            <span class="store-card-pref">${{s.prefecture}}</span>
                        </div>
                        <span class="stock-count-badge">在庫: ${{s.products.length}}点</span>
                    </div>
                    <div class="store-products-wrapper">
                        ${{productListHtml}}
                    </div>
                `;
                resultsContainer.appendChild(card);
            }});
        }}

        function filterAndRender() {{
            const query = searchInput.value.toLowerCase().trim();
            const selectedPref = prefSelect.value;

            // --- 店舗をキーにした逆引き辞書の構築 ---
            const storeMap = {{}};

            products.forEach(p => {{
                p.stores.forEach(s => {{
                    // 1. 都道府県フィルターに完全一致するかチェック（完全な排他処理）
                    const matchesPref = selectedPref === '' || s.prefecture === selectedPref;
                    if (!matchesPref) {{
                        return; // 他県の店舗は絶対に追加しない！
                    }}

                    // 2. キーワード検索の判定（店名、商品名、または都道府県名）
                    let matchesQuery = true;
                    if (query !== '') {{
                        const inTitle = p.title.toLowerCase().includes(query);
                        const inStore = s.store_name.toLowerCase().includes(query);
                        const inPref = s.prefecture.toLowerCase().includes(query);
                        matchesQuery = inTitle || inStore || inPref;
                    }}

                    if (matchesQuery) {{
                        if (!storeMap[s.store_name]) {{
                            storeMap[s.store_name] = {{
                                store_name: s.store_name,
                                prefecture: s.prefecture,
                                products: []
                            }};
                        }}
                        
                        // 店舗内の商品重複を避けて追加
                        if (!storeMap[s.store_name].products.some(prod => prod.title === p.title)) {{
                            storeMap[s.store_name].products.push({{
                                no: p.no,
                                title: p.title,
                                price_val: p.price_val,
                                detail_url: p.detail_url
                            }});
                        }}
                    }}
                }});
            }});

            // 辞書からリストに変換
            let storeList = Object.values(storeMap);

            // 各店舗内の商品は、価格が高い順にソート
            storeList.forEach(s => {{
                s.products.sort((a, b) => b.price_val - a.price_val);
            }});

            // 店舗カード自体は、在庫商品件数が多い順 ＞ 最高額商品が高い順 にソート
            storeList.sort((a, b) => {{
                // 1. 在庫数が多い順
                if (b.products.length !== a.products.length) {{
                    return b.products.length - a.products.length;
                }}
                // 2. 在庫数が同じなら、一番高い商品の価格が高い順
                const maxPriceA = a.products[0].price_val;
                const maxPriceB = b.products[0].price_val;
                return maxPriceB - maxPriceA;
            }});

            renderStores(storeList);
        }}

        // Event listeners
        searchInput.addEventListener('input', filterAndRender);
        prefSelect.addEventListener('change', filterAndRender);
    </script>
</body>
</html>
"""
    
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_template)
        
    print(f"Interactive HTML successfully generated at: {html_path}")

def time_str_placeholder():
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

def pref_options_html(prefs):
    return "\n".join([f'<option value="{p}">{p}</option>' for p in prefs])

if __name__ == "__main__":
    main()
