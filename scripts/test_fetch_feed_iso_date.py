#!/usr/bin/env python3
"""Regression tests: EGX harvest publish date must come from the
standalone date line, never from the period start (borsagator #109
follow-up).

Bug: COMI 297291/297289 were stamped 2026-01-01 — the harvester's
first-date-wins rule picked the period start ("من 01/01/2026") out of
the title line, ahead of the standalone "10/11/2026" line under it.
`attach_egx_harvest` trusted the harvest "date" field verbatim, so the
wrong date reached the feed (the #91 add path) and also broke the
3-day attach gate (an RSS item dated on the real publish date could
never match a harvest item stamped 2026-01-01).

The two COMI cases FAIL on pristine fetch_feed.py and PASS on the
fixed one; the negative case (no standalone date line) must come back
dateless (""), never stamped with the period start.

Run:  python3 scripts/test_fetch_feed_iso_date.py
  or:  python3 -m pytest scripts/test_fetch_feed_iso_date.py -q
  or:  python3 -m unittest scripts.test_fetch_feed_iso_date -v
No network, no fixture files: the real harvested bodies are embedded
below (COMI harvest, harvested_at 2026-11-18T13:40:16Z).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fetch_feed

# --- Real harvested bodies (COMI, live harvest 2026-11-18) -----------
# 297291: consolidated Q3 results. Title carries the period range;
# standalone date line "10/11/2026" sits under it. Harvest "date"
# field (buggy): "2026-01-01".
BODY_297291 = 'تفاصيل الأخبار\n\n\n\nالبنك التجاري الدولي -مصر (سى اى بى ) (COMI.CA) تعلن نتائج أعمالها المجمعة عن الفترة: من 01/01/2026 إلى 30/09/2026\n10/11/2026\nاسم البنك: البنك التجاري الدولي -مصر (سى اى بى )\nكود الترقيم الدولي: EGS60121C018\nالعملة: جنيه مصرى\nالقوائم المالية المجمعة عن الفترة: من 01/01/2026 إلى 30/09/2026\nصافي الربح: 63,079,356 (القيمة بالألف)\nأرقام المقارنة المجمعة عن الفترة: من 01/01/2025 إلى 30/09/2025\nصافي الربح لفترة المقارنة: 62,100,264 (القيمة بالألف)\nتقرير فحص محدود: مرفق\nالمصدر: البنك التجاري الدولي -مصر (سى اى بى )\n\nمع الأخذ في الإعتبار حقوق الأقلية\n تقرير ممارسة النشاط المجمعة (64 KB)'
TITLE_297291 = 'البنك التجاري الدولي -مصر (سى اى بى ) (COMI.CA) تعلن نتائج أعمالها المجمعة عن الفترة: من 01/01/2026 إلى 30/09/2026'
URL_297291 = 'https://www.egx.com.eg/ar/NewsDetails.aspx?NewsID=297291'

# 297289: standalone Q3 results. Same layout, same bug.
BODY_297289 = 'تفاصيل الأخبار\n\n\n\nالبنك التجاري الدولي -مصر (سى اى بى ) (COMI.CA) تعلن نتائج أعمالها المستقلة عن الفترة: من 01/01/2026 إلى 30/09/2026\n10/11/2026\nاسم البنك: البنك التجاري الدولي -مصر (سى اى بى )\nكود الترقيم الدولي: EGS60121C018\nالعملة: جنيه مصرى\nالقوائم المالية المستقلة عن الفترة: من 01/01/2026 إلى 30/09/2026\nصافي الربح: 62,973,508 (القيمة بالألف)\nأرقام المقارنة المستقلة عن الفترة: من 01/01/2025 إلى 30/09/2025\nصافي الربح لفترة المقارنة: 62,207,506 (القيمة بالألف)\nتقرير فحص محدود: مرفق\nالمصدر: البنك التجاري الدولي -مصر (سى اى بى )\n تقرير ممارسة النشاط المستقل (63 KB)'
TITLE_297289 = 'البنك التجاري الدولي -مصر (سى اى بى ) (COMI.CA) تعلن نتائج أعمالها المستقلة عن الفترة: من 01/01/2026 إلى 30/09/2026'
URL_297289 = 'https://www.egx.com.eg/ar/NewsDetails.aspx?NewsID=297289'

# 297292: results statement (English). Correctly stamped 2026-11-10
# by the old code too — must stay 2026-11-10 (no regression).
BODY_297292 = 'تفاصيل الأخبار\n\n\n\nبيان من البنك التجاري الدولي -مصر (سى اى بى ) (COMI.CA) بخصوص نتائج أعمال البنك (باللغة الانجليزية)\n10/11/2026\nاسم البنك: البنك التجاري الدولي -مصر (سى اى بى )\nكود الترقيم الدولي: EGS60121C018\nكود رويترز: COMI.CA\nمضمون الإعلان :\nبيان من البنك بخصوص نتائج الأعمال عن الفترة المالية المنتهية في 30/09/2026 (باللغة الانجليزية).\n بيان من البنك باللغة الإنجليزية (440 KB)'
TITLE_297292 = 'بيان من البنك التجاري الدولي -مصر (سى اى بى ) (COMI.CA) بخصوص نتائج أعمال البنك (باللغة الانجليزية)'
URL_297292 = 'https://www.egx.com.eg/ar/NewsDetails.aspx?NewsID=297292'

# 296114: board report. Correctly stamped 2026-10-18 — must stay.
BODY_296114 = 'تفاصيل الأخبار\n\n\n\nالبنك التجاري الدولي -مصر (سى اى بى ) (COMI.CA) - نموذج تقرير إفصاح عن مجلس الإدارة وهيكل المساهمين\n18/10/2026\nاسم البنك: البنك التجاري الدولي -مصر (سى اى بى )\nكود الترقيم الدولي: EGS60121C018\nكود رويترز: COMI.CA\nمضمون الإعلان :\nورد نموذج تقرير إفصاح البنك عن مجلس الإدارة وهيكل المساهمين عن الفترة المنتهية في 30/09/2026, تنفيذاً للمادة 30 من قواعد القيد وذلك على مسئولية البنك.\n نموذج تقرير إفصاح باللغة العربية (205 KB)\n نموذج تقرير إفصاح باللغة الإنجليزية (2,662 KB)'
TITLE_296114 = 'البنك التجاري الدولي -مصر (سى اى بى ) (COMI.CA) - نموذج تقرير إفصاح عن مجلس الإدارة وهيكل المساهمين'
URL_296114 = 'https://www.egx.com.eg/ar/NewsDetails.aspx?NewsID=296114'

# Negative: 297291's body with the standalone date line deleted. The
# period range is still in the title, so a first-date reader would
# still find 01/01/2026 — the fixed code must return dateless instead.
BODY_NO_DATE_LINE = 'تفاصيل الأخبار\n\n\n\nالبنك التجاري الدولي -مصر (سى اى بى ) (COMI.CA) تعلن نتائج أعمالها المجمعة عن الفترة: من 01/01/2026 إلى 30/09/2026\nاسم البنك: البنك التجاري الدولي -مصر (سى اى بى )\nكود الترقيم الدولي: EGS60121C018\nالعملة: جنيه مصرى\nالقوائم المالية المجمعة عن الفترة: من 01/01/2026 إلى 30/09/2026\nصافي الربح: 63,079,356 (القيمة بالألف)\nأرقام المقارنة المجمعة عن الفترة: من 01/01/2025 إلى 30/09/2025\nصافي الربح لفترة المقارنة: 62,100,264 (القيمة بالألف)\nتقرير فحص محدود: مرفق\nالمصدر: البنك التجاري الدولي -مصر (سى اى بى )\n\nمع الأخذ في الإعتبار حقوق الأقلية\n تقرير ممارسة النشاط المجمعة (64 KB)'


def _harvest_item(title, body, url, harvest_date, news_id):
    return {"id": news_id, "kind": "disclosure", "source": "egx.com.eg",
            "date": harvest_date, "title": title, "url": url,
            "row_text": title, "body": body,
            "body_chars": len(body)}


def _run_attach(harvest_items, disc=None, days=3650):
    """Run attach_egx_harvest with a stubbed harvest loader."""
    harvest = {"ticker": "COMI.CA", "items": harvest_items}
    orig = fetch_feed._load_egx_harvest
    fetch_feed._load_egx_harvest = lambda ticker: (harvest, "test-fixture")
    try:
        disc = list(disc) if disc is not None else []
        attached, added, source = fetch_feed.attach_egx_harvest(
            disc, "COMI.CA", days=days)
        return disc, attached, added, source
    finally:
        fetch_feed._load_egx_harvest = orig


def _by_news_id(disc):
    return {(it.get("url") or "").split("NewsID=")[-1]: it for it in disc}


class TestIsoDateFix(unittest.TestCase):
    def test_comi_297291_publish_date(self):
        """297291: stamped 2026-01-01 (period start) on pristine code;
        must be 2026-11-10 (standalone date line) after the fix."""
        items = [_harvest_item(TITLE_297291, BODY_297291, URL_297291,
                               "2026-01-01", "297291")]
        disc, _, added, _ = _run_attach(items)
        self.assertEqual(added, 1)
        self.assertEqual(_by_news_id(disc)["297291"]["date"], "2026-11-10")

    def test_comi_297289_publish_date(self):
        """297289: same bug, same expected date."""
        items = [_harvest_item(TITLE_297289, BODY_297289, URL_297289,
                               "2026-01-01", "297289")]
        disc, _, added, _ = _run_attach(items)
        self.assertEqual(added, 1)
        self.assertEqual(_by_news_id(disc)["297289"]["date"], "2026-11-10")

    def test_no_standalone_line_is_dateless_not_period_start(self):
        """No standalone date line: the item must be added DATELESS
        (""), never silently stamped with the period start 2026-01-01
        that the harvest "date" field carries."""
        items = [_harvest_item(TITLE_297291, BODY_NO_DATE_LINE,
                               URL_297291, "2026-01-01", "297291")]
        disc, _, added, _ = _run_attach(items)
        self.assertEqual(added, 1, "dateless item must still be added")
        self.assertEqual(_by_news_id(disc)["297291"]["date"], "")

    def test_unaffected_items_keep_their_dates(self):
        """Items the old code already dated right must not move."""
        items = [
            _harvest_item(TITLE_297292, BODY_297292, URL_297292,
                          "2026-11-10", "297292"),
            _harvest_item(TITLE_296114, BODY_296114, URL_296114,
                          "2026-10-18", "296114"),
        ]
        disc, _, added, _ = _run_attach(items)
        self.assertEqual(added, 2)
        got = _by_news_id(disc)
        self.assertEqual(got["297292"]["date"], "2026-11-10")
        self.assertEqual(got["296114"]["date"], "2026-10-18")

    def test_attach_gate_uses_corrected_date(self):
        """An RSS disclosure dated 2026-11-10 with 297291's title must
        get its body attached. On pristine code the 3-day gate compares
        the RSS date against the wrong harvest date 2026-01-01 and
        rejects the match, so no body attaches."""
        items = [_harvest_item(TITLE_297291, BODY_297291, URL_297291,
                               "2026-01-01", "297291")]
        rss_item = {"title": TITLE_297291, "date": "2026-11-10",
                    "source": "mubasher",
                    "url": "https://example.invalid/rss-297291",
                    "kind": "disclosure", "body": ""}
        disc, attached, _, _ = _run_attach(items, disc=[rss_item])
        self.assertEqual(attached, 1)
        self.assertGreaterEqual(len(disc[0].get("body") or ""), 200)
        self.assertEqual(disc[0].get("body_source"), "egx-harvest")


class TestHarvestPublishDate(unittest.TestCase):
    def test_standalone_line(self):
        self.assertEqual(
            fetch_feed._harvest_publish_date(BODY_297291).isoformat(),
            "2026-11-10")

    def test_inline_dates_only_are_not_a_publish_date(self):
        body = ("Title عن الفترة: من 01/01/2026 إلى 30/09/2026\n"
                "صافي الربح: 63,079,356\n")
        self.assertIsNone(fetch_feed._harvest_publish_date(body))

    def test_invalid_standalone_line_is_skipped(self):
        body = "Title\n32/13/2026\n10/11/2026\nrest\n"
        self.assertEqual(
            fetch_feed._harvest_publish_date(body).isoformat(),
            "2026-11-10")

    def test_empty_body(self):
        self.assertIsNone(fetch_feed._harvest_publish_date(""))
        self.assertIsNone(fetch_feed._harvest_publish_date(None))

    def test_single_digit_day_month(self):
        self.assertEqual(
            fetch_feed._harvest_publish_date("Title\n5/1/2026\n").isoformat(),
            "2026-01-05")


if __name__ == "__main__":
    unittest.main(verbosity=2)
