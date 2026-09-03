"""HTTP 拿不到 PDF 时，用无头浏览器点「下载」并截获文件。

不自动填登录、不破解验证码。同一轮 download 复用一个 Chromium 实例。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from invoice.fetch_pdf import FetchOutcome, fetch_invoice_pdf, is_ofd_bytes, is_pdf_bytes

GOTO_MS = 25000
DOWNLOAD_MS = 12000
CLICK_SELECTORS = (
    "a:has-text('下载电子发票')",
    "button:has-text('下载电子发票')",
    "a:has-text('下载发票')",
    "button:has-text('下载发票')",
    "a:has-text('下载PDF')",
    "a:has-text('PDF')",
    "button:has-text('下载')",
    "a:has-text('下载')",
)


def page_looks_captcha(page: Any) -> bool:
    """页面明显在要验证码或账密时放弃。"""
    try:
        if page.locator("input[type='password']").count() > 0:
            return True
        if page.get_by_text("验证码").count() > 0 and page.locator("input").count() > 0:
            return True
    except Exception:
        return False
    return False


def _read_download_pdf(download: Any) -> bytes | None:
    try:
        path = download.path()
        if path is None:
            return None
        data = path.read_bytes() if hasattr(path, "read_bytes") else open(path, "rb").read()
        if is_pdf_bytes(data):
            return data
        if is_ofd_bytes(data):
            return None
    except Exception:
        return None
    return None


def fetch_pdf_in_page(page: Any, url: str) -> FetchOutcome:
    """在已有 page 上打开 URL，听 PDF 响应并尝试点击下载。"""
    collected: list[bytes] = []

    def on_response(response: Any) -> None:
        try:
            headers = {k.lower(): v for k, v in (response.headers or {}).items()}
            ctype = headers.get("content-type", "")
            resp_url = response.url or ""
            if "pdf" not in ctype.lower() and not resp_url.lower().split("?")[0].endswith(".pdf"):
                return
            body = response.body()
            if is_pdf_bytes(body):
                collected.append(body)
        except Exception:
            return

    page.on("response", on_response)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=GOTO_MS)
    except Exception:
        if collected:
            return FetchOutcome(collected[0], "ok", url)
        return FetchOutcome(None, "error", url)

    if collected:
        return FetchOutcome(collected[0], "ok", page.url or url)

    if page_looks_captcha(page):
        return FetchOutcome(None, "login", page.url or url)

    for selector in CLICK_SELECTORS:
        locator = page.locator(selector)
        try:
            if locator.count() == 0:
                continue
            target = locator.first
            if not target.is_visible():
                continue
            with page.expect_download(timeout=DOWNLOAD_MS) as pending:
                target.click()
            data = _read_download_pdf(pending.value)
            if data:
                return FetchOutcome(data, "ok", page.url or url)
        except Exception:
            if collected:
                return FetchOutcome(collected[0], "ok", page.url or url)
            continue

    if collected:
        return FetchOutcome(collected[0], "ok", page.url or url)
    return FetchOutcome(None, "html_no_pdf", page.url or url)


class PdfFetcher:
    """先 urllib，失败再用 Playwright。可注入 urllib/browser 便于单测。"""

    def __init__(
        self,
        *,
        http_fetch: Callable[[str], FetchOutcome] = fetch_invoice_pdf,
        enable_browser: bool = True,
    ) -> None:
        self._http_fetch = http_fetch
        self._enable_browser = enable_browser
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None

    def __enter__(self) -> PdfFetcher:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def fetch(self, url: str) -> FetchOutcome:
        outcome = self._http_fetch(url)
        if outcome.pdf_bytes:
            return outcome
        if outcome.reason in {"ofd", "too_large"}:
            return outcome
        if not self._enable_browser:
            return outcome
        browser_outcome = self._browser_fetch(url)
        if browser_outcome.pdf_bytes:
            return browser_outcome
        # 浏览器也失败时保留原先原因（登录/错误），避免全变成 html_no_pdf
        if outcome.reason == "login":
            return outcome
        return browser_outcome

    def _ensure_browser(self) -> bool:
        if self._context is not None:
            return True
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return False
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
            self._context = self._browser.new_context(accept_downloads=True)
        except Exception:
            self.close()
            return False
        return True

    def _browser_fetch(self, url: str) -> FetchOutcome:
        if not self._ensure_browser() or self._context is None:
            return FetchOutcome(None, "error", url)
        page = self._context.new_page()
        try:
            return fetch_pdf_in_page(page, url)
        except Exception:
            return FetchOutcome(None, "error", url)
        finally:
            try:
                page.close()
            except Exception:
                pass

    def close(self) -> None:
        for closer in (self._context, self._browser, self._playwright):
            if closer is None:
                continue
            try:
                if hasattr(closer, "close"):
                    closer.close()
                elif hasattr(closer, "stop"):
                    closer.stop()
            except Exception:
                pass
        self._context = None
        self._browser = None
        self._playwright = None
