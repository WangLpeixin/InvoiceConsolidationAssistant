"""从 QQ 邮箱下载发票 PDF，并按金额凑单。"""

from __future__ import annotations

import os
from pathlib import Path

__version__ = "0.1.0"

POOL_DIRNAME = "2026年发票"
UNRECOGNIZED_DIRNAME = "未识别"
LEDGER_RELATIVE = Path("data") / "downloaded.json"


def project_root() -> Path:
    """定位仓库根目录（含 .env.example 或发票池目录）。"""
    env_root = os.environ.get("INVOICE_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    here = Path(__file__).resolve()
    for candidate in (here.parent, *here.parents):
        if (candidate / ".env.example").is_file() or (candidate / POOL_DIRNAME).is_dir():
            return candidate
    return Path.cwd()


def pool_dir(root: Path | None = None) -> Path:
    """未使用发票所在目录。"""
    return (root or project_root()) / POOL_DIRNAME


def unrecognized_dir(root: Path | None = None) -> Path:
    """金额识别失败的 PDF 目录。"""
    return pool_dir(root) / UNRECOGNIZED_DIRNAME


def ledger_path(root: Path | None = None) -> Path:
    """已下载邮件 / 附件去重账本。"""
    return (root or project_root()) / LEDGER_RELATIVE
