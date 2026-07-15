import unittest
from collections import Counter

import scraper_robust as scraper


PRODUCT_URL = "https://shopping.bookoff.co.jp/used/0012345678"
PRODUCT_TITLE = "照合対象 Blu-ray BOX"
JAN = "4901234567890"


def modal_html(title, declared_count, stores):
    store_rows = "".join(
        f"""
        <li class="modalStoreInformation__item">
          <a class="modalStoreInformation__link" href="{store_url}">
            BOOKOFF {store_name}
            <small class="modalStoreInformation__address">{address}</small>
          </a>
        </li>
        """
        for store_name, address, store_url in stores
    )
    return f"""
    <div id="modalStoreInformation">
      <p class="modalStoreInformation__title">{title}</p>
      <p class="modalStoreInformation__heading">商品が入荷した店舗：{declared_count}店</p>
      <ul class="modalStoreInformation__list">{store_rows}</ul>
    </div>
    """


def product_html(*modals, json_jan=JAN, displayed_jan=JAN):
    return f"""
    <html>
      <head>
        <link rel="canonical" href="{PRODUCT_URL}">
        <script type="application/ld+json">
          {{
            "@type": "Product",
            "name": "{PRODUCT_TITLE}",
            "@id": "{PRODUCT_URL}",
            "url": "{PRODUCT_URL}",
            "gtin13": "{json_jan}"
          }}
        </script>
      </head>
      <body>
        <table><tr><th>JAN</th><td>{displayed_jan}</td></tr></table>
        {''.join(modals)}
      </body>
    </html>
    """


class StoreStockParsingTests(unittest.TestCase):
    def test_selects_modal_matching_the_target_product(self):
        correct = modal_html(
            PRODUCT_TITLE,
            1,
            [("大阪心斎橋店", "大阪府大阪市", "https://www.bookoff.co.jp/shop/shop20345.html")],
        )
        dummy = modal_html(
            "別の商品",
            2,
            [
                ("札幌南2条店", "北海道札幌市中央区", "/"),
                ("札幌南2条店", "北海道札幌市中央区", "/"),
            ],
        )

        result = scraper.parse_store_stock_html(
            product_html(correct, dummy),
            PRODUCT_URL,
            PRODUCT_TITLE,
        )

        self.assertEqual(result["stock_status"], scraper.STATUS_AVAILABLE)
        self.assertEqual(result["stores"], [("大阪心斎橋店", "大阪府大阪市")])
        self.assertEqual(result["jan"], JAN)

    def test_age_gate_never_returns_the_dummy_sapporo_store(self):
        html = f"""
        <script>window.location.href = 'https://shopping.bookoff.co.jp/age-verification';</script>
        {modal_html(
            "別の商品",
            32,
            [("札幌南2条店", "北海道札幌市中央区", "/")] * 10,
        )}
        """

        result = scraper.parse_store_stock_html(html, PRODUCT_URL, PRODUCT_TITLE)

        self.assertEqual(result["stock_status"], scraper.STATUS_AGE_VERIFICATION)
        self.assertEqual(result["stores"], [])

    def test_jan_mismatch_is_not_treated_as_no_stock(self):
        zero_store_modal = modal_html(PRODUCT_TITLE, 0, [])
        result = scraper.parse_store_stock_html(
            product_html(zero_store_modal, displayed_jan="4900000000000"),
            PRODUCT_URL,
            PRODUCT_TITLE,
        )

        self.assertEqual(result["stock_status"], scraper.STATUS_IDENTITY_MISMATCH)
        self.assertNotEqual(result["stock_status"], scraper.STATUS_NO_STOCK)

    def test_broken_store_count_is_rejected(self):
        broken = modal_html(
            PRODUCT_TITLE,
            32,
            [("札幌南2条店", "北海道札幌市中央区", "/")] * 10,
        )
        result = scraper.parse_store_stock_html(
            product_html(broken),
            PRODUCT_URL,
            PRODUCT_TITLE,
        )

        self.assertEqual(result["stock_status"], scraper.STATUS_MODAL_INVALID)
        self.assertEqual(result["stores"], [])

    def test_verified_zero_store_page_is_no_stock(self):
        result = scraper.parse_store_stock_html(
            product_html(modal_html(PRODUCT_TITLE, 0, [])),
            PRODUCT_URL,
            PRODUCT_TITLE,
        )

        self.assertEqual(result["stock_status"], scraper.STATUS_NO_STOCK)
        self.assertEqual(result["stores"], [])


class SearchAndOutputSafetyTests(unittest.TestCase):
    def test_search_parser_extracts_only_high_value_product_cards(self):
        html = """
        <div class="productItem js-hoverItem">
          <a href="/used/0012345678"></a>
          <p class="productItem__title">高額商品</p>
          <p class="productItem__price">&yen;27,500円</p>
        </div>
        <div class="productItem js-hoverItem">
          <a href="/used/0099999999"></a>
          <p class="productItem__title">対象外商品</p>
          <p class="productItem__price">&yen;24,999円</p>
        </div>
        """

        products, raw_count = scraper.parse_search_products(html)

        self.assertEqual(raw_count, 2)
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["detail_url"], PRODUCT_URL)

    def test_collection_guard_detects_a_collapsed_price_range(self):
        products = [
            {
                "price_val": 27500,
                "detail_url": f"https://shopping.bookoff.co.jp/used/{index:010d}",
            }
            for index in range(20)
        ]
        previous_ranges = Counter({"27001-28000": 100})

        errors = scraper.validate_collection(products, 20, previous_ranges)

        self.assertTrue(any("27001-28000" in error for error in errors))

    def test_unverified_status_is_rendered_separately(self):
        product = {
            "stock_status": scraper.STATUS_AGE_VERIFICATION,
            "stores": [],
        }
        self.assertIn("確認保留", scraper.stock_cell_text(product))
        self.assertNotIn("在庫なし", scraper.stock_cell_text(product))


if __name__ == "__main__":
    unittest.main()
