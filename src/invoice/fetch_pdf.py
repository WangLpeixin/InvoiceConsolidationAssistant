"""从邮件链接 / 网页里把发票 PDF 拉下来。"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
TIMEOUT_SEC = 20
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024

URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
A_TAG_RE = re.compile(
    r"""<a\b[^>]*href\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a>""",
    re.IGNORECASE | re.DOTALL,
)
IMG_SRC_RE = re.compile(
    r"""<img[^>]+src\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
INVOICE_URL_HINT_RE = re.compile(
    r"发票|fapiao|einvoice|e-invoice|dzfp|chinatax|invoice|\.pdf",
    re.IGNORECASE,
)
DOWNLOAD_HINT_RE = re.compile(r"发票|下载", re.IGNORECASE)
SKIP_URL_RE = re.compile(
    r"unsubscribe|mailto:|javascript:|tracking|beacon|facebook\.com|twitter\.com|"
    r"weixin\.qq\.com/r/|aliyuncs\.com/trace|/ewm-bg\.png|tydl-login|"
    r"exct\.net|mail\.songhaoyun\.com/maillog|"
    r"w3\.org/|ns\.adobe\.com|playstation\.com|purl\.org/|"
    r"www\.51fapiao\.cn/?$|fp\.nuonuo\.com/?#?/?$|"
    r"\.png(?:\?|$)|aisino\.cn/ad_slot",
    re.IGNORECASE,
)
LOGIN_HINT_RE = re.compile(r"验证码|captcha|登录|请登录|password|login", re.IGNORECASE)
PDF_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+\.pdf[^"']*)["']""", re.IGNORECASE)


@dataclass(frozen=True)
class FetchOutcome:
    """一次 HTTP 拉取的结果。pdf_bytes 有值才算拿到发票。"""

    pdf_bytes: bytes | None
    reason: str
    source_url: str = ""


def clean_url(raw: str) -> str:
    """还原 HTML 实体并去掉尾巴上的标点。"""
    url = html.unescape(raw).strip()
    return url.rstrip(".,);]>\"'")


def is_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def is_pdf_bytes(data: bytes) -> bool:
    return data.lstrip().startswith(b"%PDF")


def is_ofd_bytes(data: bytes) -> bool:
    head = data[:8]
    return head.startswith(b"%OFD") or head.startswith(b"PK") and b"OFD" in data[:512]


def looks_like_html(data: bytes, content_type: str) -> bool:
    ctype = content_type.lower()
    if "html" in ctype or "xhtml" in ctype:
        return True
    sample = data[:200].lstrip().lower()
    return sample.startswith(b"<!doctype html") or sample.startswith(b"<html")


def extract_hrefs(html_text: str) -> list[str]:
    return [clean_url(item) for item in HREF_RE.findall(html_text)]


def extract_plain_urls(text: str) -> list[str]:
    return [clean_url(item) for item in URL_RE.findall(text)]


def extract_download_anchor_urls(html_text: str) -> list[str]:
    """锚文本含「发票/下载」的 href。短链常用这种，URL 本身没有发票字样。"""
    found: list[str] = []
    for href, inner in A_TAG_RE.findall(html_text):
        inner_text = re.sub(r"<[^>]+>", "", inner)
        if DOWNLOAD_HINT_RE.search(inner_text):
            found.append(clean_url(href))
    return found


def extract_img_srcs(html_text: str) -> list[str]:
    return [clean_url(item) for item in IMG_SRC_RE.findall(html_text)]


def select_candidate_urls(
    urls: list[str],
    *,
    html_text: str = "",
    looks_invoice: bool = False,
) -> list[str]:
    """筛出发票下载候选。广告/退订一律丢掉。"""
    ordered: list[str] = []
    seen: set[str] = set()
    extra = extract_download_anchor_urls(html_text) if looks_invoice else []
    for url in [*urls, *extra]:
        url = clean_url(url)
        if url in seen or not is_http_url(url) or SKIP_URL_RE.search(url):
            continue
        if INVOICE_URL_HINT_RE.search(url) or (looks_invoice and url in extra):
            seen.add(url)
            ordered.append(url)
    if looks_invoice and not ordered:
        # 短链本身没有「发票」字样时，退回正文里少量 http 链接
        for url in urls:
            url = clean_url(url)
            if url in seen or not is_http_url(url) or SKIP_URL_RE.search(url):
                continue
            seen.add(url)
            ordered.append(url)
            if len(ordered) >= 5:
                break
    return ordered


def nested_pdf_urls(html_text: str, base_url: str) -> list[str]:
    """HTML 里再找一层 .pdf 或「下载」链接。"""
    found: list[str] = []
    seen: set[str] = set()
    for href in [
        *PDF_HREF_RE.findall(html_text),
        *extract_download_anchor_urls(html_text),
        *extract_hrefs(html_text),
    ]:
        absolute = urljoin(base_url, clean_url(href))
        if absolute in seen or not is_http_url(absolute) or SKIP_URL_RE.search(absolute):
            continue
        if (
            INVOICE_URL_HINT_RE.search(absolute)
            or DOWNLOAD_HINT_RE.search(href)
            or absolute.lower().endswith(".pdf")
        ):
            seen.add(absolute)
            found.append(absolute)
    return found[:5]


def encode_url_for_request(url: str) -> str:
    """把路径/查询里的中文编成百分号，避免支付宝等 CDN 直接 400。"""
    parts = urlsplit(url)
    try:
        path = quote(parts.path, safe="/%:@+")
        query = urlencode(parse_qsl(parts.query, keep_blank_values=True), encoding="utf-8")
        netloc = parts.netloc.encode("idna").decode("ascii") if parts.netloc else ""
        return urlunsplit((parts.scheme, netloc, path, query, parts.fragment))
    except Exception:
        return url


def request_headers(url: str) -> dict[str, str]:
    """带上来源站点，支付宝 CDN 等需要 Referer 才会给 PDF。"""
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}/"
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "Referer": origin}
    host = (parsed.netloc or "").lower()
    if "alipay" in host:
        headers["Referer"] = "https://www.alipay.com/"
        headers["Accept"] = "application/pdf,*/*"
    return headers


