import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import generate_html


HEADER = "\n".join(
    [
        "# Test products",
        "",
        "| No | 商品タイトル | 価格 (中古) | 店舗在庫状況 | 詳細リンク |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
)


def product_row(no, title, stock, url, price="¥30,000 円"):
    return (
        f"| {no} | {title} | {price} | {stock} | "
        f"[詳細ページ]({url}) |"
    )


def markdown_with(*rows):
    return HEADER + "\n" + "\n".join(rows) + "\n"


def embedded_products(document):
    opening = '<script type="application/json" id="product-data">'
    start = document.index(opening) + len(opening)
    end = document.index("</script>", start)
    payload = document[start:end]
    return payload, json.loads(payload)


class ParseMarkdownTests(unittest.TestCase):
    def test_escaped_pipe_parsing_uses_logical_markdown_columns(self):
        document = markdown_with(
            product_row(
                1,
                r"LIVE \| TOUR",
                r"入荷店舗: 1店<br>MEGA \| 横浜店 (神奈川県)",
                "https://shopping.bookoff.co.jp/used/0000000001",
            )
        )

        products = generate_html.parse_markdown(document)

        self.assertEqual(products[0]["title"], "LIVE | TOUR")
        self.assertEqual(products[0]["stores"][0]["store_name"], "MEGA | 横浜店")
        self.assertEqual(products[0]["stores"][0]["prefecture"], "神奈川県")

    def test_failure_statuses_preserve_code_and_specific_reason(self):
        cases = [
            ("age_verification", "年齢確認ページのため照合できません"),
            ("identity_mismatch", "商品IDと商品名が一致しません"),
            ("modal_invalid", "店舗モーダルの構造が不正です"),
            ("fetch_error", "接続がタイムアウトしました"),
        ]
        rows = []
        for index, (status, reason) in enumerate(cases, 1):
            prefix = "取得失敗" if status == "fetch_error" else "確認保留"
            rows.append(
                product_row(
                    index,
                    f"Product {index}",
                    f"{prefix} [{status}]: {reason}",
                    f"https://shopping.bookoff.co.jp/used/{index:010d}",
                )
            )

        products = generate_html.parse_markdown(markdown_with(*rows))

        self.assertEqual(
            [(item["stock_status"], item["status_reason"]) for item in products],
            cases,
        )

    def test_same_title_with_distinct_urls_is_retained(self):
        title = "Same title"
        products = generate_html.parse_markdown(
            markdown_with(
                product_row(
                    1,
                    title,
                    "在庫なし (入荷店舗: 0店)",
                    "https://shopping.bookoff.co.jp/used/0000000001",
                ),
                product_row(
                    2,
                    title,
                    "在庫なし (入荷店舗: 0店)",
                    "https://shopping.bookoff.co.jp/new/0000000002",
                ),
            )
        )

        self.assertEqual(len(products), 2)
        self.assertEqual({item["product_id"] for item in products}, {"0000000001", "0000000002"})

        _, payload_products = embedded_products(
            generate_html.build_html(products, generated_at="2026-07-16 12:00:00")
        )
        self.assertEqual(len(payload_products), 2)

    def test_malformed_data_row_fails_instead_of_being_skipped(self):
        malformed = HEADER + "\n| 1 | Missing columns | ¥30,000 円 |\n"

        with self.assertRaisesRegex(generate_html.MarkdownDataError, "expected 5 columns"):
            generate_html.parse_markdown(malformed)

    def test_missing_or_invalid_detail_url_fails(self):
        invalid_links = [
            "[詳細ページ]()",
            "[詳細ページ](http://shopping.bookoff.co.jp/used/0000000001)",
            "[詳細ページ](https://example.com/used/0000000001)",
            "[詳細ページ](https://shopping.bookoff.co.jp/used/not-digits)",
            "[詳細ページ](https://shopping.bookoff.co.jp/used/0000000001?x=1)",
        ]
        for link in invalid_links:
            with self.subTest(link=link):
                row = (
                    "| 1 | Product | ¥30,000 円 | 在庫なし (入荷店舗: 0店) | "
                    + link
                    + " |"
                )
                with self.assertRaises(generate_html.MarkdownDataError):
                    generate_html.parse_markdown(markdown_with(row))

    def test_duplicate_detail_url_fails(self):
        url = "https://shopping.bookoff.co.jp/used/0000000001"
        document = markdown_with(
            product_row(1, "First", "在庫なし (入荷店舗: 0店)", url),
            product_row(2, "Second", "在庫なし (入荷店舗: 0店)", url),
        )

        with self.assertRaisesRegex(generate_html.MarkdownDataError, "duplicate detail URL"):
            generate_html.parse_markdown(document)

    def test_zero_products_fails(self):
        with self.assertRaisesRegex(generate_html.MarkdownDataError, "zero products"):
            generate_html.parse_markdown(HEADER + "\n")

    def test_stock_count_mismatch_is_malformed(self):
        document = markdown_with(
            product_row(
                1,
                "Product",
                "入荷店舗: 2店<br>横浜店 (神奈川県)",
                "https://shopping.bookoff.co.jp/used/0000000001",
            )
        )

        with self.assertRaisesRegex(generate_html.MarkdownDataError, "stock count says 2"):
            generate_html.parse_markdown(document)


class BuildHtmlTests(unittest.TestCase):
    def test_script_tag_content_is_escaped_inside_embedded_json(self):
        malicious_title = "</script><script>alert(1)</script>"
        products = generate_html.parse_markdown(
            markdown_with(
                product_row(
                    1,
                    malicious_title,
                    "在庫なし (入荷店舗: 0店)",
                    "https://shopping.bookoff.co.jp/used/0000000001",
                )
            )
        )

        document = generate_html.build_html(products, generated_at="2026-07-16")
        payload, decoded = embedded_products(document)

        self.assertNotIn("</script", payload.lower())
        self.assertIn(r"\u003c/script\u003e", payload)
        self.assertEqual(decoded[0]["title"], malicious_title)

    def test_app_is_data_only_dom_rendered_and_mobile_zoomable(self):
        products = generate_html.parse_markdown(
            markdown_with(
                product_row(
                    1,
                    "Unique product title",
                    "入荷店舗: 1店<br>横浜店 (神奈川県)",
                    "https://shopping.bookoff.co.jp/used/0000000001",
                )
            )
        )

        document = generate_html.build_html(products, generated_at="2026-07-16")

        self.assertEqual(document.count("Unique product title"), 1)
        self.assertNotIn("innerHTML", document)
        self.assertIn('name="viewport" content="width=device-width, initial-scale=1"', document)
        self.assertNotIn("user-scalable=no", document)
        self.assertIn('<link rel="icon" href="data:,">', document)
        self.assertIn('data-mode="stores"', document)
        self.assertIn('data-mode="pending"', document)
        self.assertIn('data-mode="all"', document)
        self.assertIn('age_verification: "年齢確認のため保留"', document)
        self.assertIn('modal_invalid: "店舗情報の照合を保留"', document)

    def test_atomic_writer_replaces_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "index.html"
            target.write_text("old", encoding="utf-8")

            generate_html.write_html_atomic(target, "new document")

            self.assertEqual(target.read_text(encoding="utf-8"), "new document")
            self.assertEqual(list(Path(temp_dir).glob(".index.html.*.tmp")), [])


class CliFailureTests(unittest.TestCase):
    def test_missing_input_returns_nonzero_without_writing_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing.md"
            output = Path(temp_dir) / "index.html"
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                result = generate_html.main(
                    ["--input", str(missing), "--output", str(output)]
                )

            self.assertNotEqual(result, 0)
            self.assertFalse(output.exists())
            self.assertIn("HTML generation failed", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
