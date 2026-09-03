"""命令行入口：download / pack。"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv

from invoice import pool_dir, project_root
from invoice.amount import yuan_to_fen
from invoice.audit import audit_inbox, format_audit_report, write_audit_csv
from invoice.download import DEFAULT_SINCE, download_invoices, format_download_stats
from invoice.pack import format_pack_report, pack_invoices, resolve_dest_dir


def _configure_stdio() -> None:
    """Windows 控制台默认 GBK，中文结果改走 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    root = project_root()
    load_dotenv(root / ".env")
    parser = argparse.ArgumentParser(description="QQ 邮箱发票下载与凑单")
    sub = parser.add_subparsers(dest="command", required=True)

    download_parser = sub.add_parser("download", help="从 QQ 收件箱下载发票 PDF")
    download_parser.add_argument(
        "--since",
        default=DEFAULT_SINCE.isoformat(),
        help="起始日期 YYYY-MM-DD，默认 2026-06-17",
    )
    download_parser.add_argument(
        "--rescan",
        action="store_true",
        help="忽略已记录的 Message-ID，重扫邮件（仍按文件哈希去重）",
    )

    pack_parser = sub.add_parser("pack", help="按目标金额从发票池凑单并移动")
    pack_parser.add_argument("--amount", required=True, help="目标金额（元），例如 200 或 50.5")
    pack_parser.add_argument("--folder", required=True, help="目标文件夹名，建在仓库根目录下")
    pack_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印方案，不移动文件",
    )

    audit_parser = sub.add_parser("audit", help="列出未纳入的发票邮件")
    audit_parser.add_argument(
        "--since",
        default=DEFAULT_SINCE.isoformat(),
        help="起始日期 YYYY-MM-DD，默认 2026-06-17",
    )
    audit_parser.add_argument(
        "--out",
        help="把未纳入清单导出为 CSV",
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "download":
            return _cmd_download(root, args.since, args.rescan)
        if args.command == "audit":
            return _cmd_audit(root, args.since, args.out)
        return _cmd_pack(root, args.amount, args.folder, args.dry_run)
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _cmd_download(root: Path, since_text: str, rescan: bool) -> int:
    since = _parse_date(since_text)
    stats = download_invoices(root=root, since=since, rescan=rescan)
    print(format_download_stats(stats))
    return 0


def _cmd_audit(root: Path, since_text: str, out: str | None) -> int:
    since = _parse_date(since_text)
    report = audit_inbox(root=root, since=since)
    print(format_audit_report(report))
    if out:
        dest = Path(out)
        if not dest.is_absolute():
            dest = root / dest
        write_audit_csv(report.missed, dest)
        print(f"\n已导出：{dest}")
    return 0


def _cmd_pack(root: Path, amount_text: str, folder: str, dry_run: bool) -> int:
    target_fen = _parse_amount_yuan(amount_text)
    dest_dir = resolve_dest_dir(root, folder)
    report = pack_invoices(
        pool=pool_dir(root),
        dest_dir=dest_dir,
        target_fen=target_fen,
        dry_run=dry_run,
    )
    print(format_pack_report(report, dest_dir=dest_dir, dry_run=dry_run))
    return 0 if report.ok else 2


def _parse_date(text: str) -> date:
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("日期格式应为 YYYY-MM-DD") from exc


def _parse_amount_yuan(text: str) -> int:
    try:
        yuan = Decimal(str(text).strip())
    except InvalidOperation as exc:
        raise ValueError("金额必须是数字，例如 200 或 50.5") from exc
    if yuan <= 0:
        raise ValueError("金额必须大于 0")
    return yuan_to_fen(yuan)


if __name__ == "__main__":
    raise SystemExit(main())
