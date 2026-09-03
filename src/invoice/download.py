"""从 QQ 邮箱 IMAP 下载 2026-06-17 起的发票 PDF，按金额改名前缀后写入发票池。"""

from __future__ import annotations

import email
import hashlib
import imaplib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from email.header import decode_header
from email.message import Message
from pathlib import Path
from urllib.parse import urlparse

from invoice import ledger_path, pool_dir, project_root, unrecognized_dir
from invoice.amount import build_prefixed_filename, resolve_invoice_fen, sanitize_filename
from invoice.browser_pdf import PdfFetcher
from invoice.fetch_pdf import (
    FetchOutcome,
    extract_hrefs,
    extract_img_srcs,
    extract_plain_urls,
    fetch_image_bytes,
    fetch_invoice_pdf,
    is_http_url,
    select_candidate_urls,
)

IMAP_HOST = "imap.qq.com"
IMAP_PORT = 993
DEFAULT_SINCE = date(2026, 6, 17)

# 主题、正文或附件名带「发票」视为发票邮件
INVOICE_HINT_RE = re.compile(r"发票")
PDF_NAME_RE = re.compile(r"\.pdf$", re.IGNORECASE)
IMAGE_TYPES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/bmp",
    "image/webp",
}


FetchPdfFn = Callable[[str], FetchOutcome]
DecodeQrFn = Callable[[bytes], list[str]]
FetchImageFn = Callable[[str], bytes | None]


