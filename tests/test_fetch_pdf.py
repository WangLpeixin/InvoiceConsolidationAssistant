"""测试从 HTML / HTTP 抽取并拉取发票 PDF。"""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from invoice.fetch_pdf import (
    FetchOutcome,
    encode_url_for_request,
    fetch_invoice_pdf,
    nested_pdf_urls,
    select_candidate_urls,
)


class SelectUrlTests(unittest.TestCase):
    def test_keeps_invoice_hint_and_drops_tracking(self) -> None:
        urls = [
            "https://cdn.example/pixel.gif",
            "https://fp.example/dzfp_abc.pdf",
            "https://list.example/unsubscribe?u=1",
            "https://dm-cn.aliyuncs.com/trace/v1/report?x=1",
        ]
        chosen = select_candidate_urls(urls, looks_invoice=False)
        self.assertEqual(chosen, ["https://fp.example/dzfp_abc.pdf"])

    def test_download_anchor_on_invoice_mail(self) -> None:
        html = (
            '<a href="https://ads.example/x">更多</a>'
            '<a href="https://short.example/a1b2">下载电子发票</a>'
        )
        chosen = select_candidate_urls(
            ["https://ads.example/x", "https://short.example/a1b2"],
            html_text=html,
            looks_invoice=True,
        )
        self.assertIn("https://short.example/a1b2", chosen)
        self.assertNotIn("https://ads.example/x", chosen)

    def test_invoice_mail_falls_back_to_plain_http_when_no_hint(self) -> None:
        urls = ["https://pay.example/inv/download?id=9"]
        chosen = select_candidate_urls(urls, html_text="", looks_invoice=True)
        self.assertEqual(chosen, urls)


class EncodeUrlTests(unittest.TestCase):
    def test_encodes_chinese_query(self) -> None:
        url = "https://mdn.alipayobjects.com/a.pdf?af_fileName=28.00元-发票.pdf"
        encoded = encode_url_for_request(url)
        self.assertTrue(encoded.isascii())
        self.assertIn("af_fileName=", encoded)
        self.assertNotIn("元", encoded)
    def test_nested_pdf_href(self) -> None:
        html = '<a href="/files/a.pdf">下载</a>'
        found = nested_pdf_urls(html, "https://fp.example/page")
        self.assertIn("https://fp.example/files/a.pdf", found)


class FakeResponse:
    def __init__(self, data: bytes, content_type: str, url: str) -> None:
        self._data = data
        self.headers = {"Content-Type": content_type}
        self._url = url
        self._buf = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class FetchPdfTests(unittest.TestCase):
    def test_direct_pdf(self) -> None:
        payload = b"%PDF-1.4 fake-invoice"

        def fake_open(request, timeout=0):  # noqa: ANN001
            return FakeResponse(payload, "application/pdf", request.full_url)

        with patch("invoice.fetch_pdf.urlopen", fake_open):
            outcome = fetch_invoice_pdf("https://fp.example/a.pdf")
        self.assertEqual(outcome.reason, "ok")
        self.assertEqual(outcome.pdf_bytes, payload)

    def test_html_then_nested_pdf(self) -> None:
        html = '<html><a href="https://fp.example/real.pdf">下载发票</a></html>'.encode("utf-8")
        pdf = b"%PDF-1.4 nested"

        def fake_open(request, timeout=0):  # noqa: ANN001
            if request.full_url.endswith("real.pdf"):
                return FakeResponse(pdf, "application/pdf", request.full_url)
            return FakeResponse(html, "text/html", request.full_url)

        with patch("invoice.fetch_pdf.urlopen", fake_open):
            outcome = fetch_invoice_pdf("https://fp.example/page")
        self.assertEqual(outcome.reason, "ok")
        self.assertEqual(outcome.pdf_bytes, pdf)

    def test_login_page(self) -> None:
        html = "<html><body>请登录 输入验证码</body></html>".encode("utf-8")

        def fake_open(request, timeout=0):  # noqa: ANN001
            return FakeResponse(html, "text/html; charset=utf-8", request.full_url)

        with patch("invoice.fetch_pdf.urlopen", fake_open):
            outcome = fetch_invoice_pdf("https://fp.example/need-login")
        self.assertIsNone(outcome.pdf_bytes)
        self.assertEqual(outcome.reason, "login")

    def test_network_error(self) -> None:
        def fake_open(request, timeout=0):  # noqa: ANN001
            raise URLError("offline")

        with patch("invoice.fetch_pdf.urlopen", fake_open):
            outcome = fetch_invoice_pdf("https://fp.example/a.pdf")
        self.assertEqual(outcome, FetchOutcome(None, "error", "https://fp.example/a.pdf"))


class PdfFetcherTests(unittest.TestCase):
    def test_http_success_skips_browser(self) -> None:
        from invoice.browser_pdf import PdfFetcher

        calls = {"browser": 0}

        def http_ok(url: str) -> FetchOutcome:
            return FetchOutcome(b"%PDF-1.4 ok", "ok", url)

        fetcher = PdfFetcher(http_fetch=http_ok, enable_browser=True)
        fetcher._browser_fetch = lambda url: calls.__setitem__("browser", 1) or FetchOutcome(None, "error", url)  # type: ignore[method-assign]
        outcome = fetcher.fetch("https://fp.example/a.pdf")
        self.assertEqual(outcome.reason, "ok")
        self.assertEqual(calls["browser"], 0)

    def test_html_falls_back_to_browser(self) -> None:
        from invoice.browser_pdf import PdfFetcher

        def http_html(url: str) -> FetchOutcome:
            return FetchOutcome(None, "html_no_pdf", url)

        def browser_ok(url: str) -> FetchOutcome:
            return FetchOutcome(b"%PDF-1.4 via-browser", "ok", url)

        fetcher = PdfFetcher(http_fetch=http_html, enable_browser=True)
        fetcher._browser_fetch = browser_ok  # type: ignore[method-assign]
        outcome = fetcher.fetch("https://fp.example/page")
        self.assertEqual(outcome.pdf_bytes, b"%PDF-1.4 via-browser")

    def test_ofd_does_not_open_browser(self) -> None:
        from invoice.browser_pdf import PdfFetcher

        called = {"n": 0}

        def http_ofd(url: str) -> FetchOutcome:
            return FetchOutcome(None, "ofd", url)

        fetcher = PdfFetcher(http_fetch=http_ofd, enable_browser=True)
        fetcher._browser_fetch = lambda url: called.__setitem__("n", 1) or FetchOutcome(b"%PDF", "ok", url)  # type: ignore[method-assign]
        outcome = fetcher.fetch("https://fp.example/a.ofd")
        self.assertEqual(outcome.reason, "ofd")
        self.assertEqual(called["n"], 0)


if __name__ == "__main__":
    unittest.main()
