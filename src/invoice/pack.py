"""从发票池凑出合计 ≥ 目标、超出最少、其次张数最少的一组，并移动文件。"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from invoice.amount import fen_to_yuan_text, parse_prefixed_filename

# 发票池只认根目录下「金额_*.pdf」，不进入「未识别」子目录
PDF_RE = re.compile(r"\.pdf$", re.IGNORECASE)


@dataclass(frozen=True)
class InvoiceFile:
    path: Path
    fen: int

    @property
    def name(self) -> str:
        return self.path.name


@dataclass(frozen=True)
class PackResult:
    invoices: tuple[InvoiceFile, ...]
    total_fen: int
    target_fen: int

    @property
    def overshoot_fen(self) -> int:
        return self.total_fen - self.target_fen


@dataclass(frozen=True)
class PackReport:
    """凑单结论：够则带方案，不够则只给池总额。"""

    result: PackResult | None
    pool_total_fen: int
    target_fen: int
    moved: tuple[Path, ...] = ()

    @property
    def ok(self) -> bool:
        return self.result is not None


def list_pool_invoices(pool: Path) -> list[InvoiceFile]:
    """扫描发票池根目录中带金额前缀的 PDF。"""
    if not pool.is_dir():
        return []
    invoices: list[InvoiceFile] = []
    for path in sorted(pool.iterdir(), key=lambda p: p.name):
        if not path.is_file() or not PDF_RE.search(path.name):
            continue
        parsed = parse_prefixed_filename(path.name)
        if parsed is None:
            continue
        invoices.append(InvoiceFile(path=path, fen=parsed[0]))
    return invoices


def choose_invoices(invoices: list[InvoiceFile], target_fen: int) -> PackResult | None:
    """选出合计 ≥ 目标、超出最小、超出相同则张数更少的子集。

    金额用分。单张 ≥ 目标的发票单独比较；其余用 0-1 背包在
    [目标, 目标 + 最大小票 - 1] 内求最小（合计, 张数）。
    """
    if target_fen <= 0 or not invoices:
        return None
    total = sum(item.fen for item in invoices)
    if total < target_fen:
        return None

    small = [item for item in invoices if item.fen < target_fen]
    big = [item for item in invoices if item.fen >= target_fen]

    candidates: list[PackResult] = []
    if big:
        best_big = min(big, key=lambda item: (item.fen, item.name))
        candidates.append(
            PackResult(invoices=(best_big,), total_fen=best_big.fen, target_fen=target_fen)
        )

    small_result = _choose_from_small(small, target_fen)
    if small_result is not None:
        candidates.append(small_result)

    if not candidates:
        return None
    return min(candidates, key=lambda r: (r.total_fen, len(r.invoices), [i.name for i in r.invoices]))


def _choose_from_small(small: list[InvoiceFile], target_fen: int) -> PackResult | None:
    if not small:
        return None
    amounts = [item.fen for item in small]
    total_small = sum(amounts)
    if total_small < target_fen:
        return None

    max_small = max(amounts)
    # 最小可行合计不会超过 target + max_small - 1（否则可去掉一张仍 ≥ 目标）
    bound = min(total_small, target_fen + max_small - 1)
    n = len(small)
    inf = n + 1
    prev = [inf] * (bound + 1)
    prev[0] = 0
    # take[i][s]：考虑第 i 张后，凑出恰好 s 分时是否用了这张
    take = [bytearray(bound + 1) for _ in range(n)]

    for i, weight in enumerate(amounts):
        curr = prev[:]
        if weight > bound:
            prev = curr
            continue
        for s in range(weight, bound + 1):
            if prev[s - weight] + 1 < curr[s]:
                curr[s] = prev[s - weight] + 1
                take[i][s] = 1
        prev = curr

    best_sum: int | None = None
    best_count = inf
    for s in range(target_fen, bound + 1):
        if prev[s] < inf and (best_sum is None or (s, prev[s]) < (best_sum, best_count)):
            best_sum = s
            best_count = prev[s]
    if best_sum is None:
        return None

    chosen_idx: list[int] = []
    s = best_sum
    for i in range(n - 1, -1, -1):
        if s <= 0:
            break
        if take[i][s]:
            chosen_idx.append(i)
            s -= amounts[i]
    chosen_idx.reverse()
    chosen = tuple(small[i] for i in chosen_idx)
    total_fen = sum(item.fen for item in chosen)
    return PackResult(invoices=chosen, total_fen=total_fen, target_fen=target_fen)


def unique_dest(directory: Path, filename: str) -> Path:
    """目标已存在时加 _2、_3 后缀，避免覆盖。"""
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


def resolve_dest_dir(root: Path, folder_name: str) -> Path:
    """只允许仓库根下的单层文件夹名，防止路径穿越。"""
    name = folder_name.strip().strip("/\\")
    if not name:
        raise ValueError("文件夹名称不能为空")
    dest = Path(name)
    if dest.is_absolute() or ".." in dest.parts or len(dest.parts) != 1:
        raise ValueError("文件夹名称只能是仓库根目录下的单层名称")
    return (root / dest.name).resolve()


def format_pack_report(report: PackReport, dest_dir: Path | None, dry_run: bool) -> str:
    """给人看的中文凑单结果。"""
    target_text = fen_to_yuan_text(report.target_fen)
    pool_text = fen_to_yuan_text(report.pool_total_fen)
    if report.result is None:
        gap = report.target_fen - report.pool_total_fen
        gap_text = fen_to_yuan_text(gap) if gap > 0 else "0"
        return (
            f"池中合计 {pool_text} 元，低于目标 {target_text} 元，"
            f"差额 {gap_text} 元。未移动文件。"
        )

    result = report.result
    lines = [
        f"目标：{target_text} 元",
        (
            f"选中 {len(result.invoices)} 张，合计 {fen_to_yuan_text(result.total_fen)} 元，"
            f"超出 {fen_to_yuan_text(result.overshoot_fen)} 元"
        ),
        "",
    ]
    for item in result.invoices:
        lines.append(f"  {fen_to_yuan_text(item.fen):>10}  {item.name}")
    lines.append("")
    if dry_run:
        dest_hint = dest_dir.name if dest_dir is not None else ""
        lines.append(f"[dry-run] 将移动到「{dest_hint}」，未改动文件。")
    else:
        lines.append(f"已移动到：{dest_dir}")
        for moved in report.moved:
            lines.append(f"  {moved.name}")
    return "\n".join(lines)


def pack_invoices(
    pool: Path,
    dest_dir: Path,
    target_fen: int,
    dry_run: bool = False,
) -> PackReport:
    """凑单；dry_run 只计算不移动。池子不够则不改文件。"""
    invoices = list_pool_invoices(pool)
    pool_total = sum(item.fen for item in invoices)
    chosen = choose_invoices(invoices, target_fen)
    if chosen is None:
        return PackReport(result=None, pool_total_fen=pool_total, target_fen=target_fen)

    moved: list[Path] = []
    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)
        for item in chosen.invoices:
            target = unique_dest(dest_dir, item.path.name)
            shutil.move(str(item.path), str(target))
            moved.append(target)

    return PackReport(
        result=chosen,
        pool_total_fen=pool_total,
        target_fen=target_fen,
        moved=tuple(moved),
    )
