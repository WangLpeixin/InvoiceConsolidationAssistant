"""测试漏票邮件分类（不连邮箱）。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from invoice.audit import (
    REASON_FETCH,
    REASON_LOGIN,
    REASON_NO_CLUE,
    REASON_OFD,
    REASON_WEIXIN,
    STATUS_INCLUDED,
    STATUS_MISSED,
    STATUS_OTHER,
    classify_invoice_mail,
    format_audit_report,
    write_audit_csv,
)
from invoice.download import DownloadLedger


def _ledger(*message_ids: str) -> DownloadLedger:
    return DownloadLedger(path=Path("downloaded.json"), message_ids=set(message_ids))


class ClassifyMailTests(unittest.TestCase):
    def test_non_invoice_is_other(self) -> None:
        message = EmailMessage()
        message["Subject"] = "PlayStation 促销"
        message["Message-ID"] = "<promo@test>"
        message.set_content("没有票")
        row = classify_invoice_mail(message, _ledger())
        self.assertEqual(row.status, STATUS_OTHER)

    def test_pdf_attachment_is_included(self) -> None:
        message = EmailMessage()
        message["Subject"] = "电子发票"
        message["Date"] = "Fri, 03 Jul 2026 10:00:00 +0800"
        message["Message-ID"] = "<pdf@test>"
        message.add_attachment(
            b"%PDF-1.4 fake",
            maintype="application",
            subtype="pdf",
            filename="票.pdf",
        )
        row = classify_invoice_mail(message, _ledger())
        self.assertEqual(row.status, STATUS_INCLUDED)
        self.assertEqual(row.when, date(2026, 7, 3))

    def test_ledger_id_is_included(self) -> None:
        message = EmailMessage()
        message["Subject"] = "电子发票"
        message["Message-ID"] = "<done@test>"
        message.set_content("已处理过")
        row = classify_invoice_mail(message, _ledger("<done@test>"))
        self.assertEqual(row.status, STATUS_INCLUDED)

    def test_weixin_qr_is_missed(self) -> None:
        message = EmailMessage()
        message["Subject"] = "电子发票开具通知"
        message["Message-ID"] = "<wx@test>"
        message.set_content("请扫码 http://weixin.qq.com/r/dihtdTDE522YrXQW931k")
        row = classify_invoice_mail(message, _ledger())
        self.assertEqual(row.status, STATUS_MISSED)
        self.assertEqual(row.reason, REASON_WEIXIN)

    def test_ofd_is_missed(self) -> None:
        message = EmailMessage()
        message["Subject"] = "电子发票"
        message["Message-ID"] = "<ofd@test>"
        message.add_attachment(
            b"OFD",
            maintype="application",
            subtype="octet-stream",
            filename="票.ofd",
        )
        row = classify_invoice_mail(message, _ledger())
        self.assertEqual(row.status, STATUS_MISSED)
        self.assertEqual(row.reason, REASON_OFD)

    def test_login_portal_is_missed(self) -> None:
        message = EmailMessage()
        message["Subject"] = "诺诺发票"
        message["Message-ID"] = "<nn@test>"
        message.set_content("下载 https://nnfp.jss.com.cn/az2GzNnOqG-RsKm")
        row = classify_invoice_mail(message, _ledger())
        self.assertEqual(row.status, STATUS_MISSED)
        self.assertEqual(row.reason, REASON_LOGIN)

    def test_direct_pdf_link_not_in_ledger_is_fetch_failed(self) -> None:
        message = EmailMessage()
        message["Subject"] = "电子发票"
        message["Message-ID"] = "<link@test>"
        message.set_content("https://fp.example/dzfp_abc.pdf")
        row = classify_invoice_mail(message, _ledger())
        self.assertEqual(row.status, STATUS_MISSED)
        self.assertEqual(row.reason, REASON_FETCH)

    def test_no_clue_is_missed(self) -> None:
        message = EmailMessage()
        message["Subject"] = "电子发票"
        message["Message-ID"] = "<empty@test>"
        message.set_content("请到柜台领取")
        row = classify_invoice_mail(message, _ledger())
        self.assertEqual(row.status, STATUS_MISSED)
        self.assertEqual(row.reason, REASON_NO_CLUE)


class AuditFormatTests(unittest.TestCase):
    def test_write_csv(self) -> None:
        from invoice.audit import AuditReport, AuditRow

        row = AuditRow(
            status=STATUS_MISSED,
            reason=REASON_WEIXIN,
            when=date(2026, 7, 3),
            subject="电子发票开具通知",
            sender="a@b.com",
            message_id="<wx@test>",
        )
        report = AuditReport(other=1, included=2, missed=[row])
        text = format_audit_report(report)
        self.assertIn("未纳入 1 封", text)
        self.assertIn("微信扫码", text)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missed.csv"
            write_audit_csv(report.missed, path)
            content = path.read_text(encoding="utf-8-sig")
            self.assertIn("日期,主题,发件人,原因,Message-ID", content)
            self.assertIn("2026-07-03", content)


if __name__ == "__main__":
    unittest.main()
