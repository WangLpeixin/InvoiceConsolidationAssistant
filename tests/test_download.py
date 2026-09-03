"""测试下载辅助逻辑（不连邮箱）。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from invoice.download import (
    DEFAULT_SINCE,
    DownloadLedger,
    DownloadStats,
    decode_mime_header,
    imap_since_clause,
    ingest_message,
    iter_pdf_attachments,
    unique_path,
)
from invoice.fetch_pdf import FetchOutcome


class DownloadHelperTests(unittest.TestCase):
    def test_default_since_is_june_17(self) -> None:
        self.assertEqual(DEFAULT_SINCE, date(2026, 6, 17))

    def test_imap_since_clause(self) -> None:
        self.assertEqual(imap_since_clause(date(2026, 6, 17)), "17-Jun-2026")

    def test_decode_mime_header(self) -> None:
        self.assertEqual(decode_mime_header("=?utf-8?B?5Y+R56Wo?= "), "发票")

    def test_decode_unknown_8bit(self) -> None:
        text = decode_mime_header("=?unknown-8bit?q?=B7=A2=C6=AC?=")
        self.assertTrue(len(text) > 0)

    def test_ledger_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "downloaded.json"
            ledger = DownloadLedger(path=path, message_ids={"<a@b>"}, sha256s={"abc"})
            ledger.save()
            loaded = DownloadLedger.load(path)
            self.assertEqual(loaded.message_ids, {"<a@b>"})
            self.assertEqual(loaded.sha256s, {"abc"})

    def test_unique_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            first = unique_path(directory, "a.pdf")
            first.write_text("1", encoding="utf-8")
            second = unique_path(directory, "a.pdf")
            self.assertEqual(second.name, "a_2.pdf")

    def test_iter_pdf_attachments(self) -> None:
        message = EmailMessage()
        message["Subject"] = "电子发票"
        message.add_attachment(
            b"%PDF-1.4 fake",
            maintype="application",
            subtype="pdf",
            filename="滴滴.pdf",
        )
        message.add_attachment(
            b"not-pdf",
            maintype="text",
            subtype="plain",
            filename="readme.txt",
        )
        attachments = iter_pdf_attachments(message)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0][0], "滴滴.pdf")
        self.assertTrue(attachments[0][1].startswith(b"%PDF"))


class IngestMessageTests(unittest.TestCase):
    def _dirs(self, tmp: str) -> tuple[Path, Path, DownloadLedger, DownloadStats]:
        root = Path(tmp)
        pool = root / "pool"
        unknown = root / "unknown"
        pool.mkdir()
        unknown.mkdir()
        ledger = DownloadLedger(path=root / "downloaded.json")
        return pool, unknown, ledger, DownloadStats()

    def test_link_pdf_saved_and_recorded(self) -> None:
        message = EmailMessage()
        message["Subject"] = "电子发票金额50.5元"
        message["Message-ID"] = "<link@test>"
        message.set_content("请点击 https://fp.example/dzfp.pdf 下载")

        def fake_fetch(url: str) -> FetchOutcome:
            return FetchOutcome(b"%PDF-1.4 from-link", "ok", url)

        with tempfile.TemporaryDirectory() as tmp:
            pool, unknown, ledger, stats = self._dirs(tmp)
            ingest_message(
                message,
                pool,
                unknown,
                ledger,
                stats,
                fetch_pdf=fake_fetch,
                decode_qr=lambda _b: [],
                fetch_image=lambda _u: None,
            )
            self.assertEqual(stats.saved, 1)
            self.assertEqual(stats.saved_link, 1)
            self.assertIn("<link@test>", ledger.message_ids)
            self.assertTrue(any(path.name.startswith("50.5_") for path in pool.glob("*.pdf")))

    def test_failed_link_not_recorded(self) -> None:
        message = EmailMessage()
        message["Subject"] = "电子发票"
        message["Message-ID"] = "<fail@test>"
        message.set_content('<a href="https://fp.example/need-login">下载发票</a>', subtype="html")

        def fake_fetch(url: str) -> FetchOutcome:
            return FetchOutcome(None, "login", url)

        with tempfile.TemporaryDirectory() as tmp:
            pool, unknown, ledger, stats = self._dirs(tmp)
            ingest_message(
                message,
                pool,
                unknown,
                ledger,
                stats,
                fetch_pdf=fake_fetch,
                decode_qr=lambda _b: [],
                fetch_image=lambda _u: None,
            )
            self.assertEqual(stats.saved, 0)
            self.assertEqual(stats.fetch_failed, 1)
            self.assertNotIn("<fail@test>", ledger.message_ids)

    def test_failed_link_discards_stale_message_id(self) -> None:
        message = EmailMessage()
        message["Subject"] = "电子发票"
        message["Message-ID"] = "<stale@test>"
        message.set_content('<a href="https://fp.example/need-login">下载发票</a>', subtype="html")

        def fake_fetch(url: str) -> FetchOutcome:
            return FetchOutcome(None, "html_no_pdf", url)

        with tempfile.TemporaryDirectory() as tmp:
            pool, unknown, ledger, stats = self._dirs(tmp)
            ledger.message_ids.add("<stale@test>")
            ingest_message(
                message,
                pool,
                unknown,
                ledger,
                stats,
                rescan=True,
                fetch_pdf=fake_fetch,
                decode_qr=lambda _b: [],
                fetch_image=lambda _u: None,
            )
            self.assertEqual(stats.fetch_failed, 1)
            self.assertNotIn("<stale@test>", ledger.message_ids)

    def test_rescan_reenters_recorded_mail(self) -> None:
        message = EmailMessage()
        message["Subject"] = "电子发票金额12元"
        message["Message-ID"] = "<old@test>"
        message.set_content("https://fp.example/dzfp.pdf")

        def fake_fetch(url: str) -> FetchOutcome:
            return FetchOutcome(b"%PDF-1.4 rescan", "ok", url)

        with tempfile.TemporaryDirectory() as tmp:
            pool, unknown, ledger, stats = self._dirs(tmp)
            ledger.message_ids.add("<old@test>")
            ingest_message(
                message,
                pool,
                unknown,
                ledger,
                stats,
                rescan=False,
                fetch_pdf=fake_fetch,
                decode_qr=lambda _b: [],
                fetch_image=lambda _u: None,
            )
            self.assertEqual(stats.skipped_mails, 1)
            self.assertEqual(stats.saved, 0)

            stats2 = DownloadStats()
            ingest_message(
                message,
                pool,
                unknown,
                ledger,
                stats2,
                rescan=True,
                fetch_pdf=fake_fetch,
                decode_qr=lambda _b: [],
                fetch_image=lambda _u: None,
            )
            self.assertEqual(stats2.saved, 1)
            self.assertEqual(stats2.saved_link, 1)


if __name__ == "__main__":
    unittest.main()
