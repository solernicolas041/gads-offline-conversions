#!/usr/bin/env python3
"""Offline tests — no credentials, no network. Run: python3 tests/test_gads_offline.py"""
from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gads_offline as g  # noqa: E402

TZ = "+02:00"


class TestEmail(unittest.TestCase):
    def test_lowercase_and_trim(self):
        self.assertEqual(g.norm_email("  Jane.Doe@Example.COM "), "jane.doe@example.com")

    def test_gmail_dots_and_tags_removed(self):
        self.assertEqual(g.norm_email("ja.ne.doe+ads@gmail.com"), "janedoe@gmail.com")
        self.assertEqual(g.norm_email("janedoe@googlemail.com"), "janedoe@gmail.com")

    def test_non_gmail_keeps_dots(self):
        self.assertEqual(g.norm_email("ja.ne@outlook.com"), "ja.ne@outlook.com")

    def test_garbage_rejected(self):
        for bad in ("", None, "not-an-email", "a@b", "@example.com"):
            self.assertIsNone(g.norm_email(bad))


class TestPhone(unittest.TestCase):
    def test_national_gets_country_code(self):
        self.assertEqual(g.norm_phone("06 39 98 76 01", "33"), "+33639987601")

    def test_spreadsheet_float_and_missing_zero(self):
        self.assertEqual(g.norm_phone("639987601.0", "33"), "+33639987601")

    def test_already_e164_is_untouched(self):
        self.assertEqual(g.norm_phone("+33639987601", "33"), "+33639987601")

    def test_empty_rejected(self):
        for bad in ("", None, "   ", "n/a"):
            self.assertIsNone(g.norm_phone(bad, "33"))


class TestAmount(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(g.parse_amount("450"), 450.0)

    def test_french_decimal_comma(self):
        self.assertEqual(g.parse_amount("1 234,56 €"), 1234.56)

    def test_english_thousands(self):
        self.assertEqual(g.parse_amount("$1,234.56"), 1234.56)

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            g.parse_amount("")


class TestDatetime(unittest.TestCase):
    def test_offset_appended(self):
        self.assertEqual(g.parse_datetime("2026-07-14 15:09:11", TZ), "2026-07-14 15:09:11+02:00")

    def test_existing_offset_kept(self):
        self.assertEqual(g.parse_datetime("2026-07-14 15:09:11+05:30", TZ),
                         "2026-07-14 15:09:11+05:30")

    def test_date_only_becomes_midnight(self):
        self.assertEqual(g.parse_datetime("2026-07-14", TZ), "2026-07-14 00:00:00+02:00")

    def test_unreadable_rejected(self):
        with self.assertRaises(ValueError):
            g.parse_datetime("14/07/2026", TZ)


class TestBuildRows(unittest.TestCase):
    def test_calls_mode(self):
        rows, skipped = g.build_rows(
            [{"label": "Job 1", "caller_id": "0639987601",
              "call_start_time": "2026-07-14 15:09:11",
              "conversion_time": "2026-07-14 18:00:00", "value": "450"}],
            "calls", TZ, "EUR", "33")
        self.assertEqual(skipped, [])
        self.assertEqual(rows[0]["caller_id"], "+33639987601")
        self.assertEqual(rows[0]["call_start_time"], "2026-07-14 15:09:11+02:00")

    def test_calls_without_caller_is_skipped_with_a_reason(self):
        rows, skipped = g.build_rows(
            [{"label": "Job 1", "caller_id": "", "call_start_time": "2026-07-14 15:09:11",
              "conversion_time": "2026-07-14 18:00:00", "value": "450"}],
            "calls", TZ, "EUR", "33")
        self.assertEqual(rows, [])
        self.assertIn("caller_id", skipped[0]["reason"])
        self.assertEqual(skipped[0]["line"], 2)

    def test_clicks_mode_picks_any_click_id(self):
        rows, _ = g.build_rows(
            [{"label": "Deal", "wbraid": "Cj0KabC", "conversion_time": "2026-07-14 10:00:00",
              "value": "1200"}], "clicks", TZ, "EUR")
        self.assertEqual(rows[0]["click_id_field"], "wbraid")

    def test_clicks_without_any_id_is_skipped(self):
        _, skipped = g.build_rows(
            [{"label": "Deal", "conversion_time": "2026-07-14 10:00:00", "value": "1200"}],
            "clicks", TZ, "EUR")
        self.assertIn("gclid", skipped[0]["reason"])

    def test_enhanced_hashes_both_identifiers(self):
        rows, _ = g.build_rows(
            [{"label": "Lead", "email": "Jane@Example.com", "phone": "0639987601",
              "conversion_time": "2026-07-14 10:00:00", "value": "800"}],
            "enhanced", TZ, "EUR", "33")
        self.assertEqual(rows[0]["hashed_email"],
                         hashlib.sha256(b"jane@example.com").hexdigest())
        self.assertEqual(rows[0]["hashed_phone"],
                         hashlib.sha256(b"+33639987601").hexdigest())

    def test_enhanced_without_identifiers_is_skipped(self):
        _, skipped = g.build_rows(
            [{"label": "Lead", "conversion_time": "2026-07-14 10:00:00", "value": "800"}],
            "enhanced", TZ, "EUR")
        self.assertIn("neither", skipped[0]["reason"])

    def test_one_bad_row_does_not_stop_the_file(self):
        rows, skipped = g.build_rows(
            [{"label": "good", "gclid": "abc", "conversion_time": "2026-07-14 10:00:00", "value": "10"},
             {"label": "bad", "gclid": "def", "conversion_time": "nope", "value": "20"},
             {"label": "good2", "gclid": "ghi", "conversion_time": "2026-07-15 10:00:00", "value": "30"}],
            "clicks", TZ, "EUR")
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(skipped), 1)


