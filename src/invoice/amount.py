"""发票金额解析。

统一用「分」做整数运算，文件名前缀用去掉多余尾零的元，例如 50.5_原名.pdf。
"""

from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader

# 文件名合法前缀：50 / 50.5 / 50.50
PREFIX_RE = re.compile(r"^(\d+(?:\.\d+)?)_(.+\.pdf)$", re.IGNORECASE)

# 价税合计、小写金额（增值税电子发票常见排版）
YUAN_TOKEN_RE = re.compile(r"(?:¥|￥)\s*(\d{1,8}(?:\.\d{1,2})?)")
XIAOXIE_RE = re.compile(
    r"小写[）\)]?\s*[：:]*\s*[¥￥]?\s*(\d{1,8}(?:\.\d{1,2})?)"
)
YUAN_UNIT_RE = re.compile(r"(?<!\d)(\d{1,8}(?:\.\d{1,2})?)\s*元")
AMOUNT_LABEL_RE = re.compile(
    r"金额\s*[：:]*\s*[¥￥]?\s*(\d{1,8}(?:\.\d{1,2})?)"
)


def yuan_to_fen(yuan: Decimal | str) -> int:
    """元 → 分，四舍五入到分。"""
    value = yuan if isinstance(yuan, Decimal) else Decimal(str(yuan))
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fen_to_yuan_text(fen: int) -> str:
    """分 → 去尾零的元字符串，如 5050 → '50.5'，5000 → '50'。"""
    yuan = (Decimal(fen) / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(yuan, "f").rstrip("0").rstrip(".")


def parse_yuan_token(token: str) -> int | None:
    """把一段数字文本转成分；非法则返回 None。"""
    token = token.strip().replace(",", "")
    if not token:
        return None
    try:
        fen = yuan_to_fen(token)
    except Exception:
        return None
    if fen <= 0:
        return None
    return fen


def parse_prefixed_filename(name: str) -> tuple[int, str] | None:
    """解析「金额_其余.pdf」。成功返回 (分, 原文件名部分)。"""
    matched = PREFIX_RE.match(name)
    if not matched:
        return None
    fen = parse_yuan_token(matched.group(1))
    if fen is None:
        return None
    return fen, matched.group(2)


def build_prefixed_filename(fen: int, original_name: str) -> str:
    """生成 50.5_原名.pdf；原名已带同一前缀则不重复拼接。"""
    safe = sanitize_filename(original_name)
    parsed = parse_prefixed_filename(safe)
    if parsed is not None and parsed[0] == fen:
        return safe
    if parsed is not None:
        safe = parsed[1]
    prefix = fen_to_yuan_text(fen)
    return f"{prefix}_{safe}"


def sanitize_filename(name: str) -> str:
    """去掉路径成分和 Windows 非法字符，保证仍以 .pdf 结尾。"""
    base = Path(name.replace("\\", "/")).name.strip()
    if not base:
        base = "invoice.pdf"
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base)
    cleaned = cleaned.rstrip(" .")
    if not cleaned.lower().endswith(".pdf"):
        cleaned = f"{cleaned}.pdf"
    if cleaned.lower() == ".pdf":
        cleaned = "invoice.pdf"
    return cleaned


def extract_text_from_pdf_bytes(data: bytes) -> str:
    """抽取 PDF 全部页文本；加密或损坏时返回空串。"""
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                return ""
        pages = []
        for page in reader.pages:
            pages.append(page.extract_text() or "")
        return "\n".join(pages)
    except Exception:
        return ""


def extract_amount_from_text(text: str) -> int | None:
    """从发票正文取价税合计（小写），单位：分。"""
    if not text:
        return None
    compact = re.sub(r"[ \t]", "", text)

    # 优先：价税合计附近的小写金额
    idx = compact.find("价税合计")
    if idx >= 0:
        window = compact[idx : idx + 160]
        xiaoxie = _first_fen(XIAOXIE_RE, window)
        if xiaoxie is not None:
            return xiaoxie
        yen = _last_fen(YUAN_TOKEN_RE, window)
        if yen is not None:
            return yen

    # 全文小写
    xiaoxie = _first_fen(XIAOXIE_RE, compact)
    if xiaoxie is not None:
        return xiaoxie

    # 全文只有一个 ¥ 金额时采用，多个则视为歧义
    yen_all = _all_fen(YUAN_TOKEN_RE, compact)
    if len(yen_all) == 1:
        return yen_all[0]
    return None


def extract_amount_from_hint(text: str) -> int | None:
    """从邮件主题或附件名里猜金额（正文抽不到时的退路）。"""
    if not text:
        return None
    compact = re.sub(r"[ \t]", "", text)
    labeled = _first_fen(AMOUNT_LABEL_RE, compact)
    if labeled is not None:
        return labeled
    # 主题里「50.5元」一类；多个不同金额则放弃，避免误用发票代码
    units = _all_fen(YUAN_UNIT_RE, compact)
    unique = list(dict.fromkeys(units))
    if len(unique) == 1:
        return unique[0]
    yen = _all_fen(YUAN_TOKEN_RE, compact)
    unique_yen = list(dict.fromkeys(yen))
    if len(unique_yen) == 1:
        return unique_yen[0]
    return None


def resolve_invoice_fen(
    pdf_bytes: bytes,
    subject: str = "",
    attachment_name: str = "",
) -> int | None:
    """按 PDF 正文 → 主题 → 附件名 的顺序解析金额。"""
    text = extract_text_from_pdf_bytes(pdf_bytes)
    from_pdf = extract_amount_from_text(text)
    if from_pdf is not None:
        return from_pdf
    from_subject = extract_amount_from_hint(subject)
    if from_subject is not None:
        return from_subject
    return extract_amount_from_hint(attachment_name)


def _first_fen(pattern: re.Pattern[str], text: str) -> int | None:
    matched = pattern.search(text)
    if not matched:
        return None
    return parse_yuan_token(matched.group(1))


def _last_fen(pattern: re.Pattern[str], text: str) -> int | None:
    matches = list(pattern.finditer(text))
    if not matches:
        return None
    return parse_yuan_token(matches[-1].group(1))


def _all_fen(pattern: re.Pattern[str], text: str) -> list[int]:
    result = []
    for matched in pattern.finditer(text):
        fen = parse_yuan_token(matched.group(1))
        if fen is not None:
            result.append(fen)
    return result
