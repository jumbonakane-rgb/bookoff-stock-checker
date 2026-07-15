import tempfile
import unittest
from pathlib import Path

from markdown_table import escape_markdown_cell, split_markdown_row
from scraper_robust import load_previous_snapshot_stats, render_markdown


class MarkdownTableTests(unittest.TestCase):
    def test_escaped_pipe_does_not_shift_columns(self):
        row = (
            r"| 1 | TITLE A\|B | ¥27,500 円 | 在庫なし (入荷店舗: 0店) | "
            r"[詳細ページ](https://shopping.bookoff.co.jp/used/0012345678) |"
        )

        columns = split_markdown_row(row)

        self.assertEqual(len(columns), 5)
        self.assertEqual(columns[1], "TITLE A|B")
        self.assertEqual(columns[2], "¥27,500 円")

    def test_rendered_product_round_trips_pipe_and_backslash(self):
        products = [{
            "title": r"A\B | BOX",
            "price_str": "¥27,500 円",
            "detail_url": "https://shopping.bookoff.co.jp/used/0012345678",
            "stock_status": "no_stock",
            "stores": [],
        }]

        data_row = next(line for line in render_markdown(products).splitlines() if line.startswith("| 1 |"))
        columns = split_markdown_row(data_row)

        self.assertEqual(columns[1], r"A\B | BOX")

    def test_previous_snapshot_stats_handle_escaped_pipe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "snapshot.md"
            path.write_text(
                "| No | 商品タイトル | 価格 (中古) | 店舗在庫状況 | 詳細リンク |\n"
                "| :--- | :--- | :--- | :--- | :--- |\n"
                "| 1 | A\\|B | ¥27,500 円 | 在庫なし (入荷店舗: 0店) | "
                "[詳細ページ](https://shopping.bookoff.co.jp/used/0012345678) |\n",
                encoding="utf-8",
            )

            count, ranges = load_previous_snapshot_stats(path)

        self.assertEqual(count, 1)
        self.assertEqual(ranges["27001-28000"], 1)

    def test_escape_helper_preserves_logical_value(self):
        value = r"A\B|C"
        self.assertEqual(split_markdown_row(f"| {escape_markdown_cell(value)} |")[0], value)

    def test_invalid_row_is_rejected(self):
        with self.assertRaises(ValueError):
            split_markdown_row("not a table row")


if __name__ == "__main__":
    unittest.main()