@dataclass
class DownloadLedger:
    """用 Message-ID 和附件 SHA256 避免重复落盘。"""

    path: Path
    message_ids: set[str] = field(default_factory=set)
    sha256s: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, path: Path) -> DownloadLedger:
        if not path.is_file():
            return cls(path=path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls(path=path)
        return cls(
            path=path,
            message_ids=set(raw.get("message_ids") or []),
            sha256s=set(raw.get("sha256s") or []),
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "message_ids": sorted(self.message_ids),
            "sha256s": sorted(self.sha256s),
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


@dataclass
class DownloadStats:
    scanned_mails: int = 0
    skipped_mails: int = 0
    saved: int = 0
    saved_attachment: int = 0
    saved_link: int = 0
    saved_qr: int = 0
    unrecognized: int = 0
    skipped_dup: int = 0
    skipped_non_pdf: int = 0
    fetch_failed: int = 0


def load_qq_credentials() -> tuple[str, str]:
    """从环境变量读取 QQ 邮箱和 IMAP 授权码。"""
    user = (os.environ.get("QQ_EMAIL") or "").strip()
    auth_code = (os.environ.get("QQ_AUTH_CODE") or "").strip()
    if not user or not auth_code:
        raise RuntimeError(
            "缺少 QQ_EMAIL 或 QQ_AUTH_CODE。请复制 .env.example 为 .env 后填入授权码。"
        )
    return user, auth_code


def decode_mime_header(value: str | None) -> str:
    """解码 Subject / filename 的 MIME 编码。"""
    if not value:
        return ""
    chunks: list[str] = []
    for piece, charset in decode_header(value):
        if isinstance(piece, bytes):
            encoding = charset or "utf-8"
            try:
                chunks.append(piece.decode(encoding, errors="replace"))
            except LookupError:
                chunks.append(piece.decode("utf-8", errors="replace"))
        else:
            chunks.append(piece)
    return "".join(chunks).strip()


def iter_pdf_attachments(message: Message) -> list[tuple[str, bytes]]:
    """取出邮件中的 PDF 附件（含以 .pdf 命名的 inline 部分）。"""
    found: list[tuple[str, bytes]] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = decode_mime_header(part.get_filename())
        content_type = (part.get_content_type() or "").lower()
        if not filename and content_type != "application/pdf":
            continue
        name = filename or "invoice.pdf"
        if not PDF_NAME_RE.search(name) and content_type != "application/pdf":
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        if not PDF_NAME_RE.search(name):
            name = f"{name}.pdf"
        found.append((name, payload))
    return found


def extract_body(message: Message) -> tuple[str, str]:
    """返回 (纯文本, HTML) 正文。"""
    texts: list[str] = []
    htmls: list[str] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = (part.get_content_type() or "").lower()
        if content_type not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        decoded = payload.decode(charset, errors="replace")
        if content_type == "text/html":
            htmls.append(decoded)
        else:
            texts.append(decoded)
    return "\n".join(texts), "\n".join(htmls)


def looks_like_invoice(subject: str, text: str, html_text: str) -> bool:
    blob = f"{subject}\n{text}\n{html_text}"
    return bool(INVOICE_HINT_RE.search(blob))


def collect_candidate_urls(text: str, html_text: str, looks_invoice: bool) -> list[str]:
    """从正文抽出可能的发票下载链接。"""
    raw = [*extract_plain_urls(text), *extract_plain_urls(html_text), *extract_hrefs(html_text)]
    return select_candidate_urls(raw, html_text=html_text, looks_invoice=looks_invoice)


def iter_embedded_images(message: Message) -> list[bytes]:
    images: list[bytes] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = (part.get_content_type() or "").lower()
        if content_type not in IMAGE_TYPES:
            continue
        payload = part.get_payload(decode=True)
        if payload:
            images.append(payload)
    return images


def decode_qr_urls(image_bytes: bytes) -> list[str]:
    """从图片里解出 http(s) 二维码内容。OpenCV 不可用时返回空。"""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return []
    array = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        return []
    detector = cv2.QRCodeDetector()
    found: list[str] = []
    decode_multi = getattr(detector, "detectAndDecodeMulti", None)
    if decode_multi is not None:
        ok, decoded, _points, _straight = decode_multi(image)
        if ok and decoded:
            for text in decoded:
                if text and is_http_url(text.strip()):
                    found.append(text.strip())
    if found:
        return found
    single, _points, _straight = detector.detectAndDecode(image)
    if single and is_http_url(single.strip()):
        return [single.strip()]
    return []


def collect_qr_urls(
    message: Message,
    html_text: str,
    *,
    looks_invoice: bool,
    decode_qr: DecodeQrFn,
    fetch_image: FetchImageFn,
) -> list[str]:
    """附件/内嵌图 + 发票邮件里的远程图片二维码。"""
    urls: list[str] = []
    seen: set[str] = set()
    for payload in iter_embedded_images(message):
        for url in decode_qr(payload):
            if url not in seen:
                seen.add(url)
                urls.append(url)
    if looks_invoice:
        for src in extract_img_srcs(html_text):
            if not is_http_url(src):
                continue
            image = fetch_image(src)
            if not image:
                continue
            for url in decode_qr(image):
                if url not in seen:
                    seen.add(url)
                    urls.append(url)
    return urls


def filename_from_url(url: str) -> str:
    name = Path(urlparse(url).path).name
    if name and PDF_NAME_RE.search(name):
        return sanitize_filename(name)
    return "invoice.pdf"


def unique_path(directory: Path, filename: str) -> Path:
    dest = directory / filename
    if not dest.exists():
        return dest
    stem = dest.stem
    suffix = dest.suffix
    serial = 2
    while True:
        candidate = directory / f"{stem}_{serial}{suffix}"
        if not candidate.exists():
            return candidate
        serial += 1


def imap_since_clause(since: date) -> str:
    """IMAP SINCE 日期，例如 01-Jan-2026。"""
    return since.strftime("%d-%b-%Y")


def download_invoices(
    root: Path | None = None,
    since: date = DEFAULT_SINCE,
    *,
    rescan: bool = False,
) -> DownloadStats:
    """连接 QQ IMAP，把 PDF 发票写入 2026年发票（识别失败进未识别）。"""
    root = root or project_root()
    user, auth_code = load_qq_credentials()
    pool = pool_dir(root)
    unknown = unrecognized_dir(root)
    pool.mkdir(parents=True, exist_ok=True)
    unknown.mkdir(parents=True, exist_ok=True)

    ledger = DownloadLedger.load(ledger_path(root))
    stats = DownloadStats()
    clause = f"(SINCE {imap_since_clause(since)})"

    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        mail.login(user, auth_code)
        typ, _ = mail.select("INBOX", readonly=True)
        if typ != "OK":
            raise RuntimeError("无法打开收件箱 INBOX，请确认 QQ 邮箱已开启 IMAP。")
        typ, data = mail.search(None, clause)
        if typ != "OK":
            raise RuntimeError("IMAP 搜索失败。")
        numbers = data[0].split() if data and data[0] else []
        with PdfFetcher() as fetcher:
            for num in numbers:
                stats.scanned_mails += 1
                typ, fetched = mail.fetch(num, "(RFC822)")
                if typ != "OK" or not fetched or fetched[0] is None:
                    continue
                raw = fetched[0][1]
                if not isinstance(raw, (bytes, bytearray)):
                    continue
                message = email.message_from_bytes(raw)
                ingest_message(
                    message,
                    pool,
                    unknown,
                    ledger,
                    stats,
                    rescan=rescan,
                    fetch_pdf=fetcher.fetch,
                )
    except imaplib.IMAP4.error as exc:
        raise RuntimeError(
            f"IMAP 登录失败：{exc}。请确认已开启 IMAP，且 .env 里是授权码而不是登录密码。"
        ) from exc
    finally:
        try:
            mail.logout()
        except Exception:
            pass
        ledger.save()
    return stats


def ingest_message(
    message: Message,
    pool: Path,
    unknown: Path,
    ledger: DownloadLedger,
    stats: DownloadStats,
    *,
    rescan: bool = False,
    fetch_pdf: FetchPdfFn = fetch_invoice_pdf,
    decode_qr: DecodeQrFn = decode_qr_urls,
    fetch_image: FetchImageFn = fetch_image_bytes,
) -> None:
    """处理一封邮件：附件、链接、二维码。失败且仍有线索时不记 Message-ID。"""
    message_id = (message.get("Message-ID") or "").strip()
    if message_id and (not rescan) and message_id in ledger.message_ids:
        stats.skipped_mails += 1
        return

    subject = decode_mime_header(message.get("Subject"))
    text, html_text = extract_body(message)
    invoice_mail = looks_like_invoice(subject, text, html_text)
    attachments = iter_pdf_attachments(message)
    link_urls = collect_candidate_urls(text, html_text, invoice_mail) if invoice_mail else []
    qr_urls = (
        collect_qr_urls(
            message,
            html_text,
            looks_invoice=invoice_mail,
            decode_qr=decode_qr,
            fetch_image=fetch_image,
        )
        if invoice_mail
        else []
    )

    pdfs: list[tuple[str, bytes, str]] = [(name, payload, "attachment") for name, payload in attachments]
    fetch_failed = False
    seen_urls: set[str] = set()
    for url in link_urls:
        seen_urls.add(url)
        outcome = fetch_pdf(url)
        if outcome.pdf_bytes:
            pdfs.append((filename_from_url(outcome.source_url or url), outcome.pdf_bytes, "link"))
        elif outcome.reason in {"login", "ofd", "html_no_pdf", "error", "too_large"}:
            fetch_failed = True
            print(f"  拉取失败 [{outcome.reason}] {url}", flush=True)
    for url in qr_urls:
        if url in seen_urls:
            continue
        seen_urls.add(url)
        outcome = fetch_pdf(url)
        if outcome.pdf_bytes:
            pdfs.append((filename_from_url(outcome.source_url or url), outcome.pdf_bytes, "qr"))
        elif outcome.reason in {"login", "ofd", "html_no_pdf", "error", "too_large"}:
            fetch_failed = True
            print(f"  拉取失败 [{outcome.reason}] {url}", flush=True)

    if not pdfs:
        if invoice_mail and (link_urls or qr_urls or fetch_failed):
            # 有下载线索但没拿到 PDF：从账本拿掉，下次还能重试
            stats.fetch_failed += 1
            if message_id:
                ledger.message_ids.discard(message_id)
            return
        stats.skipped_non_pdf += 1
        if message_id:
            ledger.message_ids.add(message_id)
        return

    saved_any = False
    prefer_subject = bool(INVOICE_HINT_RE.search(subject))
    for name, payload, source in pdfs:
        digest = hashlib.sha256(payload).hexdigest()
        if digest in ledger.sha256s:
            stats.skipped_dup += 1
            continue
        fen = resolve_invoice_fen(payload, subject=subject, attachment_name=name)
        prefer = prefer_subject or bool(INVOICE_HINT_RE.search(name)) or source in {"link", "qr"}
        if fen is None and not prefer:
            stats.skipped_non_pdf += 1
            continue
        if fen is None:
            dest = unique_path(unknown, sanitize_filename(name))
            dest.write_bytes(payload)
            stats.unrecognized += 1
        else:
            dest_name = build_prefixed_filename(fen, name)
            dest = unique_path(pool, dest_name)
            dest.write_bytes(payload)
            stats.saved += 1
            if source == "attachment":
                stats.saved_attachment += 1
            elif source == "link":
                stats.saved_link += 1
            else:
                stats.saved_qr += 1
        ledger.sha256s.add(digest)
        saved_any = True

    # 拿到文件才记完成；有线索却失败则从账本剔除，避免旧记录挡住重试
    if saved_any:
        if message_id:
            ledger.message_ids.add(message_id)
    elif fetch_failed:
        if message_id:
            ledger.message_ids.discard(message_id)
    elif message_id:
        ledger.message_ids.add(message_id)


def format_download_stats(stats: DownloadStats) -> str:
    return (
        f"扫描邮件 {stats.scanned_mails} 封，"
        f"跳过已处理 {stats.skipped_mails} 封；"
        f"写入发票池 {stats.saved} 张"
        f"（附件 {stats.saved_attachment} / 链接 {stats.saved_link} / 二维码 {stats.saved_qr}），"
        f"未识别 {stats.unrecognized} 张，"
        f"重复 {stats.skipped_dup} 个，"
        f"链接或扫码失败 {stats.fetch_failed} 封。"
    )
