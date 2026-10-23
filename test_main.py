#!/usr/bin/env python3
"""
Automated deterministic regression and validation test suite for govt-notice-telegram-alerts.
Covers all 19 official targets, live_test mode, valid empty-board baseline completion,
unrecognized page/API rejection, multi-page deduplication, lossless state merging,
delivery retries, and secret redaction using deterministic mocks and fixtures.
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

# Ensure we import main from govt-notice-telegram-alerts if run from root or subdir
_HERE = os.path.dirname(os.path.abspath(__file__))
_SUBDIR = os.path.join(_HERE, "govt-notice-telegram-alerts")
if os.path.isfile(os.path.join(_HERE, "main.py")):
    sys.path.insert(0, _HERE)
elif os.path.isfile(os.path.join(_SUBDIR, "main.py")):
    sys.path.insert(0, _SUBDIR)

import main as app


class FakeHttpClient(app.HttpClient):
    """Deterministic HTTP client returning fixture responses per URL."""

    def __init__(
        self,
        routes: Dict[str, Any],
        max_retries: int = 3,
    ) -> None:
        self.sleep_calls: List[float] = []
        super().__init__(
            timeout=(2, 2),
            max_retries=max_retries,
            sleep_fn=lambda d: self.sleep_calls.append(d),
        )
        self.routes = routes
        self.call_counts: Dict[str, int] = {}

    def _perform_single_request(
        self,
        url: str,
        method: str,
        headers: Dict[str, str],
        data: Optional[bytes],
    ) -> Tuple[int, Dict[str, str], bytes, str]:
        self.call_counts[url] = self.call_counts.get(url, 0) + 1
        handler = self.routes.get(url)
        if handler is None:
            for k, v in self.routes.items():
                if url.startswith(k):
                    handler = v
                    break
        if handler is None:
            return 404, {}, b"Not Found", url
        if callable(handler):
            return handler(url, self.call_counts[url])
        if isinstance(handler, Exception):
            raise handler
        status, resp_headers, body = handler
        if isinstance(body, str):
            body = body.encode("utf-8")
        return status, resp_headers, body, url


class RecordingNotifier(app.TelegramNotifier):
    """Deterministic TelegramNotifier spy that captures formatted HTML messages without network calls."""

    def __init__(
        self,
        bot_token: str = "123456789:AAH_TestTokenValue1234567890",
        chat_id: str = "-1001234567890",
        fail_notice_titles: Optional[List[str]] = None,
        fail_summary: bool = False,
    ) -> None:
        super().__init__(
            bot_token=bot_token,
            chat_id=chat_id,
            max_retries=1,
            sleep_fn=lambda _: None,
        )
        self.fail_notice_titles = set(fail_notice_titles or [])
        self.fail_summary = fail_summary
        self.sent_notices: List[Tuple[Dict[str, Any], bool, str]] = []
        self.sent_summaries: List[str] = []

    def send_notice(
        self,
        notice_record: Dict[str, Any],
        is_live_test: bool = False,
    ) -> str:
        msg = app.format_telegram_message(notice_record, is_live_test=is_live_test)
        if notice_record.get("title") in self.fail_notice_titles:
            raise app.TelegramDeliveryError(
                "Simulated Telegram HTTP 502 delivery failure", permanent=False
            )
        self.sent_notices.append((dict(notice_record), is_live_test, msg))
        return msg

    def send_live_test_summary(self, summary: app.RunSummary) -> str:
        msg = app.format_live_test_summary_message(summary)
        if self.fail_summary:
            raise app.TelegramDeliveryError(
                "Simulated summary delivery failure", permanent=False
            )
        self.sent_summaries.append(msg)
        return msg


class ValidationSuite(unittest.TestCase):
    """Comprehensive deterministic tests covering all original and upgraded requirements."""

    def test_01_all_9_targets_and_cli_modes_configured(self) -> None:
        """Verify exactly 9 official active targets, CRPF/CISF/SSB exclusion, and CLI modes."""
        self.assertEqual(len(app.SOURCES), 9)
        source_ids = [s.source_id for s in app.SOURCES]
        orgs = [s.organization for s in app.SOURCES]
        urls = [s.homepage_url for s in app.SOURCES]
        self.assertEqual(len(set(source_ids)), 9)
        self.assertEqual(len(set(orgs)), 9)
        self.assertEqual(len(set(urls)), 9)

        expected_pairs = [
            ("Staff Selection Commission (SSC)", "https://ssc.gov.in/"),
            ("Union Public Service Commission (UPSC)", "https://www.upsc.gov.in/"),
            ("Uttar Pradesh Public Service Commission (UPPSC)", "https://uppsc.up.nic.in/"),
            ("Haryana Staff Selection Commission (HSSC)", "https://www.hssc.gov.in/"),
            ("Rajasthan Staff Selection Board (RSSB)", "https://rssb.rajasthan.gov.in/"),
            ("Madhya Pradesh Employees Selection Board (MPESB)", "https://esb.mp.gov.in/"),
            ("Delhi Subordinate Services Selection Board (DSSSB)", "https://dsssb.delhi.gov.in/"),
            ("Uttar Pradesh Police Recruitment and Promotion Board (UPPRPB)", "https://uppbpb.gov.in/"),
            ("Uttarakhand Public Service Commission (UKPSC)", "https://psc.uk.gov.in/"),
        ]
        actual_pairs = [(s.organization, s.homepage_url) for s in app.SOURCES]
        self.assertEqual(actual_pairs, expected_pairs)

        # Verify all 9 active adapters are registered in ADAPTER_REGISTRY and no extra adapters remain
        self.assertEqual(len(app.ADAPTER_REGISTRY), 9)
        for src in app.SOURCES:
            self.assertIn(src.adapter_name, app.ADAPTER_REGISTRY)
            self.assertTrue(len(src.notice_urls) >= 1)

        # Verify CRPF, CISF, and SSB are completely excluded from active monitoring
        for excluded_id in ("crpf", "cisf", "ssb"):
            self.assertNotIn(excluded_id, source_ids)
            self.assertNotIn(excluded_id, app.SOURCE_BY_ID)
            self.assertNotIn(f"adapter_{excluded_id}", app.ADAPTER_REGISTRY)
            self.assertFalse(hasattr(app, f"adapter_{excluded_id}"))

        # Verify all 10 removed sites (7 earlier + CRPF, CISF, SSB) are tracked in REMOVED_SOURCE_IDS
        self.assertEqual(
            app.REMOVED_SOURCE_IDS,
            frozenset(
                {
                    "upsssc",
                    "bpsc",
                    "bssc",
                    "hppsc",
                    "bsf",
                    "itbp",
                    "assam_rifles",
                    "crpf",
                    "cisf",
                    "ssb",
                }
            ),
        )
        for rem_id in app.REMOVED_SOURCE_IDS:
            self.assertNotIn(rem_id, source_ids)

        for mode in ("auto", "baseline", "monitor", "live_test"):
            parsed = app.parse_args(["--mode", mode])
            self.assertEqual(parsed.mode, mode)

    def test_02_url_and_title_normalization_and_serial_stripping(self) -> None:
        """Verify URL/title normalization, jsessionid removal, and table serial-number stripping."""
        u1 = app.normalize_url(
            "http://uppsc.up.nic.in/Open_PDF_DB.aspx;jsessionid=ABC1234?I4PnQ0tBag=1&utm_source=twitter#section2"
        )
        u2 = app.normalize_url(
            "https://UPPSC.UP.NIC.IN:443/Open_PDF_DB.aspx/?I4PnQ0tBag=1&fbclid=xyz123"
        )
        self.assertEqual(u1, u2)
        self.assertIn("I4PnQ0tBag=1", u1)
        self.assertNotIn("utm_source", u1)
        self.assertNotIn("jsessionid", u1.lower())

        # Table serial number shifts (e.g. Row 1 -> Row 2) must produce identical normalized titles
        row1 = app.normalize_title("1 18-10-2026 Public Notice regarding CET Group-D Examination (1.2 MB)")
        row2 = app.normalize_title("2 18-10-2026 Public Notice regarding CET Group-D Examination [1.2 MB]")
        row3 = app.normalize_title("S.No. 15. 18-10-2026 Public Notice regarding CET Group-D Examination")
        self.assertEqual(row1, row2)
        self.assertEqual(row1, row3)

        # Distinct notices remain distinct
        t_adv1 = app.normalize_title("Advt. No. 05/2026 Group-D Recruitment")
        t_adv2 = app.normalize_title("Advt. No. 06/2026 Group-C Recruitment")
        self.assertNotEqual(t_adv1, t_adv2)

    def test_03_fingerprint_stability_and_multi_page_deduplication(self) -> None:
        """
        Verify duplicate notices discovered across multiple pages or mirror URLs
        are sent only once, while distinct documents or titles are preserved.
        """
        # Same notice discovered on UPSC /whats-new wrapper page and /active-exams direct PDF
        n_wrapper = app.DiscoveredNotice(
            source_id="upsc",
            organization="Union Public Service Commission (UPSC)",
            title="Examination Schedule: Combined Geo-Scientist (Main) Examination, 2026",
            url="https://www.upsc.gov.in/whats-new/Geo-Scientist-2026",
        )
        n_pdf = app.DiscoveredNotice(
            source_id="upsc",
            organization="Union Public Service Commission (UPSC)",
            title="  Examination Schedule: Combined Geo-Scientist (Main) Examination, 2026 ",
            url="https://www.upsc.gov.in/sites/default/files/TimeTable-GeoScientist-2026.pdf",
            publication_date="18-10-2026",
        )
        # Distinct notice with a different official PDF document
        n_other_pdf = app.DiscoveredNotice(
            source_id="upsc",
            organization="Union Public Service Commission (UPSC)",
            title="Examination Schedule: Combined Geo-Scientist (Main) Examination, 2026",
            url="https://www.upsc.gov.in/sites/default/files/Corrigendum-GeoScientist-2026.pdf",
        )
        deduped = app.deduplicate_source_notices([n_wrapper, n_pdf, n_other_pdf])
        self.assertEqual(len(deduped), 2)
        # Prefer the direct PDF URL and retain the explicit publication date
        self.assertEqual(
            deduped[0].url,
            "https://www.upsc.gov.in/sites/default/files/TimeTable-GeoScientist-2026.pdf",
        )
        self.assertEqual(deduped[0].publication_date, "18-10-2026")

    def test_04_state_schema_validation_and_corrupt_state_handling(self) -> None:
        """Validate JSON state schema and corrupt/missing state handling."""
        initial = app.create_initial_state()
        validated = app.validate_state_schema(initial)
        self.assertEqual(validated["schema_version"], 1)
        self.assertFalse(validated["initialized"])
        self.assertEqual(len(validated["sources"]), 9)

        with tempfile.TemporaryDirectory() as tmpdir:
            missing_path = os.path.join(tmpdir, "missing.json")
            with self.assertRaises(app.StateValidationError):
                app.load_state(missing_path, allow_init_if_missing=False)

            empty_path = os.path.join(tmpdir, "empty.json")
            with open(empty_path, "w", encoding="utf-8") as f:
                f.write("   ")
            with self.assertRaises(app.StateValidationError):
                app.load_state(empty_path, allow_init_if_missing=False)

            corrupt_path = os.path.join(tmpdir, "corrupt.json")
            with open(corrupt_path, "w", encoding="utf-8") as f:
                f.write("{corrupt json")
            with self.assertRaises(app.StateValidationError):
                app.load_state(corrupt_path, allow_init_if_missing=False)

    def test_05_baseline_and_incremental_alert_lifecycle(self) -> None:
        """
        Verify normal baseline and incremental monitoring:
        - First run baselines existing notices with 0 alerts
        - Subsequent run sends 0 alerts for known notices and 1 alert per genuinely new notice
        - Metadata updates on existing notices do not trigger duplicate alerts
        - Failed Telegram delivery is retried without counting as a brand-new notice again
        """
        test_sources = (app.SOURCES[0], app.SOURCES[1])  # SSC, UPSC
        ssc_api_url = test_sources[0].notice_urls[0]
        upsc_url = test_sources[1].notice_urls[0]

        ssc_payload_v1 = {
            "statusCode": "200",
            "data": [
                {
                    "id": "ssc-1",
                    "headline": "Combined Graduate Level Examination 2026 Final Answer Key",
                    "createdAt": "",
                    "attachments": [{"path": "uploads/masterData/NoticeBoards/cgl_key_2026.pdf"}],
                }
            ],
        }
        upsc_html_v1 = """
        <html><body>
          <ul>
            <li><a href="/sites/default/files/UPSC_CSE_2026_Result.pdf">Final Result: Civil Services Examination 2026 (12 Oct 2026)</a></li>
          </ul>
        </body></html>
        """

        client_v1 = FakeHttpClient(
            routes={
                ssc_api_url: (200, {"Content-Type": "application/json"}, json.dumps(ssc_payload_v1)),
                upsc_url: (200, {"Content-Type": "text/html"}, upsc_html_v1),
            }
        )
        notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            app.save_state_atomically(app.create_initial_state(), state_file)

            # Run 1: First-run baseline
            summary1, state1 = app.run_monitor(
                state_path=state_file,
                mode="auto",
                sources=test_sources,
                http_client=client_v1,
                notifier=notifier,
            )
            self.assertEqual(summary1.succeeded_websites, 2)
            self.assertEqual(summary1.baselined_notices, 2)
            self.assertEqual(summary1.genuinely_new_notices, 0)
            self.assertEqual(len(notifier.sent_notices), 0)
            self.assertTrue(state1["sources"]["ssc"]["baseline_completed"])
            self.assertTrue(state1["sources"]["upsc"]["baseline_completed"])

            # Run 2: SSC backfills createdAt date on ssc-1; 0 new alerts should be sent
            ssc_payload_v1["data"][0]["createdAt"] = "2026-10-10T10:00:00.000Z"
            client_v1_dated = FakeHttpClient(
                routes={
                    ssc_api_url: (200, {"Content-Type": "application/json"}, json.dumps(ssc_payload_v1)),
                    upsc_url: (200, {"Content-Type": "text/html"}, upsc_html_v1),
                }
            )
            summary2, state2 = app.run_monitor(
                state_path=state_file,
                mode="auto",
                sources=test_sources,
                http_client=client_v1_dated,
                notifier=notifier,
            )
            self.assertEqual(summary2.genuinely_new_notices, 0)
            self.assertEqual(len(notifier.sent_notices), 0)
            ssc_rec = next(r for r in state2["notices"].values() if r["source_id"] == "ssc")
            self.assertEqual(ssc_rec["publication_date"], "2026-10-10")

            # Run 3: SSC adds 1 new notice while UPSC times out; Telegram fails transiently
            ssc_payload_v2 = {
                "statusCode": "200",
                "data": [
                    {
                        "id": "ssc-2",
                        "headline": "Junior Engineer Examination 2026 Admit Card Notice",
                        "createdAt": "2026-10-18T08:00:00.000Z",
                        "attachments": [{"path": "uploads/masterData/NoticeBoards/je_admit_2026.pdf"}],
                    },
                    ssc_payload_v1["data"][0],
                ],
            }
            client_v2 = FakeHttpClient(
                routes={
                    ssc_api_url: (200, {"Content-Type": "application/json"}, json.dumps(ssc_payload_v2)),
                    upsc_url: app.SourceFetchError("CONNECTION_TIMEOUT", "UPSC timed out"),
                }
            )
            failing_notifier = RecordingNotifier(
                fail_notice_titles=["Junior Engineer Examination 2026 Admit Card Notice"]
            )
            summary3, state3 = app.run_monitor(
                state_path=state_file,
                mode="auto",
                sources=test_sources,
                http_client=client_v2,
                notifier=failing_notifier,
            )
            self.assertEqual(summary3.genuinely_new_notices, 1)
            self.assertEqual(summary3.telegram_delivery_failures, 1)
            self.assertEqual(summary3.telegram_deliveries_confirmed, 0)

            # Run 4: Telegram recovers -> retries failed delivery without counting as brand-new
            summary4, state4 = app.run_monitor(
                state_path=state_file,
                mode="auto",
                sources=test_sources,
                http_client=client_v2,
                notifier=notifier,
            )
            self.assertEqual(summary4.genuinely_new_notices, 0)
            self.assertEqual(summary4.telegram_deliveries_confirmed, 1)
            self.assertEqual(len(notifier.sent_notices), 1)
            sent_rec, is_test_flag, msg_text = notifier.sent_notices[0]
            self.assertFalse(is_test_flag)
            self.assertNotIn("[LIVE TEST]", msg_text)
            self.assertEqual(sent_rec["title"], "Junior Engineer Examination 2026 Admit Card Notice")

            # Run 5: Repeated run does not resend delivered or baselined notices
            summary5, _ = app.run_monitor(
                state_path=state_file,
                mode="auto",
                sources=test_sources,
                http_client=client_v2,
                notifier=notifier,
            )
            self.assertEqual(summary5.genuinely_new_notices, 0)
            self.assertEqual(summary5.telegram_deliveries_confirmed, 0)
            self.assertEqual(len(notifier.sent_notices), 1)

    def test_06_live_test_mode_end_to_end_and_subsequent_monitor_isolation(self) -> None:
        """
        Verify live_test mode requirements:
        - Selects the latest notice (preferring explicit publication date & source ordering)
        - Sends [LIVE TEST]-prefixed message for latest notice from each successful source
        - Does NOT send ordinary pending alerts during live_test
        - Baselines all discovered notices for successful sources (including the tested latest notice)
        - Preserves failed sources as unbaselined
        - Subsequent normal monitoring run sends 0 alerts for notices discovered during live_test
        - Sends final Telegram test summary with all required counters
        """
        ssc_src = app.SOURCE_BY_ID["ssc"]
        upsc_src = app.SOURCE_BY_ID["upsc"]
        uppsc_src = app.SOURCE_BY_ID["uppsc"]
        test_sources = (ssc_src, upsc_src, uppsc_src)

        # SSC has 2 notices: index 0 has older date (2026-10-10), index 1 has newer explicit date (2026-10-18)
        ssc_payload = {
            "statusCode": "200",
            "data": [
                {
                    "id": "ssc-old",
                    "headline": "Older SSC Notice from October 10",
                    "createdAt": "2026-10-10T09:00:00.000Z",
                    "attachments": [{"path": "uploads/old_notice.pdf"}],
                },
                {
                    "id": "ssc-latest",
                    "headline": "Latest SSC Constable GD Examination Notice 2026",
                    "createdAt": "2026-10-18T11:30:00.000Z",
                    "attachments": [{"path": "uploads/latest_gd_2026.pdf"}],
                },
            ],
        }
        # UPSC has 2 notices without explicit dates -> must pick first in source order and NOT invent a date
        upsc_html = """
        <html><body>
          <h2>What's New</h2>
          <ul>
            <li><a href="/sites/default/files/UPSC_Top_Notice.pdf">UPSC Civil Services Main Examination Schedule</a></li>
            <li><a href="/sites/default/files/UPSC_Second_Notice.pdf">UPSC Engineering Services Reserve List</a></li>
          </ul>
        </body></html>
        """

        client = FakeHttpClient(
            routes={
                ssc_src.notice_urls[0]: (200, {"Content-Type": "application/json"}, json.dumps(ssc_payload)),
                upsc_src.notice_urls[0]: (200, {"Content-Type": "text/html"}, upsc_html),
                # UPPSC fails with timeout
                uppsc_src.notice_urls[0]: app.SourceFetchError("CONNECTION_TIMEOUT", "UPPSC timed out"),
                uppsc_src.notice_urls[1]: app.SourceFetchError("CONNECTION_TIMEOUT", "UPPSC timed out"),
            }
        )
        notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            app.save_state_atomically(app.create_initial_state(), state_file)

            summary_lt, state_lt = app.run_monitor(
                state_path=state_file,
                mode="live_test",
                sources=test_sources,
                http_client=client,
                notifier=notifier,
            )

            self.assertEqual(summary_lt.total_websites, 3)
            self.assertEqual(summary_lt.succeeded_websites, 2)
            self.assertEqual(summary_lt.failed_websites, 1)
            self.assertEqual(summary_lt.websites_with_notices, 2)
            self.assertEqual(summary_lt.test_messages_delivered, 2)
            self.assertEqual(summary_lt.telegram_delivery_failures, 0)
            self.assertTrue(summary_lt.summary_message_delivered)

            # Exactly 2 [LIVE TEST] notice messages + 1 summary message sent
            self.assertEqual(len(notifier.sent_notices), 2)
            self.assertEqual(len(notifier.sent_summaries), 1)

            # Check SSC message selected the newest explicit date (2026-10-18)
            ssc_sent_rec, ssc_is_test, ssc_msg = notifier.sent_notices[0]
            self.assertTrue(ssc_is_test)
            self.assertTrue(ssc_msg.startswith("[LIVE TEST]"))
            self.assertEqual(ssc_sent_rec["title"], "Latest SSC Constable GD Examination Notice 2026")
            self.assertEqual(ssc_sent_rec["publication_date"], "2026-10-18")
            self.assertIn("2026-10-18", ssc_msg)
            self.assertIn("https://ssc.gov.in/api/attachment/uploads/latest_gd_2026.pdf", ssc_msg)

            # Check UPSC message selected first item in source order and did NOT fabricate a publication date
            upsc_sent_rec, upsc_is_test, upsc_msg = notifier.sent_notices[1]
            self.assertTrue(upsc_is_test)
            self.assertTrue(upsc_msg.startswith("[LIVE TEST]"))
            self.assertEqual(upsc_sent_rec["title"], "UPSC Civil Services Main Examination Schedule")
            self.assertIsNone(upsc_sent_rec["publication_date"])
            self.assertNotIn("Publication Date:", upsc_msg)

            # Check summary message contents
            summary_msg = notifier.sent_summaries[0]
            self.assertIn("[LIVE TEST]", summary_msg)
            self.assertIn("Total websites:</b> 3", summary_msg)
            self.assertIn("Successful website fetches and parsing:</b> 2", summary_msg)
            self.assertIn("Failed websites:</b> 1", summary_msg)
            self.assertIn("Websites with notices found:</b> 2", summary_msg)
            self.assertIn("Test messages delivered successfully:</b> 2", summary_msg)
            self.assertIn("Telegram delivery failures:</b> 0", summary_msg)
            self.assertIn("Websites requiring adapter review:</b> 0", summary_msg)

            # All 4 discovered notices must be recorded as 'baselined' in state
            self.assertEqual(len(state_lt["notices"]), 4)
            for rec in state_lt["notices"].values():
                self.assertEqual(rec["delivery_status"], "baselined")

            # SSC and UPSC completed baseline; failed UPPSC remains unbaselined
            self.assertTrue(state_lt["sources"]["ssc"]["baseline_completed"])
            self.assertTrue(state_lt["sources"]["upsc"]["baseline_completed"])
            self.assertFalse(state_lt["sources"]["uppsc"]["baseline_completed"])

            # Next normal monitoring run MUST NOT alert on any of the notices discovered during live_test
            notifier_next = RecordingNotifier()
            summary_next, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=test_sources,
                http_client=client,
                notifier=notifier_next,
            )
            self.assertEqual(summary_next.genuinely_new_notices, 0)
            self.assertEqual(summary_next.telegram_deliveries_confirmed, 0)
            self.assertEqual(len(notifier_next.sent_notices), 0)

    def test_07_live_test_telegram_failure_is_not_queued_as_normal_alert(self) -> None:
        """
        Verify that if a [LIVE TEST] Telegram message fails to send:
        - The failure is counted and recorded in test_delivery_status='failed'
        - The notice's normal delivery_status remains 'baselined'
        - The next normal scheduled monitor run does NOT send that old notice as a new alert
        """
        ssc_src = app.SOURCE_BY_ID["ssc"]
        ssc_payload = {
            "statusCode": "200",
            "data": [
                {
                    "id": "ssc-100",
                    "headline": "SSC CHSL 2026 Tier-I Result Declaration Notice",
                    "createdAt": "2026-10-15T10:00:00.000Z",
                    "attachments": [{"path": "uploads/chsl_2026.pdf"}],
                }
            ],
        }
        client = FakeHttpClient(
            routes={
                ssc_src.notice_urls[0]: (200, {"Content-Type": "application/json"}, json.dumps(ssc_payload)),
            }
        )
        failing_test_notifier = RecordingNotifier(
            fail_notice_titles=["SSC CHSL 2026 Tier-I Result Declaration Notice"]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            app.save_state_atomically(app.create_initial_state(), state_file)

            summary_lt, state_lt = app.run_monitor(
                state_path=state_file,
                mode="live_test",
                sources=(ssc_src,),
                http_client=client,
                notifier=failing_test_notifier,
            )
            self.assertEqual(summary_lt.test_messages_delivered, 0)
            self.assertEqual(summary_lt.telegram_delivery_failures, 1)

            rec = next(iter(state_lt["notices"].values()))
            self.assertEqual(rec["delivery_status"], "baselined")
            self.assertEqual(rec["test_delivery_status"], "failed")

            # Subsequent normal monitoring run must NOT send this baselined notice
            healthy_notifier = RecordingNotifier()
            summary_mon, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(ssc_src,),
                http_client=client,
                notifier=healthy_notifier,
            )
            self.assertEqual(summary_mon.genuinely_new_notices, 0)
            self.assertEqual(len(healthy_notifier.sent_notices), 0)

    def test_08_valid_empty_notice_board_vs_unrecognized_page_or_invalid_api(self) -> None:
        """
        Verify:
        - A valid empty notice board (API data=[] or recognized empty HTML table) completes baseline
          and reports NO_NOTICE_FOUND in live_test mode.
        - An unrecognized HTML page, access-denied page, or invalid API schema does NOT complete
          baseline and reports REVIEW_REQUIRED.
        """
        ssc_src = app.SOURCE_BY_ID["ssc"]
        upsc_src = app.SOURCE_BY_ID["upsc"]
        uppsc_src = app.SOURCE_BY_ID["uppsc"]
        hssc_src = app.SOURCE_BY_ID["hssc"]

        valid_empty_html = """
        <html><body>
          <h2>Public Notice Board</h2>
          <table>
            <thead><tr><th>S.No.</th><th>Date</th><th>Subject</th><th>Download</th></tr></thead>
            <tbody><tr><td colspan="4">No notices available at present.</td></tr></tbody>
          </table>
        </body></html>
        """
        unrecognized_html = """
        <html><body>
          <h1>Welcome to Unknown Portal Layout</h1>
          <p>Some unrelated text with zero notice board structure.</p>
        </body></html>
        """
        invalid_ssc_api = {"unexpectedKey": "bad_schema"}

        client = FakeHttpClient(
            routes={
                # SSC returns invalid API response and homepage is unrecognized
                ssc_src.notice_urls[0]: (200, {"Content-Type": "application/json"}, json.dumps(invalid_ssc_api)),
                ssc_src.notice_urls[1]: (200, {"Content-Type": "text/html"}, unrecognized_html),
                # UPSC returns a valid recognized empty notice board
                upsc_src.notice_urls[0]: (200, {"Content-Type": "text/html"}, valid_empty_html),
                # UPPSC returns an unrecognized page
                uppsc_src.notice_urls[0]: (200, {"Content-Type": "text/html"}, unrecognized_html),
                uppsc_src.notice_urls[1]: (200, {"Content-Type": "text/html"}, unrecognized_html),
                # HSSC returns an access-denied block page
                hssc_src.notice_urls[0]: (200, {"Content-Type": "text/html"}, "<html><body><h1>403 Forbidden - Access Denied</h1></body></html>"),
            }
        )
        notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            app.save_state_atomically(app.create_initial_state(), state_file)

            summary, state = app.run_monitor(
                state_path=state_file,
                mode="live_test",
                sources=(ssc_src, upsc_src, uppsc_src, hssc_src),
                http_client=client,
                notifier=notifier,
            )

            # Only UPSC (valid empty board) succeeded and completed its baseline
            self.assertEqual(summary.succeeded_websites, 1)
            self.assertEqual(summary.failed_websites, 3)
            self.assertEqual(summary.websites_with_no_notices, 1)
            self.assertEqual(summary.websites_requiring_review, 3)

            self.assertTrue(state["sources"]["upsc"]["baseline_completed"])
            self.assertEqual(summary.per_source_results["upsc"]["test_outcome"], "NO_NOTICE_FOUND")

            self.assertFalse(state["sources"]["ssc"]["baseline_completed"])
            self.assertEqual(summary.per_source_results["ssc"]["test_outcome"], "REVIEW_REQUIRED")

            self.assertFalse(state["sources"]["uppsc"]["baseline_completed"])
            self.assertEqual(summary.per_source_results["uppsc"]["test_outcome"], "REVIEW_REQUIRED")

            self.assertFalse(state["sources"]["hssc"]["baseline_completed"])
            self.assertEqual(summary.per_source_results["hssc"]["test_outcome"], "REVIEW_REQUIRED")

    def test_09_lossless_state_merge_preserves_fingerprints_and_delivered_status(self) -> None:
        """Verify merge_states never discards fingerprints or downgrades delivered/baselined notices."""
        base = app.create_initial_state()
        inc = app.create_initial_state()

        base["initialized"] = True
        base["sources"]["ssc"]["baseline_completed"] = True
        base["notices"]["fp_1"] = {
            "fingerprint": "fp_1",
            "source_id": "ssc",
            "organization": "Staff Selection Commission (SSC)",
            "title": "Notice 1",
            "normalized_url": "https://ssc.gov.in/1.pdf",
            "first_seen_at": "2026-10-18T01:00:00Z",
            "delivery_status": "delivered",
            "delivered_at": "2026-10-18T01:01:00Z",
        }

        inc["initialized"] = True
        inc["sources"]["upsc"]["baseline_completed"] = True
        # fp_1 is only 'pending' in inc, while fp_2 is newly baselined in inc
        inc["notices"]["fp_1"] = {
            "fingerprint": "fp_1",
            "source_id": "ssc",
            "organization": "Staff Selection Commission (SSC)",
            "title": "Notice 1",
            "normalized_url": "https://ssc.gov.in/1.pdf",
            "first_seen_at": "2026-10-18T02:00:00Z",
            "delivery_status": "pending",
        }
        inc["notices"]["fp_2"] = {
            "fingerprint": "fp_2",
            "source_id": "upsc",
            "organization": "Union Public Service Commission (UPSC)",
            "title": "Notice 2",
            "normalized_url": "https://www.upsc.gov.in/2.pdf",
            "first_seen_at": "2026-10-18T02:00:00Z",
            "delivery_status": "baselined",
        }

        merged = app.merge_states(base, inc)
        self.assertTrue(merged["sources"]["ssc"]["baseline_completed"])
        self.assertTrue(merged["sources"]["upsc"]["baseline_completed"])
        self.assertEqual(len(merged["notices"]), 2)
        self.assertEqual(merged["notices"]["fp_1"]["delivery_status"], "delivered")
        self.assertEqual(merged["notices"]["fp_2"]["delivery_status"], "baselined")

    def test_10_retry_limits_and_http_error_handling(self) -> None:
        """Test retry limits, Retry-After handling, and non-retryable 403/404."""
        client_503 = FakeHttpClient(
            routes={
                "https://example.gov.in/503": (503, {"Retry-After": "1"}, "Service Unavailable"),
                "https://example.gov.in/403": (403, {}, "Forbidden"),
            },
            max_retries=3,
        )
        with self.assertRaises(app.SourceFetchError) as ctx503:
            client_503.fetch_text("https://example.gov.in/503")
        self.assertEqual(ctx503.exception.category, "HTTP_503_SERVER_ERROR")
        self.assertEqual(client_503.call_counts["https://example.gov.in/503"], 3)

        with self.assertRaises(app.SourceFetchError) as ctx403:
            client_503.fetch_text("https://example.gov.in/403")
        self.assertEqual(ctx403.exception.category, "HTTP_403_FORBIDDEN")
        self.assertEqual(client_503.call_counts["https://example.gov.in/403"], 1)

    def test_11_secret_redaction_and_missing_env_vars(self) -> None:
        """Test missing environment variables in monitor & live_test modes and secret redaction."""
        secret_token = "123456789:AAH_SecretBotTokenValueXYZ987654"
        secret_chat = "-1009876543210"

        stream = io.StringIO()
        logger = logging.getLogger("test_redaction")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        handler = logging.StreamHandler(stream)
        redactor = app.SecretRedactingFilter([secret_token, secret_chat])
        handler.addFilter(redactor)
        logger.addHandler(handler)

        logger.error(
            "Error calling https://api.telegram.org/bot%s/sendMessage for chat %s",
            secret_token,
            secret_chat,
        )
        output = stream.getvalue()
        self.assertNotIn(secret_token, output)
        self.assertNotIn(secret_chat, output)
        self.assertIn("[REDACTED]", output)

        with tempfile.TemporaryDirectory() as tmpdir:
            st_path = os.path.join(tmpdir, "state.json")
            st = app.create_initial_state()
            st["initialized"] = True
            app.save_state_atomically(st, st_path)

            with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}, clear=False):
                self.assertEqual(app.main(["--mode", "monitor", "--state-file", st_path]), 3)
                self.assertEqual(app.main(["--mode", "live_test", "--state-file", st_path]), 3)

    def test_12_workflow_yaml_structure_and_live_test_mode(self) -> None:
        """Validate GitHub Actions YAML structure, 10-min cron, live_test option, and lossless merge."""
        workflow_path = os.path.join(
            os.path.dirname(os.path.abspath(app.__file__)),
            ".github",
            "workflows",
            "main.yml",
        )
        self.assertTrue(os.path.exists(workflow_path))
        with open(workflow_path, "r", encoding="utf-8") as fh:
            content = fh.read()

        self.assertIn('cron: "*/10 * * * *"', content)
        self.assertIn("workflow_dispatch:", content)
        self.assertIn("- live_test", content)
        self.assertIn("concurrency:", content)
        self.assertIn("cancel-in-progress: false", content)
        self.assertIn("contents: write", content)
        self.assertIn("secrets.TELEGRAM_BOT_TOKEN", content)
        self.assertIn("secrets.TELEGRAM_CHAT_ID", content)
        self.assertIn("git diff --quiet -- data/notice_state.json", content)
        self.assertIn("--merge-state", content)
        self.assertNotIn("-X theirs", content)
        self.assertNotIn("--force", content)

    def test_13_all_9_adapters_and_live_test_pending_isolation(self) -> None:
        """
        Verify all 9 active adapters parse realistic fixtures and that live_test mode
        never sends ordinary pending notices that were sitting in state.
        """
        routes: Dict[str, Any] = {}
        for src in app.SOURCES:
            if src.source_id == "ssc":
                routes[src.notice_urls[0]] = (
                    200,
                    {"Content-Type": "application/json"},
                    json.dumps(
                        {
                            "statusCode": "200",
                            "data": [
                                {
                                    "id": "ssc-99",
                                    "headline": "SSC Combined Graduate Level Examination 2026 Schedule",
                                    "createdAt": "2026-10-18T10:00:00Z",
                                    "attachments": [{"path": "uploads/ssc_cgl_2026.pdf"}],
                                }
                            ],
                        }
                    ),
                )
            elif src.source_id == "uppsc":
                routes[src.notice_urls[0]] = (
                    200,
                    {"Content-Type": "text/html"},
                    """
                    <html><body><table>
                      <tr><td>1</td><td><a href=Open_PDF_DB.aspx?I4PnQ0tBag=99 title="UPPSC Combined State Upper Subordinate Exam 2026 Notification">UPPSC Exam...</a></td><td>Visible upto 31 Dec 2026</td></tr>
                    </table></body></html>
                    """,
                )
            else:
                routes[src.notice_urls[0]] = (
                    200,
                    {"Content-Type": "text/html"},
                    f"""
                    <html><body>
                      <h2>Notice Board - {src.organization}</h2>
                      <table>
                        <tr><td>1</td><td>18-10-2026</td><td><a href="/notices/{src.source_id}_advt_2026.pdf">Official Recruitment and Examination Notice 2026 for {src.source_id.upper()}</a></td></tr>
                      </table>
                    </body></html>
                    """,
                )

        client = FakeHttpClient(routes=routes)
        notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            st = app.create_initial_state()
            # Seed a pre-existing unrelated 'pending' notice from an earlier run
            st["notices"]["preexisting_pending_fp"] = {
                "fingerprint": "preexisting_pending_fp",
                "source_id": "ssc",
                "organization": "Staff Selection Commission (SSC)",
                "title": "Old Unsent Pending Notice From Past Run",
                "normalized_url": "https://ssc.gov.in/old_unsent.pdf",
                "first_seen_at": "2026-01-01T00:00:00Z",
                "delivery_status": "pending",
            }
            app.save_state_atomically(st, state_file)

            summary, final_state = app.run_monitor(
                state_path=state_file,
                mode="live_test",
                sources=app.SOURCES,
                http_client=client,
                notifier=notifier,
            )

            self.assertEqual(summary.total_websites, 9)
            self.assertEqual(summary.succeeded_websites, 9)
            self.assertEqual(summary.failed_websites, 0)
            self.assertEqual(summary.websites_with_notices, 9)
            self.assertEqual(summary.test_messages_delivered, 9)
            self.assertTrue(summary.summary_message_delivered)

            # Verify all 9 sent messages have [LIVE TEST] prefix and the old pending notice was NOT sent
            self.assertEqual(len(notifier.sent_notices), 9)
            for rec, is_test, msg in notifier.sent_notices:
                self.assertTrue(is_test)
                self.assertTrue(msg.startswith("[LIVE TEST]"))
                self.assertNotEqual(rec["title"], "Old Unsent Pending Notice From Past Run")

            # Verify all 9 sources are marked baseline_completed=True
            for src in app.SOURCES:
                self.assertTrue(final_state["sources"][src.source_id]["baseline_completed"])

    def test_14_trailing_new_badges_and_meaningful_details_preserved(self) -> None:
        """
        Verify trailing 'New'/'[NEW]'/'(New)' badges and leading serial numbers are
        stripped while post codes, advt numbers, years, and corrigendum numbers remain distinct.
        """
        t_base = app.normalize_title(" Combined Graduate Level Exam 2026 (Advt No. 04/2026) ")
        t_new1 = app.normalize_title("1. Combined Graduate Level Exam 2026 (Advt No. 04/2026) New")
        t_new2 = app.normalize_title("(12) Combined Graduate Level Exam 2026 (Advt No. 04/2026) [NEW!]")
        t_new3 = app.normalize_title("S.No. 5: Combined Graduate Level Exam 2026 (Advt No. 04/2026) (New) (1.5 MB)")
        self.assertEqual(t_base, t_new1)
        self.assertEqual(t_base, t_new2)
        self.assertEqual(t_base, t_new3)

        # Meaningful recruitment identifiers must remain distinct
        self.assertNotEqual(
            app.normalize_title("Recruitment for Post Code 41/26 in DSSSB"),
            app.normalize_title("Recruitment for Post Code 42/26 in DSSSB"),
        )
        self.assertNotEqual(
            app.normalize_title("Corrigendum-1 to Advt. No. 05/2025"),
            app.normalize_title("Corrigendum-2 to Advt. No. 05/2025"),
        )
        self.assertNotEqual(
            app.normalize_title("Combined State Exam 2025 Final Result"),
            app.normalize_title("Combined State Exam 2026 Final Result"),
        )

    def test_15_expired_and_historical_notices_suppressed_in_monitor_mode(self) -> None:
        """
        Verify expired notices ('Visible upto ...', 'Valid till ...' in the past) and
        old historical notices are baselined rather than sent as new production alerts.
        """
        self.assertEqual(
            app.extract_expiry_date("Notice for Exam 2025 Visible upto : 15/08/2025"),
            "15/08/2025",
        )
        self.assertTrue(
            app.is_notice_expired(
                "Notice for Exam 2025 Visible upto : 15/08/2025",
                reference_date=app.datetime.date(2026, 10, 19),
            )
        )
        self.assertFalse(
            app.is_notice_expired(
                "Notice for Exam 2026 Visible upto : 31/12/2026",
                reference_date=app.datetime.date(2026, 10, 19),
            )
        )

        upsc_src = app.SOURCE_BY_ID["upsc"]
        initial_html = """
        <html><body><h2>What's New</h2><ul>
          <li><a href="/whats-new/notice_1.pdf">18-10-2026 Active Notice One for Examination 2026</a></li>
        </ul></body></html>
        """
        # Second run adds 1 expired notice, 1 archival 2023 notice, and 1 genuinely new 2026 notice
        second_html = """
        <html><body><h2>What's New</h2><ul>
          <li><a href="/whats-new/notice_fresh.pdf">19-10-2026 Genuinely New Examination Schedule 2026</a></li>
          <li><a href="/whats-new/notice_1.pdf">18-10-2026 Active Notice One for Examination 2026</a></li>
          <li><a href="/whats-new/notice_exp.pdf">Old Exam Notice Valid till 15-08-2025</a></li>
          <li><a href="/whats-new/notice_arch.pdf">10-01-2023 Archival Recruitment Result 2023</a></li>
        </ul></body></html>
        """
        routes: Dict[str, Any] = {
            upsc_src.notice_urls[0]: (200, {"Content-Type": "text/html"}, initial_html),
        }
        client = FakeHttpClient(routes=routes)
        notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            app.save_state_atomically(app.create_initial_state(), state_file)

            # Baseline run
            app.run_monitor(
                state_path=state_file,
                mode="baseline",
                sources=(upsc_src,),
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 18),
            )
            self.assertEqual(len(notifier.sent_notices), 0)

            # Incremental monitor run with expired + historical + 1 fresh notice
            routes[upsc_src.notice_urls[0]] = (200, {"Content-Type": "text/html"}, second_html)
            summary_mon, state_mon = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(upsc_src,),
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 19),
            )
            self.assertEqual(summary_mon.genuinely_new_notices, 1)
            self.assertEqual(summary_mon.telegram_deliveries_confirmed, 1)
            self.assertEqual(len(notifier.sent_notices), 1)
            self.assertIn("Genuinely New Examination Schedule 2026", notifier.sent_notices[0][0]["title"])
            # All 4 notices are now stored in state (1 delivered, 3 baselined)
            self.assertEqual(len(state_mon["notices"]), 4)

    def test_16_stale_failed_notices_and_ambiguous_post_send_timeout_not_resent(self) -> None:
        """
        Verify:
        - Previously failed notices that are stale (>24h old or >= MAX_NOTICE_DELIVERY_ATTEMPTS)
          are transitioned to 'baselined' and never resent.
        - An ambiguous post-send timeout (TelegramDeliveryError(ambiguous=True)) marks the
          notice as 'delivered' so it is not blindly resent as a duplicate.
        """
        ssc_src = app.SOURCE_BY_ID["ssc"]
        ssc_payload = {
            "statusCode": "200",
            "data": [
                {
                    "id": "ssc-new-ambig",
                    "headline": "SSC New Recruitment Notice With Ambiguous Timeout 2026",
                    "createdAt": "2026-10-19T08:00:00Z",
                    "attachments": [{"path": "uploads/ambig_2026.pdf"}],
                }
            ],
        }
        client = FakeHttpClient(
            routes={
                ssc_src.notice_urls[0]: (200, {"Content-Type": "application/json"}, json.dumps(ssc_payload)),
            }
        )

        class AmbiguousOnceNotifier(RecordingNotifier):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def send_notice(self, notice_record: Dict[str, Any], is_live_test: bool = False) -> str:
                self.calls += 1
                if self.calls == 1:
                    raise app.TelegramDeliveryError(
                        "Ambiguous post-send read timeout", permanent=False, ambiguous=True
                    )
                return super().send_notice(notice_record, is_live_test=is_live_test)

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            st = app.create_initial_state()
            st["initialized"] = True
            st["sources"]["ssc"]["baseline_completed"] = True
            st["sources"]["ssc"]["baseline_completed_at"] = "2026-10-01T00:00:00Z"
            # Seed a stale failed notice from 5 days ago
            st["notices"]["stale_failed_fp"] = {
                "fingerprint": "stale_failed_fp",
                "source_id": "ssc",
                "organization": "Staff Selection Commission (SSC)",
                "title": "Stale Failed Notice From Five Days Ago",
                "normalized_url": "https://ssc.gov.in/stale_failed.pdf",
                "first_seen_at": "2026-10-10T00:00:00Z",
                "delivery_status": "failed",
                "delivery_attempts": 1,
            }
            app.save_state_atomically(st, state_file)

            ambig_notifier = AmbiguousOnceNotifier()
            summary1, state1 = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(ssc_src,),
                http_client=client,
                notifier=ambig_notifier,
            )
            # Stale failed notice must have been suppressed to 'baselined' without calling notifier
            self.assertEqual(state1["notices"]["stale_failed_fp"]["delivery_status"], "baselined")
            # Ambiguous notice was attempted once and marked 'delivered' to prevent duplicate resend
            self.assertEqual(ambig_notifier.calls, 1)
            for fp, rec in state1["notices"].items():
                if fp != "stale_failed_fp":
                    self.assertEqual(rec["delivery_status"], "delivered")

            # Run monitor again: 0 calls to notifier!
            summary2, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(ssc_src,),
                http_client=client,
                notifier=ambig_notifier,
            )
            self.assertEqual(ambig_notifier.calls, 1)
            self.assertEqual(summary2.telegram_deliveries_confirmed, 0)

    def test_17_stale_state_cannot_overwrite_newer_sent_or_reintroduce_old_pending(self) -> None:
        """
        Verify merge_states preserves 'delivered' ('sent') records even when merged with
        a stale state file, and promotes stale 'pending' records that predate baseline_completed_at.
        """
        newer = app.create_initial_state()
        stale = app.create_initial_state()

        newer["initialized"] = True
        newer["sources"]["ssc"]["baseline_completed"] = True
        newer["sources"]["ssc"]["baseline_completed_at"] = "2026-10-19T08:00:00Z"
        newer["notices"]["fp_sent"] = {
            "fingerprint": "fp_sent",
            "source_id": "ssc",
            "organization": "Staff Selection Commission (SSC)",
            "title": "Already Sent Notice",
            "normalized_url": "https://ssc.gov.in/sent.pdf",
            "first_seen_at": "2026-10-19T08:05:00Z",
            "delivery_status": "sent",
            "delivered_at": "2026-10-19T08:05:05Z",
        }

        stale["initialized"] = True
        stale["sources"]["ssc"]["baseline_completed"] = False
        stale["notices"]["fp_sent"] = {
            "fingerprint": "fp_sent",
            "source_id": "ssc",
            "organization": "Staff Selection Commission (SSC)",
            "title": "Already Sent Notice",
            "normalized_url": "https://ssc.gov.in/sent.pdf",
            "first_seen_at": "2026-10-19T08:05:00Z",
            "delivery_status": "pending",
        }
        stale["notices"]["fp_old_pending"] = {
            "fingerprint": "fp_old_pending",
            "source_id": "ssc",
            "organization": "Staff Selection Commission (SSC)",
            "title": "Old Pending Notice From Before Baseline",
            "normalized_url": "https://ssc.gov.in/old_before_baseline.pdf",
            "first_seen_at": "2026-10-19T07:50:00Z",
            "delivery_status": "pending",
        }

        merged_a = app.merge_states(newer, stale)
        merged_b = app.merge_states(stale, newer)
        for m in (merged_a, merged_b):
            self.assertEqual(m["notices"]["fp_sent"]["delivery_status"], "delivered")
            self.assertEqual(m["notices"]["fp_old_pending"]["delivery_status"], "baselined")

    def test_18_state_retention_policy_preserves_fingerprints(self) -> None:
        """
        Verify apply_state_retention_policy compacts verbose fields above max_full_records
        without discarding notice fingerprints needed for duplicate protection.
        """
        st = app.create_initial_state()
        st["initialized"] = True
        for i in range(15):
            fp = f"fp_{i:04d}"
            st["notices"][fp] = {
                "fingerprint": fp,
                "source_id": "ssc",
                "organization": "Staff Selection Commission (SSC)",
                "title": f"Notice Title {i}",
                "normalized_title": f"notice title {i}",
                "normalized_url": f"https://ssc.gov.in/{i}.pdf",
                "first_seen_at": f"2026-10-19T00:{i:02d}:00Z",
                "last_seen_at": f"2026-10-19T00:{i:02d}:00Z",
                "delivery_status": "delivered",
                "last_delivery_error": None,
                "test_delivered_at": "2026-10-19T00:00:00Z",
                "test_delivery_attempts": 1,
            }
        app.apply_state_retention_policy(st, max_full_records=5, max_total_fingerprints=100)
        # All 15 fingerprints must still be present
        self.assertEqual(len(st["notices"]), 15)
        # Older compacted records still pass schema validation and retain 'delivered' status
        app.validate_state_schema(st)
        self.assertNotIn("test_delivered_at", st["notices"]["fp_0000"])
        self.assertEqual(st["notices"]["fp_0000"]["delivery_status"], "delivered")

    def test_19_direct_document_url_identity_matching_across_title_edits(self) -> None:
        """
        Verify that if an already-baselined notice has a specific PDF URL and its
        link text is later edited on the portal while pointing to the same PDF,
        it is matched to the existing record and does not trigger a duplicate alert.
        """
        upsc_src = app.SOURCE_BY_ID["upsc"]
        html_v1 = """
        <html><body><h2>What's New</h2><ul>
          <li><a href="/sites/default/files/Advt_No_12_2026.pdf">Recruitment Advertisement No. 12/2026 for Specialist Grade III</a></li>
        </ul></body></html>
        """
        html_v2 = """
        <html><body><h2>What's New</h2><ul>
          <li><a href="/sites/default/files/Advt_No_12_2026.pdf">Revised Title: Recruitment Advertisement No. 12/2026 for Specialist Grade-III Officers</a></li>
        </ul></body></html>
        """
        routes: Dict[str, Any] = {
            upsc_src.notice_urls[0]: (200, {"Content-Type": "text/html"}, html_v1),
        }
        client = FakeHttpClient(routes=routes)
        notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            app.save_state_atomically(app.create_initial_state(), state_file)

            app.run_monitor(
                state_path=state_file,
                mode="baseline",
                sources=(upsc_src,),
                http_client=client,
                notifier=notifier,
            )
            routes[upsc_src.notice_urls[0]] = (200, {"Content-Type": "text/html"}, html_v2)
            summary_mon, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(upsc_src,),
                http_client=client,
                notifier=notifier,
            )
            self.assertEqual(summary_mon.genuinely_new_notices, 0)
            self.assertEqual(len(notifier.sent_notices), 0)

    def test_20_repeated_manual_and_scheduled_runs_zero_duplicates(self) -> None:
        """
        Verify a full sequence of baseline -> auto -> live_test -> monitor -> auto
        runs produces zero duplicate production alerts.
        """
        ssc_src = app.SOURCE_BY_ID["ssc"]
        payload_1 = {
            "statusCode": "200",
            "data": [
                {
                    "id": "ssc-1",
                    "headline": "SSC Multi Tasking Staff Examination 2026 Notice",
                    "createdAt": "2026-10-18T10:00:00Z",
                    "attachments": [{"path": "uploads/mts_2026.pdf"}],
                }
            ],
        }
        payload_2 = {
            "statusCode": "200",
            "data": [
                {
                    "id": "ssc-2",
                    "headline": "SSC Junior Engineer Examination 2026 Paper-II Schedule",
                    "createdAt": "2026-10-19T09:00:00Z",
                    "attachments": [{"path": "uploads/je_2026.pdf"}],
                },
                {
                    "id": "ssc-1",
                    "headline": "SSC Multi Tasking Staff Examination 2026 Notice",
                    "createdAt": "2026-10-18T10:00:00Z",
                    "attachments": [{"path": "uploads/mts_2026.pdf"}],
                },
            ],
        }
        routes: Dict[str, Any] = {
            ssc_src.notice_urls[0]: (200, {"Content-Type": "application/json"}, json.dumps(payload_1)),
        }
        client = FakeHttpClient(routes=routes)
        prod_notifier = RecordingNotifier()
        test_notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            app.save_state_atomically(app.create_initial_state(), state_file)

            # 1. Initial auto run (baselines payload_1, 0 prod alerts)
            s1, _ = app.run_monitor(state_path=state_file, mode="auto", sources=(ssc_src,), http_client=client, notifier=prod_notifier)
            self.assertEqual(s1.mode, "baseline")
            self.assertEqual(len(prod_notifier.sent_notices), 0)

            # 2. Second auto run with unchanged data (0 prod alerts)
            s2, _ = app.run_monitor(state_path=state_file, mode="auto", sources=(ssc_src,), http_client=client, notifier=prod_notifier)
            self.assertEqual(s2.mode, "monitor")
            self.assertEqual(len(prod_notifier.sent_notices), 0)

            # 3. Manual live_test run (sends 1 [LIVE TEST] message on test_notifier, 0 prod alerts)
            s3, _ = app.run_monitor(state_path=state_file, mode="live_test", sources=(ssc_src,), http_client=client, notifier=test_notifier)
            self.assertEqual(s3.test_messages_delivered, 1)
            self.assertEqual(len(prod_notifier.sent_notices), 0)

            # 4. New notice appears in payload_2 -> monitor run sends exactly 1 prod alert
            routes[ssc_src.notice_urls[0]] = (200, {"Content-Type": "application/json"}, json.dumps(payload_2))
            s4, _ = app.run_monitor(state_path=state_file, mode="monitor", sources=(ssc_src,), http_client=client, notifier=prod_notifier)
            self.assertEqual(s4.genuinely_new_notices, 1)
            self.assertEqual(s4.telegram_deliveries_confirmed, 1)
            self.assertEqual(len(prod_notifier.sent_notices), 1)

            # 5. Repeated auto runs with payload_2 -> 0 prod alerts
            s5, _ = app.run_monitor(state_path=state_file, mode="auto", sources=(ssc_src,), http_client=client, notifier=prod_notifier)
            self.assertEqual(s5.genuinely_new_notices, 0)
            self.assertEqual(len(prod_notifier.sent_notices), 1)

    def test_21_rssb_rajasthan_portal_and_legacy_tls_adapter(self) -> None:
        """
        Verify RSSB adapter parses official Rajasthan State Recruitment Portal
        (<div class="notice-item"> notificationdocview links and <div class="job-card"> ongoing recruitments).
        """
        rssb_src = app.SOURCE_BY_ID["rssb"]
        html_fixture = """
        <html><body>
        <div class="notice-item">
          <p>Notification for Livestock Assistant Direct Recruitment 2026 <a href="notificationdocview?ID=LSA_2026.pdf">View Document</a></p>
        </div>
        <ul>
          <li>
            <div class="job-card">
              <span class="badge">RSSB</span>
              <h6 onclick="postDetailsClick(4686)">JUNIOR INSTRUCTOR DIRECT RECRUITMENT 2026</h6>
              <small>28-Oct-2026</small>
            </div>
          </li>
        </ul>
        </body></html>
        """
        client = FakeHttpClient(
            routes={
                "https://recruitment.rajasthan.gov.in/": (200, {"Content-Type": "text/html"}, html_fixture),
            }
        )
        notices = app.adapter_rssb(rssb_src, client)
        self.assertGreaterEqual(len(notices), 2)
        self.assertIn("Livestock Assistant Direct Recruitment 2026", notices[0].title)
        self.assertIn("notificationdocview?ID=LSA_2026.pdf", notices[0].url)
        self.assertTrue(any("JUNIOR INSTRUCTOR DIRECT RECRUITMENT 2026" in n.title and "28-Oct-2026" in n.title for n in notices))

    def test_22_dsssb_online_portal_vacancies_and_admit_cards(self) -> None:
        """
        Verify DSSSB adapter extracts Current Vacancies (Post Code:XX/YY) and eAdmit Card links
        from https://dsssbonline.nic.in/.
        """
        dsssb_src = app.SOURCE_BY_ID["dsssb"]
        html_fixture = """
        <html><body>
        <table>
          <tr><td>Current Vacancies</td></tr>
          <tr><td>LEGAL ASSISTANT :: Post Code:815/25</td></tr>
          <tr><td>ASSISTANT ENGINEER (CIVIL) :: Post Code:816/25</td></tr>
        </table>
        <a href="AdmitCardEntry.aspx">Generate/Print eAdmit Card</a>
        </body></html>
        """
        client = FakeHttpClient(
            routes={
                "https://dsssbonline.nic.in/": (200, {"Content-Type": "text/html"}, html_fixture),
            }
        )
        notices = app.adapter_dsssb(dsssb_src, client)
        self.assertEqual(len(notices), 3)
        self.assertIn("Post Code: 815/25", notices[0].title)
        self.assertIn("Post Code: 816/25", notices[1].title)
        self.assertTrue(any("eAdmit Card" in n.title for n in notices))

    def test_23_ukpsc_react_bundle_and_hash_router_preservation(self) -> None:
        """
        Verify UKPSC adapter extracts structured exam notifications from ukpsc.net.in's React JS bundle
        and preserves SPA hash-router paths ('#/') in normalize_url.
        """
        ukpsc_src = app.SOURCE_BY_ID["ukpsc"]
        spa_url = app.normalize_url("https://ukpscnet.in/#/pg0zx1/admit-card-login")
        self.assertEqual(spa_url, "https://ukpscnet.in/#/pg0zx1/admit-card-login")

        html_shell = '<html><head><script defer="defer" src="/static/js/main.06eb16fc.js"></script></head><body><div id="root"></div></body></html>'
        js_bundle = """
        const exams = [
          {id:1,exam_name:"Uttarakhand Combined State Civil/Upper Subordinate Services Examination-2026",exam_apply_url:"https://ukpscnet.in/#/upcs26/login",start_date_format:"10-10-2026 (Saturday)",end_date_format:"30-10-2026 (Friday)"},
          {id:2,exam_name:"Lecturer Uttarakhand Special Subordinate Education Service Exam-2026",exam_apply_url:"https://ukpscnet.in/#/lect26/admit-card",start_date_format:"05-10-2026 (Monday)",end_date_format:"25-10-2026 (Sunday)"}
        ];
        """
        client = FakeHttpClient(
            routes={
                "https://ukpsc.net.in/": (200, {"Content-Type": "text/html"}, html_shell),
                "https://ukpsc.net.in/static/js/main.06eb16fc.js": (200, {"Content-Type": "application/javascript"}, js_bundle),
            }
        )
        notices = app.adapter_ukpsc(ukpsc_src, client)
        self.assertEqual(len(notices), 2)
        self.assertIn("Upper Subordinate Services Examination-2026", notices[0].title)
        self.assertEqual(notices[0].publication_date, "10-10-2026")
        self.assertIn("#/upcs26/login", notices[0].url)

    def test_24_all_9_retained_sources_deliver_production_telegram_alerts(self) -> None:
        """
        Verify that a new eligible notice from each of the 9 retained websites triggers
        a production Telegram notification and is never resent on subsequent runs.
        """
        routes: Dict[str, Any] = {}
        for src in app.SOURCES:
            if src.source_id == "ssc":
                routes[src.notice_urls[0]] = (
                    200,
                    {"Content-Type": "application/json"},
                    json.dumps(
                        {
                            "statusCode": "200",
                            "data": [
                                {
                                    "id": "ssc-prod-9",
                                    "headline": "SSC Combined Graduate Level Examination 2026 Final Result",
                                    "createdAt": "2026-10-19T10:00:00Z",
                                    "attachments": [{"path": "uploads/ssc_cgl_result_2026.pdf"}],
                                }
                            ],
                        }
                    ),
                )
            elif src.source_id == "uppsc":
                routes[src.notice_urls[0]] = (
                    200,
                    {"Content-Type": "text/html"},
                    """
                    <html><body><table>
                      <tr><td>1</td><td>19-10-2026</td><td><a href="Open_PDF_DB.aspx?I4PnQ0tBag=101" title="19-10-2026 UPPSC Combined State Upper Subordinate Recruitment Notification 2026">19-10-2026 UPPSC Combined State Upper Subordinate Recruitment Notification 2026</a></td><td>Visible upto 31 Dec 2026</td></tr>
                    </table></body></html>
                    """,
                )
            else:
                routes[src.notice_urls[0]] = (
                    200,
                    {"Content-Type": "text/html"},
                    f"""
                    <html><body>
                      <h2>Notice Board - {src.organization}</h2>
                      <table>
                        <tr><td>1</td><td>19-10-2026</td><td><a href="/notices/{src.source_id}_new_2026.pdf">19-10-2026 Official Recruitment Notification and Exam Schedule 2026 for {src.source_id.upper()}</a></td></tr>
                      </table>
                    </body></html>
                    """,
                )

        client = FakeHttpClient(routes=routes)
        notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            st = app.create_initial_state()
            st["initialized"] = True
            for src in app.SOURCES:
                st["sources"][src.source_id]["baseline_completed"] = True
                st["sources"][src.source_id]["baseline_completed_at"] = "2026-10-01T00:00:00Z"
            app.save_state_atomically(st, state_file)

            summary1, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=app.SOURCES,
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 19),
                max_alerts_per_run=10,
            )
            self.assertEqual(summary1.total_websites, 9)
            self.assertEqual(summary1.succeeded_websites, 9)
            self.assertEqual(summary1.failed_websites, 0)
            self.assertEqual(summary1.genuinely_new_notices, 9)
            self.assertEqual(summary1.telegram_deliveries_confirmed, 9)
            self.assertEqual(len(notifier.sent_notices), 9)

            # Repeated run sends 0 duplicate alerts
            summary2, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=app.SOURCES,
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 19),
                max_alerts_per_run=10,
            )
            self.assertEqual(summary2.genuinely_new_notices, 0)
            self.assertEqual(summary2.telegram_deliveries_confirmed, 0)
            self.assertEqual(len(notifier.sent_notices), 9)

    def test_25_single_source_failure_does_not_block_remaining_8_websites(self) -> None:
        """
        Verify that a failure on one retained website does not block the other 8 websites
        from being fetched, parsed, and having their eligible Telegram alerts delivered.
        """
        routes: Dict[str, Any] = {}
        for src in app.SOURCES:
            if src.source_id == "mpesb":
                for u in src.notice_urls:
                    routes[u] = app.SourceFetchError("CONNECTION_TIMEOUT", "MPESB timed out")
            elif src.source_id == "ssc":
                routes[src.notice_urls[0]] = (
                    200,
                    {"Content-Type": "application/json"},
                    json.dumps(
                        {
                            "statusCode": "200",
                            "data": [
                                {
                                    "id": "ssc-iso-1",
                                    "headline": "SSC Selection Post Phase-XIV Recruitment Notification 2026",
                                    "createdAt": "2026-10-19T10:00:00Z",
                                    "attachments": [{"path": "uploads/ssc_phase14_2026.pdf"}],
                                }
                            ],
                        }
                    ),
                )
            elif src.source_id == "uppsc":
                routes[src.notice_urls[0]] = (
                    200,
                    {"Content-Type": "text/html"},
                    """
                    <html><body><table>
                      <tr><td>1</td><td>19-10-2026</td><td><a href="Open_PDF_DB.aspx?I4PnQ0tBag=202" title="19-10-2026 UPPSC RO/ARO Examination Schedule 2026">19-10-2026 UPPSC RO/ARO Examination Schedule 2026</a></td><td>Visible upto 31 Dec 2026</td></tr>
                    </table></body></html>
                    """,
                )
            else:
                routes[src.notice_urls[0]] = (
                    200,
                    {"Content-Type": "text/html"},
                    f"""
                    <html><body>
                      <h2>Notice Board - {src.organization}</h2>
                      <table>
                        <tr><td>1</td><td>19-10-2026</td><td><a href="/notices/{src.source_id}_iso_2026.pdf">19-10-2026 Direct Recruitment Advertisement 2026 for {src.source_id.upper()}</a></td></tr>
                      </table>
                    </body></html>
                    """,
                )

        client = FakeHttpClient(routes=routes)
        notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            st = app.create_initial_state()
            st["initialized"] = True
            for src in app.SOURCES:
                st["sources"][src.source_id]["baseline_completed"] = True
                st["sources"][src.source_id]["baseline_completed_at"] = "2026-10-01T00:00:00Z"
            app.save_state_atomically(st, state_file)

            summary, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=app.SOURCES,
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 19),
                max_alerts_per_run=10,
            )
            self.assertEqual(summary.total_websites, 9)
            self.assertEqual(summary.succeeded_websites, 8)
            self.assertEqual(summary.failed_websites, 1)
            self.assertEqual(summary.genuinely_new_notices, 8)
            self.assertEqual(summary.telegram_deliveries_confirmed, 8)
            self.assertEqual(len(notifier.sent_notices), 8)

    def test_26_dead_host_caching_prevents_redundant_timeout_retries(self) -> None:
        """
        Verify HttpClient caches unreachable hosts (CONNECTION_TIMEOUT, CONNECTION_RESET, DNS_ERROR)
        within a single run so fallback URLs on the same dead host fail fast without redundant retries.
        """
        client = FakeHttpClient(
            routes={
                "https://upsssc.gov.in/": app.SourceFetchError("CONNECTION_TIMEOUT", "Connection timed out"),
                "https://upsssc.gov.in/Default.aspx": app.SourceFetchError("CONNECTION_TIMEOUT", "Connection timed out"),
            },
            max_retries=2,
        )
        with self.assertRaises(app.SourceFetchError) as ctx1:
            client.fetch_text("https://upsssc.gov.in/")
        self.assertEqual(ctx1.exception.category, "CONNECTION_TIMEOUT")
        self.assertEqual(client.call_counts["https://upsssc.gov.in/"], 2)

        # Second URL on the same dead host should fail immediately from cache with 0 network calls
        with self.assertRaises(app.SourceFetchError) as ctx2:
            client.fetch_text("https://upsssc.gov.in/Default.aspx")
        self.assertEqual(ctx2.exception.category, "CONNECTION_TIMEOUT")
        self.assertEqual(client.call_counts.get("https://upsssc.gov.in/Default.aspx", 0), 0)

    def test_27_state_migration_prunes_crpf_cisf_ssb_and_preserves_retained_9_history(self) -> None:
        """
        Verify that loading/validating a state containing 12 or 19 sources (including CRPF, CISF, SSB)
        safely prunes removed sites from active monitoring while preserving all 9 retained sources,
        their baseline status, and their notice history without re-sending old notices.
        """
        legacy_state = {
            "schema_version": 1,
            "initialized": True,
            "created_at": "2026-10-18T00:00:00Z",
            "updated_at": "2026-10-18T01:00:00Z",
            "sources": {
                # Retained working sources
                "ssc": {
                    "organization": "Staff Selection Commission (SSC)",
                    "homepage_url": "https://ssc.gov.in/",
                    "baseline_completed": True,
                    "baseline_completed_at": "2026-10-18T00:05:00Z",
                    "last_checked_at": "2026-10-18T01:00:00Z",
                    "last_success_at": "2026-10-18T01:00:00Z",
                    "last_status": "MONITOR_CHECK_OK",
                    "last_error": None,
                    "notice_count": 100,
                },
                "upsc": {
                    "organization": "Union Public Service Commission (UPSC)",
                    "homepage_url": "https://www.upsc.gov.in/",
                    "baseline_completed": True,
                    "baseline_completed_at": "2026-10-18T00:05:00Z",
                    "last_checked_at": "2026-10-18T01:00:00Z",
                    "last_success_at": "2026-10-18T01:00:00Z",
                    "last_status": "MONITOR_CHECK_OK",
                    "last_error": None,
                    "notice_count": 57,
                },
                # Removed CRPF, CISF, SSB sources and earlier removed sources
                "crpf": {
                    "organization": "Central Reserve Police Force (CRPF)",
                    "homepage_url": "https://rect.crpf.gov.in/",
                    "baseline_completed": True,
                    "baseline_completed_at": "2026-10-18T00:05:00Z",
                    "last_checked_at": "2026-10-18T01:00:00Z",
                    "last_success_at": "2026-10-18T01:00:00Z",
                    "last_status": "MONITOR_CHECK_OK",
                    "last_error": None,
                    "notice_count": 20,
                },
                "cisf": {
                    "organization": "Central Industrial Security Force (CISF)",
                    "homepage_url": "https://cisfrectt.cisf.gov.in/",
                    "baseline_completed": True,
                    "baseline_completed_at": "2026-10-18T00:05:00Z",
                    "last_checked_at": "2026-10-18T01:00:00Z",
                    "last_success_at": "2026-10-18T01:00:00Z",
                    "last_status": "MONITOR_CHECK_OK",
                    "last_error": None,
                    "notice_count": 15,
                },
                "ssb": {
                    "organization": "Sashastra Seema Bal (SSB)",
                    "homepage_url": "https://ssb.gov.in/",
                    "baseline_completed": True,
                    "baseline_completed_at": "2026-10-18T00:05:00Z",
                    "last_checked_at": "2026-10-18T01:00:00Z",
                    "last_success_at": "2026-10-18T01:00:00Z",
                    "last_status": "MONITOR_CHECK_OK",
                    "last_error": None,
                    "notice_count": 30,
                },
                "upsssc": {
                    "organization": "Uttar Pradesh Subordinate Services Selection Commission (UPSSSC)",
                    "homepage_url": "https://upsssc.gov.in/",
                    "baseline_completed": False,
                    "baseline_completed_at": None,
                    "last_checked_at": "2026-10-18T01:00:00Z",
                    "last_success_at": None,
                    "last_status": "FETCH_FAILED",
                    "last_error": "Connection timed out",
                    "notice_count": 0,
                },
            },
            "notices": {
                "ssc_notice_fp_1": {
                    "fingerprint": "ssc_notice_fp_1",
                    "source_id": "ssc",
                    "organization": "Staff Selection Commission (SSC)",
                    "title": "SSC CGL 2026 Examination Notice",
                    "normalized_url": "https://ssc.gov.in/notices/cgl2026.pdf",
                    "first_seen_at": "2026-10-18T00:05:00Z",
                    "delivery_status": "delivered",
                },
                "upsc_notice_fp_2": {
                    "fingerprint": "upsc_notice_fp_2",
                    "source_id": "upsc",
                    "organization": "Union Public Service Commission (UPSC)",
                    "title": "UPSC Civil Services Examination 2026",
                    "normalized_url": "https://upsc.gov.in/notices/cse2026.pdf",
                    "first_seen_at": "2026-10-18T00:05:00Z",
                    "delivery_status": "baselined",
                },
                "crpf_removed_notice": {
                    "fingerprint": "crpf_removed_notice",
                    "source_id": "crpf",
                    "organization": "Central Reserve Police Force (CRPF)",
                    "title": "CRPF Notice",
                    "normalized_url": "https://rect.crpf.gov.in/old.pdf",
                    "first_seen_at": "2026-10-18T00:05:00Z",
                    "delivery_status": "baselined",
                },
                "cisf_removed_notice": {
                    "fingerprint": "cisf_removed_notice",
                    "source_id": "cisf",
                    "organization": "Central Industrial Security Force (CISF)",
                    "title": "CISF Notice",
                    "normalized_url": "https://cisfrectt.cisf.gov.in/old.pdf",
                    "first_seen_at": "2026-10-18T00:05:00Z",
                    "delivery_status": "baselined",
                },
                "ssb_removed_notice": {
                    "fingerprint": "ssb_removed_notice",
                    "source_id": "ssb",
                    "organization": "Sashastra Seema Bal (SSB)",
                    "title": "SSB Notice",
                    "normalized_url": "https://ssb.gov.in/old.pdf",
                    "first_seen_at": "2026-10-18T00:05:00Z",
                    "delivery_status": "baselined",
                },
            },
        }

        migrated = app.validate_state_schema(legacy_state)

        # 1. Exactly 9 active sources configured in SOURCES
        self.assertEqual(len(migrated["sources"]), 9)
        for src in app.SOURCES:
            self.assertIn(src.source_id, migrated["sources"])

        # 2. All removed sources (including crpf, cisf, ssb) pruned from sources
        for rem_id in app.REMOVED_SOURCE_IDS:
            self.assertNotIn(rem_id, migrated["sources"])

        # 3. Retained sources preserve their baseline_completed status and timestamps
        self.assertTrue(migrated["sources"]["ssc"]["baseline_completed"])
        self.assertEqual(migrated["sources"]["ssc"]["baseline_completed_at"], "2026-10-18T00:05:00Z")
        self.assertTrue(migrated["sources"]["upsc"]["baseline_completed"])

        # 4. State remains initialized
        self.assertTrue(migrated["initialized"])

        # 5. Retained notices preserved intact; removed source notices pruned
        self.assertIn("ssc_notice_fp_1", migrated["notices"])
        self.assertEqual(migrated["notices"]["ssc_notice_fp_1"]["delivery_status"], "delivered")
        self.assertIn("upsc_notice_fp_2", migrated["notices"])
        self.assertEqual(migrated["notices"]["upsc_notice_fp_2"]["delivery_status"], "baselined")
        self.assertNotIn("crpf_removed_notice", migrated["notices"])
        self.assertNotIn("cisf_removed_notice", migrated["notices"])
        self.assertNotIn("ssb_removed_notice", migrated["notices"])

    def test_28_strict_keyword_filtering_and_all_five_categories(self) -> None:
        """
        Verify strict keyword matching across all 5 categories (Recruitment, Examination,
        Results, Answer Keys, Next Stages), case-insensitivity, punctuation/spacing
        variations, and custom keyword configuration.
        """
        # 1. Recruitment
        ok, cat, kws = app.match_notice_keywords(
            "Detailed Recruitment Advertisement for 450 Posts — Apply Online via Application Form"
        )
        self.assertTrue(ok)
        self.assertEqual(cat, "Recruitment")
        self.assertTrue(any(k in kws for k in ("recruitment", "advertisement", "apply online", "application form")))

        # 2. Examination
        ok_ex, cat_ex, _ = app.match_notice_keywords(
            "Notice regarding Exam Schedule, City Intimation and Hall-Ticket / E-Admit Card for CGL 2026"
        )
        self.assertTrue(ok_ex)
        self.assertEqual(cat_ex, "Examination")

        # 3. Results (including cut-off / cutoff / cut off variations)
        for phrase in (
            "Final Result and Selection List for Combined State Exam 2026",
            "Declaration of Scorecard and Cut-Off Marks for Tier-1",
            "Provisional Merit List and Cutoff Marks for Constable Exam",
        ):
            ok_res, cat_res, _ = app.match_notice_keywords(phrase)
            self.assertTrue(ok_res, f"Expected match for: {phrase}")
            self.assertEqual(cat_res, "Results")

        # 4. Answer Keys (including answer-key / answerkey / response sheet variations)
        for phrase in (
            "Uploading of Provisional Answer-Key and Response Sheet for JE Exam 2026",
            "Final AnswerKey for Sub-Inspector Examination 2026",
        ):
            ok_ak, cat_ak, _ = app.match_notice_keywords(phrase)
            self.assertTrue(ok_ak, f"Expected match for: {phrase}")
            self.assertEqual(cat_ak, "Answer Keys")

        # 5. Next Stages (DV, document verification, skill test, typing test, interview, PET, PST, medical examination)
        for phrase in (
            "Schedule for Document Verification (DV) and Medical Examination of Selected Candidates",
            "Call Letter for Skill Test / Typing Test for Stenographer Recruitment 2026",
            "Schedule of PET / PST and Interview for Assistant Commandant Examination 2026",
        ):
            ok_ns, cat_ns, _ = app.match_notice_keywords(phrase)
            self.assertTrue(ok_ns, f"Expected match for: {phrase}")
            self.assertEqual(cat_ns, "Next Stages")

        # 6. Custom keyword configuration override
        custom = {"Recruitment": ["special apprenticeship drive"]}
        ok_cust, cat_cust, _ = app.match_notice_keywords(
            "Notice for Special Apprenticeship Drive 2026",
            keyword_categories=custom,
        )
        self.assertTrue(ok_cust)
        self.assertEqual(cat_cust, "Recruitment")

    def test_29_irrelevant_notices_and_contextual_false_positives_rejected(self) -> None:
        """
        Verify administrative, tender, transfer, seniority list, and contextual false-positive
        notices ('posts', 'notification', 'DV', 'interview', 'PET', 'PST' in unrelated contexts)
        are strictly rejected.
        """
        irrelevant_samples = [
            "Notice Inviting Tender (NIT) for Supply of Office Furniture and Computers",
            "Corrigendum to e-Tender / e-Procurement Notice for Security Services",
            "Transfer and Posting Order of Section Officers and Administrative Staff",
            "Provisional Gradation List / Seniority List of Ministerial Cadre as on 01-01-2026",
            "Office Order regarding Empanelment of Private Hospitals and Medical Reimbursement",
            "Public Auction Notice for Condemned Government Vehicles",
            # Contextual false positives for 'posts'
            "Applications sent by Speed Post or Registered Post will not be entertained",
            "Order regarding Ex-Post Facto Sanction of Leave for Staff",
            # Contextual false positives for 'notification'
            "Gazette Notification regarding Public Holidays for Calendar Year 2026",
            "Tariff Notification and Pension Rules Amendment 2026",
            # Contextual false positives for 'DV'
            "Statement of Disbursement Voucher (DV No. 402) for March Quarter",
            # Contextual false positives for 'interview'
            "Press Release and Media Interview of Hon'ble Chairman regarding Digital Portal",
            # Generic navigation/unmatched text
            "Welcome to Official Portal - Annual Report 2025-26 Released",
        ]
        for sample in irrelevant_samples:
            ok, cat, kws = app.match_notice_keywords(sample)
            self.assertFalse(ok, f"Expected irrelevant notice to be rejected: {sample!r} (matched {cat}: {kws})")

    def test_30_mandatory_publication_date_validation_and_non_publication_dates(self) -> None:
        """
        Verify mandatory publication-date validation:
        - Rejects missing, invalid/impossible (31-02-2026, 29-02-2026), future, and stale dates
        - Never treats modified dates, application deadlines, or exam dates as the publication date
        - Extracts application_date and exam_date separately and includes them in formatted alerts
        """
        ref = app.datetime.date(2026, 10, 20)

        # Missing date -> rejected
        valid_missing, iso_missing, reason_missing = app.validate_publication_date(None, reference_date=ref)
        self.assertFalse(valid_missing)
        self.assertIsNone(iso_missing)
        self.assertEqual(reason_missing, "MISSING_PUBLICATION_DATE")

        # Impossible calendar dates -> rejected
        for bad_date in ("31-02-2026", "29-02-2026", "2026-02-30", "32-01-2026"):
            self.assertIsNone(app.parse_publication_date_sort_key(bad_date))
            valid_bad, _, reason_bad = app.validate_publication_date(bad_date, reference_date=ref)
            self.assertFalse(valid_bad)
            self.assertEqual(reason_bad, "INVALID_PUBLICATION_DATE")

        # Future date -> rejected unless explicitly validated
        valid_fut, iso_fut, reason_fut = app.validate_publication_date("25-10-2026", reference_date=ref)
        self.assertFalse(valid_fut)
        self.assertEqual(iso_fut, "2026-10-25")
        self.assertEqual(reason_fut, "FUTURE_PUBLICATION_DATE")

        # Stale date (> max_age_days) -> rejected
        valid_stale, iso_stale, reason_stale = app.validate_publication_date(
            "01-08-2026", reference_date=ref, max_age_days=30
        )
        self.assertFalse(valid_stale)
        self.assertEqual(iso_stale, "2026-08-01")
        self.assertEqual(reason_stale, "STALE_PUBLICATION_DATE")

        # Valid fresh date -> accepted and normalized to YYYY-MM-DD
        valid_ok, iso_ok, reason_ok = app.validate_publication_date(
            "18-10-2026", reference_date=ref, max_age_days=30
        )
        self.assertTrue(valid_ok)
        self.assertEqual(iso_ok, "2026-10-18")
        self.assertEqual(reason_ok, "VALID_PUBLICATION_DATE")

        # Modified dates, application deadlines, and exam dates must NEVER be extracted as publication_date
        self.assertIsNone(app.extract_explicit_date("Page Last Modified on 19-10-2026"))
        self.assertIsNone(app.extract_explicit_date("Last Date for Apply Online: 15-11-2026"))
        self.assertIsNone(app.extract_explicit_date("Closing Date of Application: 20/11/2026"))
        self.assertIsNone(app.extract_explicit_date("Written Exam Date: 12-12-2026"))

        # When notice has publication date + application window + exam date, each is extracted accurately
        rich_text = (
            "Published on 18-10-2026: Combined Graduate Level Recruitment 2026 "
            "(Apply Online from 20-10-2026 to 19-11-2026; Written Exam Date: 15-12-2026)"
        )
        self.assertEqual(app.extract_explicit_date(rich_text), "18-10-2026")
        self.assertEqual(app.extract_application_date(rich_text), "20-10-2026 to 19-11-2026")
        self.assertEqual(app.extract_exam_date(rich_text), "15-12-2026")

        # Verify formatted Telegram message includes Organization, Title, Category, Publication Date, Application/Exam Dates, and URL
        msg = app.format_telegram_message(
            {
                "organization": "Staff Selection Commission (SSC)",
                "title": "Combined Graduate Level Recruitment 2026",
                "category": "Recruitment",
                "publication_date": "2026-10-18",
                "application_date": "20-10-2026 to 19-11-2026",
                "exam_date": "15-12-2026",
                "normalized_url": "https://ssc.gov.in/notices/cgl_2026.pdf",
            }
        )
        self.assertIn("Staff Selection Commission (SSC)", msg)
        self.assertIn("Combined Graduate Level Recruitment 2026", msg)
        self.assertIn("Recruitment", msg)
        self.assertIn("2026-10-18", msg)
        self.assertIn("Application Date:</b> 20-10-2026 to 19-11-2026", msg)
        self.assertIn("Examination Date:</b> 15-12-2026", msg)
        self.assertIn("https://ssc.gov.in/notices/cgl_2026.pdf", msg)

    def test_31_monitor_suppresses_irrelevant_missing_date_and_old_notices(self) -> None:
        """
        Verify run_monitor in monitor mode suppresses:
        - Irrelevant notices (e.g. Tender / Transfer order)
        - Relevant notices missing an official publication date
        - Relevant notices with an impossible or stale publication date (> max_age_days)
        And sends alerts ONLY for relevant, newly published notices with valid dates.
        """
        upsc_src = app.SOURCE_BY_ID["upsc"]
        baseline_html = """
        <html><body><h2>What's New</h2><ul>
          <li><a href="/sites/default/files/Baseline_Exam_2026.pdf">10-10-2026 Civil Services Preliminary Examination 2026 Notification</a></li>
        </ul></body></html>
        """
        incremental_html = """
        <html><body><h2>What's New</h2><ul>
          <li><a href="/sites/default/files/Baseline_Exam_2026.pdf">10-10-2026 Civil Services Preliminary Examination 2026 Notification</a></li>
          <li><a href="/sites/default/files/Tender_Furniture_2026.pdf">19-10-2026 Notice Inviting Tender for Office Computers</a></li>
          <li><a href="/sites/default/files/Postal_Notice_2026.pdf">19-10-2026 Public Notice regarding Speed Post and Registered Post</a></li>
          <li><a href="/sites/default/files/Missing_Date_Result.pdf">Final Result for Combined Medical Services Examination</a></li>
          <li><a href="/sites/default/files/Old_Stale_AdmitCard.pdf">01-05-2026 e-Admit Card for NDA Examination (I) 2026</a></li>
          <li><a href="/sites/default/files/Valid_Fresh_Result_2026.pdf">19-10-2026 Final Result and Merit List for Engineering Services Examination 2026</a></li>
        </ul></body></html>
        """
        routes: Dict[str, Any] = {
            upsc_src.notice_urls[0]: (200, {"Content-Type": "text/html"}, baseline_html),
        }
        client = FakeHttpClient(routes=routes)
        notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            app.save_state_atomically(app.create_initial_state(), state_file)

            # Step 1: Baseline
            app.run_monitor(
                state_path=state_file,
                mode="baseline",
                sources=(upsc_src,),
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 18),
            )
            self.assertEqual(len(notifier.sent_notices), 0)

            # Step 2: Monitor run with 1 tender, 1 missing date, 1 old notice, and 1 valid fresh result
            routes[upsc_src.notice_urls[0]] = (200, {"Content-Type": "text/html"}, incremental_html)
            summary, state = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(upsc_src,),
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 19),
                max_age_days=30,
            )
            self.assertEqual(summary.genuinely_new_notices, 1)
            self.assertEqual(summary.telegram_deliveries_confirmed, 1)
            self.assertEqual(summary.filtered_irrelevant_notices, 1)
            self.assertEqual(summary.rejected_missing_or_invalid_date_notices, 1)
            self.assertEqual(summary.rejected_stale_notices, 1)
            self.assertEqual(len(notifier.sent_notices), 1)
            self.assertIn(
                "Final Result and Merit List for Engineering Services Examination 2026",
                notifier.sent_notices[0][0]["title"],
            )

            # Step 3: Repeated run sends 0 alerts and does not re-evaluate suppressed notices
            summary_repeat, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(upsc_src,),
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 19),
                max_age_days=30,
            )
            self.assertEqual(summary_repeat.genuinely_new_notices, 0)
            self.assertEqual(summary_repeat.telegram_deliveries_confirmed, 0)
            self.assertEqual(len(notifier.sent_notices), 1)

    def test_32_cross_page_url_and_title_variation_deduplication(self) -> None:
        """
        Verify strong duplicate prevention across multiple pages, URL query parameters,
        HTTP/HTTPS scheme differences, and punctuation/spacing variations in titles.
        """
        upsc_src = app.SOURCE_BY_ID["upsc"]
        html_v1 = """
        <html><body><h2>What's New</h2><ul>
          <li><a href="http://www.upsc.gov.in/sites/default/files/Advt-12-2026.pdf?utm_source=homepage">18-10-2026 Recruitment Advertisement No. 12/2026 for 85 Posts</a></li>
        </ul></body></html>
        """
        # Second run serves the same notice with HTTPS, different query param, and minor title spacing/badge variation
        html_v2 = """
        <html><body><h2>What's New</h2><ul>
          <li><a href="https://upsc.gov.in/sites/default/files/Advt-12-2026.pdf?download=1">1. 18-10-2026 Recruitment  Advertisement No. 12/2026 - for 85 Posts [NEW] (2.1 MB)</a></li>
        </ul></body></html>
        """
        routes: Dict[str, Any] = {
            upsc_src.notice_urls[0]: (200, {"Content-Type": "text/html"}, html_v1),
        }
        client = FakeHttpClient(routes=routes)
        notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            st = app.create_initial_state()
            st["initialized"] = True
            st["sources"]["upsc"]["baseline_completed"] = True
            st["sources"]["upsc"]["baseline_completed_at"] = "2026-10-01T00:00:00Z"
            app.save_state_atomically(st, state_file)

            # Run 1 sends the new notice once
            s1, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(upsc_src,),
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 19),
            )
            self.assertEqual(s1.genuinely_new_notices, 1)
            self.assertEqual(s1.telegram_deliveries_confirmed, 1)

            # Run 2 with URL/title variations must detect duplicate and send 0 alerts
            routes[upsc_src.notice_urls[0]] = (200, {"Content-Type": "text/html"}, html_v2)
            s2, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(upsc_src,),
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 19),
            )
            self.assertEqual(s2.genuinely_new_notices, 0)
            self.assertEqual(s2.telegram_deliveries_confirmed, 0)
            self.assertEqual(len(notifier.sent_notices), 1)

    def test_33_max_ten_production_alerts_per_run_cap(self) -> None:
        """
        Verify that a single monitoring run sends at most MAX_PRODUCTION_ALERTS_PER_RUN (10)
        production notifications and leaves overflow notices queued for the next run.
        """
        ssc_src = app.SOURCE_BY_ID["ssc"]
        items = [
            {
                "id": f"ssc-bulk-{i}",
                "headline": f"Recruitment Notification No. {i}/2026 for Group-B Posts",
                "createdAt": "2026-10-19T09:00:00.000Z",
                "attachments": [{"path": f"uploads/notice_{i}_2026.pdf"}],
            }
            for i in range(1, 15)  # 14 distinct valid new notices
        ]
        ssc_payload = {"statusCode": "200", "data": items}
        client = FakeHttpClient(
            routes={
                ssc_src.notice_urls[0]: (200, {"Content-Type": "application/json"}, json.dumps(ssc_payload)),
            }
        )
        notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            st = app.create_initial_state()
            st["initialized"] = True
            st["sources"]["ssc"]["baseline_completed"] = True
            st["sources"]["ssc"]["baseline_completed_at"] = "2026-10-01T00:00:00Z"
            app.save_state_atomically(st, state_file)

            # Run 1: discovers 14 valid notices, sends exactly 10 (capped)
            s1, state1 = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(ssc_src,),
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 19),
                max_alerts_per_run=10,
            )
            self.assertEqual(s1.genuinely_new_notices, 14)
            self.assertEqual(s1.telegram_deliveries_confirmed, 10)
            self.assertEqual(s1.capped_deferred_notices, 4)
            self.assertEqual(len(notifier.sent_notices), 10)

            # Run 2: sends the remaining 4 deferred notices
            s2, state2 = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(ssc_src,),
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 19),
                max_alerts_per_run=10,
            )
            self.assertEqual(s2.genuinely_new_notices, 0)
            self.assertEqual(s2.telegram_deliveries_confirmed, 4)
            self.assertEqual(len(notifier.sent_notices), 14)


if __name__ == "__main__":
    unittest.main(verbosity=2)



