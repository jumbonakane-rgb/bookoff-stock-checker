import unittest
from unittest.mock import patch

import auto_update_checker as updater


class AutoUpdateSafetyTests(unittest.TestCase):
    def test_network_timeout_is_a_failure(self):
        with (
            patch.object(updater, "should_update", return_value=True),
            patch.object(updater, "local_branch_is_ahead", return_value=False),
            patch.object(updater, "wait_for_internet", return_value=False),
        ):
            result = updater.run_locked_update()

        self.assertEqual(result, 1)

    def test_unpushed_commit_is_recovered_even_before_next_interval(self):
        with (
            patch.object(updater, "should_update", return_value=False),
            patch.object(updater, "local_branch_is_ahead", return_value=True),
            patch.object(updater, "wait_for_internet", return_value=True),
            patch.object(updater, "run_pipeline") as run_pipeline,
            patch.object(updater, "push_and_confirm_remote") as push,
            patch.object(updater, "wait_for_pages") as pages,
            patch.object(updater, "write_last_update") as write_timestamp,
        ):
            result = updater.run_locked_update()

        self.assertEqual(result, 0)
        run_pipeline.assert_not_called()
        push.assert_called_once_with()
        pages.assert_called_once_with()
        write_timestamp.assert_called_once_with()

    def test_timestamp_is_not_written_when_pages_verification_fails(self):
        with (
            patch.object(updater, "should_update", return_value=False),
            patch.object(updater, "local_branch_is_ahead", return_value=True),
            patch.object(updater, "wait_for_internet", return_value=True),
            patch.object(updater, "push_and_confirm_remote"),
            patch.object(updater, "wait_for_pages", side_effect=RuntimeError("stale Pages")),
            patch.object(updater, "write_last_update") as write_timestamp,
        ):
            with self.assertRaisesRegex(RuntimeError, "stale Pages"):
                updater.run_locked_update()

        write_timestamp.assert_not_called()

    def test_generated_commit_scope_excludes_source_files(self):
        self.assertEqual(
            updater.GENERATED_FILES,
            [
                "index.html",
                "high_price_dvd_stock_by_prefecture.md",
                "high_price_dvd_stock.md",
            ],
        )

    def test_dirty_runtime_source_stops_before_pipeline(self):
        fake_result = type("Result", (), {"stdout": " M scraper_robust.py\n"})()
        with patch.object(updater, "git", return_value=fake_result):
            with self.assertRaisesRegex(RuntimeError, "uncommitted changes"):
                updater.assert_runtime_source_is_committed()


if __name__ == "__main__":
    unittest.main()
