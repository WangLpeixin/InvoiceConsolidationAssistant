"""只读对账：列出像发票但未纳入发票池的邮件。"""

from __future__ import annotations

import csv
import email
import imaplib
import re
from dataclasses import dataclass, field
from datetime import date
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path

from invoice import ledger_path, project_root
from invoice.download import (
    DEFAULT_SINCE,
    DownloadLedger,
    IMAP_HOST,
    IMAP_PORT,
    collect_candidate_urls,
    decode_mime_header,
    extract_body,
    imap_since_clause,
    iter_pdf_attachments,
    load_qq_credentials,
    looks_like_invoice,
)
from invoice.fetch_pdf import extract_hrefs, extract_plain_urls

WEIXIN_RE = re.compile(r"weixin\.qq\.com/r/", re.IGNORECASE)
OFD_RE = re.compile(r"\.ofd(?:\?|$)|filename[^&]*\.ofd", re.IGNORECASE)
LOGIN_PORTAL_RE = re.compile(
    r"nnfp\.jss\.com\.cn|nuonuo\.com|vpiaotong\.com|piaotongyun\.com|"
    r"51fapiao\.cn|exct\.net",
    re.IGNORECASE,
)
STATUS_INCLUDED = "included"
STATUS_MISSED = "missed"
STATUS_OTHER = "other"

REASON_WEIXIN = "微信扫码"
REASON_OFD = "OFD"
REASON_LOGIN = "需登录网页"
REASON_FETCH = "拉取失败"
REASON_NO_CLUE = "无线索"


@dataclass(frozen=True)
class AuditRow:
    status: str
    reason: str
    when: date | None
    subject: str
    sender: str
    message_id: str


@dataclass
class AuditReport:
    other: int = 0
    included: int = 0
    missed: list[AuditRow] = field(default_factory=list)

    @property
    def invoice_total(self) -> int:
        return self.included + len(self.missed)


def message_date(message: Message) -> date | None:
    raw = message.get("Date")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).date()
    except (TypeError, ValueError, IndexError):
        return None


def collect_all_urls(text: str, html_text: str) -> list[str]:
    """正文里出现过的 http(s) 链接，用于判断漏票原因。"""
    seen: set[str] = set()
    ordered: list[str] = []
    for url in [*extract_plain_urls(text), *extract_plain_urls(html_text), *extract_hrefs(html_text)]:
        if url in seen:
            continue
        seen.add(url)
        ordered.append(url)
    return ordered


def has_ofd_part(message: Message) -> bool:
    for part in message.walk():
        if part.is_multipart():
            continue
        name = decode_mime_header(part.get_filename())
        if name and OFD_RE.search(name):
            return True
        content_type = (part.get_content_type() or "").lower()
        if "ofd" in content_type:
            return True
    return False


def miss_reason(message: Message, text: str, html_text: str) -> str:
    """未纳入时的原因，按更具体的线索优先。"""
    urls = collect_all_urls(text, html_text)
    blob = "\n".join(urls)
    if any(WEIXIN_RE.search(url) for url in urls) or WEIXIN_RE.search(blob):
        return REASON_WEIXIN
    if has_ofd_part(message) or any(OFD_RE.search(url) for url in urls):
        return REASON_OFD
    if any(LOGIN_PORTAL_RE.search(url) for url in urls):
        return REASON_LOGIN
    if collect_candidate_urls(text, html_text, looks_invoice=True):
        return REASON_FETCH
    return REASON_NO_CLUE


def classify_invoice_mail(message: Message, ledger: DownloadLedger) -> AuditRow:
    """把一封邮件标成其它 / 已纳入 / 未纳入。"""
    subject = decode_mime_header(message.get("Subject"))
    sender = decode_mime_header(message.get("From"))
    message_id = (message.get("Message-ID") or "").strip()
    when = message_date(message)
    text, html_text = extract_body(message)

    if not looks_like_invoice(subject, text, html_text):
        return AuditRow(STATUS_OTHER, "", when, subject, sender, message_id)

    attachments = iter_pdf_attachments(message)
    if attachments or (message_id and message_id in ledger.message_ids):
        return AuditRow(STATUS_INCLUDED, "", when, subject, sender, message_id)

    reason = miss_reason(message, text, html_text)
    return AuditRow(STATUS_MISSED, reason, when, subject, sender, message_id)


def format_audit_report(report: AuditReport) -> str:
    lines = [
        (
            f"未纳入 {len(report.missed)} 封"
            f"（发票邮件 {report.invoice_total} / 已纳入 {report.included} / "
            f"其它邮件 {report.other}）"
        ),
        "",
    ]
    if not report.missed:
        lines.append("没有发现未纳入的发票邮件。")
        return "\n".join(lines)
    for row in report.missed:
        day = row.when.isoformat() if row.when else "未知日期"
        subject = row.subject.replace("\n", " ").strip() or "(无主题)"
        if len(subject) > 40:
            subject = subject[:39] + "…"
        lines.append(f"{day}  {subject:<40}  {row.reason}")
    return "\n".join(lines)


def write_audit_csv(rows: list[AuditRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["日期", "主题", "发件人", "原因", "Message-ID"])
        for row in rows:
            writer.writerow(
                [
                    row.when.isoformat() if row.when else "",
                    row.subject,
                    row.sender,
                    row.reason,
                    row.message_id,
                ]
            )


def audit_inbox(
    root: Path | None = None,
    since: date = DEFAULT_SINCE,
) -> AuditReport:
    """连接收件箱做只读分类，不下载、不改账本。"""
    root = root or project_root()
    ledger = DownloadLedger.load(ledger_path(root))
    user, auth_code = load_qq_credentials()
    report = AuditReport()
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
        for num in numbers:
            typ, fetched = mail.fetch(num, "(RFC822)")
            if typ != "OK" or not fetched or fetched[0] is None:
                continue
            raw = fetched[0][1]
            if not isinstance(raw, (bytes, bytearray)):
                continue
            message = email.message_from_bytes(raw)
            row = classify_invoice_mail(message, ledger)
            if row.status == STATUS_OTHER:
                report.other += 1
            elif row.status == STATUS_INCLUDED:
                report.included += 1
            else:
                report.missed.append(row)
    except imaplib.IMAP4.error as exc:
        raise RuntimeError(
            f"IMAP 登录失败：{exc}。请确认已开启 IMAP，且 .env 里是授权码而不是登录密码。"
        ) from exc
    finally:
        try:
            mail.logout()
        except Exception:
            pass

    report.missed.sort(key=lambda item: (item.when or date.min, item.subject))
    return report
