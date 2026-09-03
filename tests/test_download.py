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
    decode_mime_header,
    imap_since_clause,
    iter_pdf_attachments,
    unique_path,
)


class DownloadHelperTests(unittest.TestCase):
    def test_default_since_is_june_17(self) -> None:
        self.assertEqual(DEFAULT_SINCE, date(2026, 6, 17))

    def test_imap_since_clause(self) -> None:
        self.assertEqual(imap_since_clause(date(2026, 6, 17)), "17-Jun-2026")

    def test_decode_mime_header(self) -> None:
        self.assertEqual(decode_mime_header("=?utf-8?B?5Y+R56Wo?= "), "发票")

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


if __name__ == "__main__":
    unittest.main()