def http_get(url: str, max_bytes: int = MAX_PDF_BYTES) -> tuple[bytes, str, str]:
    """GET 一个 http(s) URL。返回 (body, content_type, 最终 URL)。"""
    if not is_http_url(url):
        raise ValueError("只允许 http/https")
    encoded = encode_url_for_request(url)
    request = Request(
        encoded,
        headers=request_headers(url),
        method="GET",
    )
    with urlopen(request, timeout=TIMEOUT_SEC) as response:
        content_type = response.headers.get("Content-Type", "") or ""
        final_url = response.geturl() or url
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("too_large")
            chunks.append(chunk)
        return b"".join(chunks), content_type, final_url


def fetch_invoice_pdf(url: str, *, follow_html: bool = True) -> FetchOutcome:
    """把链接变成 PDF 字节。登录页 / OFD / 失败都返回 reason，不抛给调用方。"""
    try:
        body, content_type, final_url = http_get(url)
    except ValueError as exc:
        reason = "too_large" if str(exc) == "too_large" else "error"
        return FetchOutcome(None, reason, url)
    except (HTTPError, URLError, TimeoutError, OSError):
        return FetchOutcome(None, "error", url)

    if is_pdf_bytes(body) or "pdf" in content_type.lower():
        if is_pdf_bytes(body):
            return FetchOutcome(body, "ok", final_url)
        return FetchOutcome(None, "error", final_url)

    if is_ofd_bytes(body) or "ofd" in content_type.lower() or final_url.lower().endswith(".ofd"):
        return FetchOutcome(None, "ofd", final_url)

    if looks_like_html(body, content_type):
        try:
            html_text = body.decode("utf-8", errors="replace")
        except Exception:
            html_text = ""
        if LOGIN_HINT_RE.search(html_text):
            return FetchOutcome(None, "login", final_url)
        if not follow_html:
            return FetchOutcome(None, "html_no_pdf", final_url)
        for nested in nested_pdf_urls(html_text, final_url):
            nested_outcome = fetch_invoice_pdf(nested, follow_html=False)
            if nested_outcome.pdf_bytes:
                return nested_outcome
        return FetchOutcome(None, "html_no_pdf", final_url)

    return FetchOutcome(None, "error", final_url)


def fetch_image_bytes(url: str) -> bytes | None:
    """拉远程图片供二维码识别；失败返回 None。"""
    try:
        body, content_type, _ = http_get(url, max_bytes=MAX_IMAGE_BYTES)
    except (ValueError, HTTPError, URLError, TimeoutError, OSError):
        return None
    if "html" in content_type.lower():
        return None
    return body or None