class TestDedupe(unittest.TestCase):
    def _call_row(self, line, value, caller="+33639987601", start="2026-07-14 15:09:11+02:00"):
        return {"line": line, "label": f"row{line}", "value": value,
                "caller_id": caller, "call_start_time": start,
                "conversion_time": "2026-07-14 18:00:00+02:00", "currency": "EUR"}

    def test_same_call_counted_once(self):
        kept, dropped = g.dedupe([self._call_row(2, 450), self._call_row(3, 450)], "calls")
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(dropped), 1)

    def test_different_call_times_are_both_kept(self):
        kept, dropped = g.dedupe(
            [self._call_row(2, 450), self._call_row(3, 450, start="2026-07-14 15:12:06+02:00")],
            "calls")
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, [])

    def test_clicks_dedupe_on_click_id(self):
        rows = [{"line": 2, "label": "a", "value": 10, "click_id": "X",
                 "conversion_time": "t", "currency": "EUR"},
                {"line": 3, "label": "b", "value": 10, "click_id": "X",
                 "conversion_time": "t", "currency": "EUR"}]
        kept, dropped = g.dedupe(rows, "clicks")
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped[0]["line"], 3)


class TestModeGuard(unittest.TestCase):
    def test_calls_onto_a_click_action_is_refused(self):
        action = {"name": "Web lead", "type": "UPLOAD_CLICKS", "status": "ENABLED"}
        with self.assertRaises(SystemExit) as ctx:
            g.check_mode_matches_action("calls", action)
        self.assertIn("records nothing", str(ctx.exception))

    def test_clicks_onto_a_call_action_is_refused(self):
        action = {"name": "Phone lead", "type": "UPLOAD_CALLS", "status": "ENABLED"}
        with self.assertRaises(SystemExit):
            g.check_mode_matches_action("clicks", action)

    def test_matching_pair_passes(self):
        g.check_mode_matches_action("calls", {"name": "Phone", "type": "UPLOAD_CALLS",
                                              "status": "ENABLED"})
        g.check_mode_matches_action("enhanced", {"name": "Lead", "type": "UPLOAD_CLICKS",
                                                 "status": "ENABLED"})

    def test_paused_action_is_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            g.check_mode_matches_action("calls", {"name": "Phone", "type": "UPLOAD_CALLS",
                                                  "status": "REMOVED"})
        self.assertIn("discarded", str(ctx.exception))


class TestSummary(unittest.TestCase):
    def test_dry_run_says_nothing_was_sent(self):
        rows = [{"line": 2, "label": "Job", "value": 450.0, "currency": "EUR",
                 "conversion_time": "2026-07-14 18:00:00+02:00",
                 "caller_id": "+33639987601", "call_start_time": "2026-07-14 15:09:11+02:00"}]
        out = g.summary(rows, [], [], {"name": "Calls", "type": "UPLOAD_CALLS",
                                       "status": "ENABLED"}, "calls", live=False)
        self.assertIn("DRY RUN", out)
        self.assertIn("Nothing was sent", out)
        self.assertIn("450.00", out)

    def test_duplicates_are_reported_with_their_value(self):
        dup = [{"line": 3, "label": "Job again", "value": 450.0, "currency": "EUR"}]
        out = g.summary([], [], dup, {"name": "Calls", "type": "UPLOAD_CALLS",
                                      "status": "ENABLED"}, "calls", live=False)
        self.assertIn("Duplicates dropped: 1", out)
        self.assertIn("inflated", out)


class TestCli(unittest.TestCase):
    def test_missing_file_exits(self):
        with self.assertRaises(SystemExit):
            g.main(["--customer-id", "123-456-7890", "--conversion-action", "1",
                    "--mode", "calls", "--csv", "/nope/missing.csv"])

    def test_bad_customer_id_exits(self):
        sample = Path(__file__).resolve().parents[1] / "examples" / "calls.csv"
        with self.assertRaises(SystemExit):
            g.main(["--customer-id", "123", "--conversion-action", "1",
                    "--mode", "calls", "--csv", str(sample)])

    def test_examples_parse_cleanly(self):
        base = Path(__file__).resolve().parents[1] / "examples"
        for mode, name in (("calls", "calls.csv"), ("clicks", "clicks.csv"),
                           ("enhanced", "enhanced.csv")):
            raw = g.read_csv(base / name)
            self.assertTrue(raw, f"{name} is empty")
            self.assertFalse(g.REQUIRED_COLUMNS[mode] - set(raw[0]),
                             f"{name} is missing required columns for --mode {mode}")
            rows, skipped = g.build_rows(raw, mode, TZ, "EUR", "33")
            self.assertEqual(skipped, [], f"{name}: {skipped}")
            self.assertTrue(rows)


if __name__ == "__main__":
    unittest.main(verbosity=2)
