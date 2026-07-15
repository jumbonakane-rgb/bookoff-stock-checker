import unittest
from collections import Counter
from unittest.mock import patch

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

    def test_age_gate_is_detected_without_redirect_script(self):
        html = '<a href="/age-verification?return=/used/0012345678">年齢確認</a>'

        result = scraper.parse_store_stock_html(html, PRODUCT_URL, PRODUCT_TITLE)

        self.assertEqual(result["stock_status"], scraper.STATUS_AGE_VERIFICATION)

    def test_matching_dummy_sapporo_modal_with_root_link_is_rejected(self):
        dummy = modal_html(
            PRODUCT_TITLE,
            1,
            [("札幌南2条店", "北海道札幌市中央区", "/")],
        )

        result = scraper.parse_store_stock_html(
            product_html(dummy),
            PRODUCT_URL,
            PRODUCT_TITLE,
        )

        self.assertEqual(result["stock_status"], scraper.STATUS_MODAL_INVALID)
        self.assertEqual(result["stores"], [])

    def test_duplicate_store_rows_are_rejected_even_when_count_matches(self):
        duplicate = modal_html(
            PRODUCT_TITLE,
            2,
            [
                ("大阪心斎橋店", "大阪府大阪市", "https://www.bookoff.co.jp/shop/shop20345.html"),
                ("大阪心斎橋店", "大阪府大阪市", "https://www.bookoff.co.jp/shop/shop20345.html"),
            ],
        )

        result = scraper.parse_store_stock_html(
            product_html(duplicate),
            PRODUCT_URL,
            PRODUCT_TITLE,
        )

        self.assertEqual(result["stock_status"], scraper.STATUS_MODAL_INVALID)

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

    def test_age_verification_form_is_validated(self):
        html = """
        <form id="ageVerificationForm" method="post"
              action="https://shopping.bookoff.co.jp/age-verification">
          <input type="hidden" name="ageVerification" value="ok">
          <input type="hidden" name="backUrl" value="">
        </form>
        """

        action, form_data = scraper.parse_age_verification_form(html)

        self.assertEqual(action, scraper.AGE_VERIFICATION_URL)
        self.assertEqual(form_data, {"ageVerification": "ok", "backUrl": ""})

    def test_untrusted_age_verification_form_is_rejected(self):
        html = """
        <form id="ageVerificationForm" method="post" action="https://example.com/">
          <input type="hidden" name="ageVerification" value="ok">
        </form>
        """

        with self.assertRaisesRegex(ValueError, "送信先"):
            scraper.parse_age_verification_form(html)

    def test_fetch_rechecks_product_after_age_verification(self):
        age_html = "<script>location.href='/age-verification';</script>"
        gate_html = """
        <form id="ageVerificationForm" method="post"
              action="https://shopping.bookoff.co.jp/age-verification">
          <input type="hidden" name="ageVerification" value="ok">
          <input type="hidden" name="backUrl" value="">
        </form>
        """
        verified_html = product_html(modal_html(PRODUCT_TITLE, 0, []))

        class FakeResponse:
            def __init__(self, text, status_code=200):
                self.text = text
                self.status_code = status_code

        class FakeSession:
            def __init__(self):
                self.headers = {}
                self.detail_requests = 0
                self.posted = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, url, timeout):
                if url == scraper.AGE_VERIFICATION_URL:
                    return FakeResponse(gate_html)
                self.detail_requests += 1
                return FakeResponse(age_html if self.detail_requests == 1 else verified_html)

            def post(self, url, data, timeout):
                self.posted.append((url, data))
                return FakeResponse("ok")

        fake_session = FakeSession()
        with (
            patch.object(scraper.requests, "Session", return_value=fake_session),
            patch.object(scraper.time, "sleep"),
            patch.object(scraper.random, "uniform", return_value=0),
        ):
            result = scraper.fetch_store_stock(PRODUCT_URL, PRODUCT_TITLE, max_retries=1)

        self.assertEqual(result["stock_status"], scraper.STATUS_NO_STOCK)
        self.assertEqual(fake_session.detail_requests, 2)
        self.assertEqual(fake_session.posted[0][0], scraper.AGE_VERIFICATION_URL)


class SearchAndOutputSafetyTests(unittest.TestCase):
    def test_search_parser_extracts_only_high_value_product_cards(self):
        html = """
        <p class="productSearch__num">1件～1件（全1件）</p>
        <div class="productItem js-hoverItem">
          <a href="/used/0012345678"></a>
          <p class="productItem__title">高額商品</p>
          <p class="productItem__price">&yen;27,500円</p>
        </div>
        """

        parsed = scraper.parse_search_products(html)

        self.assertEqual(parsed["raw_item_count"], 1)
        self.assertEqual(parsed["total_count"], 1)
        self.assertEqual(parsed["parse_errors"], [])
        self.assertEqual(parsed["products"][0]["detail_url"], PRODUCT_URL)

    def test_search_parser_rejects_an_unparseable_card(self):
        html = """
        <p class="productSearch__num">1件～1件（全1件）</p>
        <div class="productItem js-hoverItem">
          <p class="productItem__title">URLなし商品</p>
          <p class="productItem__price">&yen;27,500円</p>
        </div>
        """

        parsed = scraper.parse_search_products(html)

        self.assertEqual(parsed["raw_item_count"], 1)
        self.assertEqual(parsed["products"], [])
        self.assertTrue(parsed["parse_errors"])

    def test_search_parser_requires_explicit_empty_message(self):
        blank = scraper.parse_search_products("<html><body></body></html>")
        explicit = scraper.parse_search_products(
            f"<p>{scraper.SEARCH_EMPTY_MESSAGE}</p>"
        )

        self.assertFalse(blank["confirmed_empty"])
        self.assertTrue(explicit["confirmed_empty"])

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
            "status_reason": "年齢確認ページです",
        }
        self.assertIn("確認保留", scraper.stock_cell_text(product))
        self.assertIn("age_verification", scraper.stock_cell_text(product))
        self.assertIn("年齢確認ページです", scraper.stock_cell_text(product))
        self.assertNotIn("在庫なし", scraper.stock_cell_text(product))


if __name__ == "__main__":
    unittest.main()
