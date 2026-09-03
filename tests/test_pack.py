"""测试凑单：恰好等于、必须超过、总额不够、dry-run 不改文件。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from invoice.pack import (
    InvoiceFile,
    choose_invoices,
    pack_invoices,
    resolve_dest_dir,
)


def _inv(name: str, fen: int) -> InvoiceFile:
    return InvoiceFile(path=Path(name), fen=fen)


class ChooseInvoicesTests(unittest.TestCase):
    def test_exact_match(self) -> None:
        invoices = [_inv("10_a.pdf", 1000), _inv("20_b.pdf", 2000), _inv("5_c.pdf", 500)]
        result = choose_invoices(invoices, 2500)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.total_fen, 2500)
        self.assertEqual(result.overshoot_fen, 0)
        names = {item.name for item in result.invoices}
        self.assertEqual(names, {"20_b.pdf", "5_c.pdf"})

    def test_must_exceed_cannot_go_under(self) -> None:
        invoices = [_inv("15_a.pdf", 1500), _inv("15_b.pdf", 1500)]
        result = choose_invoices(invoices, 2000)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.total_fen, 3000)
        self.assertEqual(len(result.invoices), 2)

    def test_prefer_fewer_invoices_when_sum_tied(self) -> None:
        invoices = [_inv("10_a.pdf", 1000), _inv("10_b.pdf", 1000), _inv("20_c.pdf", 2000)]
        result = choose_invoices(invoices, 2000)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.total_fen, 2000)
        self.assertEqual(len(result.invoices), 1)
        self.assertEqual(result.invoices[0].name, "20_c.pdf")

    def test_prefer_smaller_overshoot(self) -> None:
        # 6+6=12 优于 6+10=16 或 10+6
        invoices = [_inv("6_a.pdf", 600), _inv("6_b.pdf", 600), _inv("10_c.pdf", 1000)]
        result = choose_invoices(invoices, 1200)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.total_fen, 1200)
        names = {item.name for item in result.invoices}
        self.assertEqual(names, {"6_a.pdf", "6_b.pdf"})

    def test_insufficient_returns_none(self) -> None:
        invoices = [_inv("10_a.pdf", 1000), _inv("20_b.pdf", 2000)]
        self.assertIsNone(choose_invoices(invoices, 10000))

    def test_empty_or_non_positive_target(self) -> None:
        invoices = [_inv("10_a.pdf", 1000)]
        self.assertIsNone(choose_invoices(invoices, 0))
        self.assertIsNone(choose_invoices([], 1000))


class PackMoveTests(unittest.TestCase):
    def test_dry_run_does_not_move(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pool = root / "2026年发票"
            dest = root / "出差报销"
            pool.mkdir()
            (pool / "50.5_a.pdf").write_bytes(b"%PDF-1.4")
            (pool / "150_b.pdf").write_bytes(b"%PDF-1.4")
            report = pack_invoices(pool, dest, target_fen=20000, dry_run=True)
            self.assertTrue(report.ok)
            assert report.result is not None
            self.assertEqual(report.result.total_fen, 20050)
            self.assertFalse(dest.exists())
            self.assertTrue((pool / "50.5_a.pdf").exists())
            self.assertTrue((pool / "150_b.pdf").exists())

    def test_move_when_enough(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pool = root / "2026年发票"
            dest = root / "出差报销"
            pool.mkdir()
            (pool / "50.5_a.pdf").write_bytes(b"a")
            (pool / "150_b.pdf").write_bytes(b"b")
            (pool / "9_c.pdf").write_bytes(b"c")
            report = pack_invoices(pool, dest, target_fen=20000, dry_run=False)
            self.assertTrue(report.ok)
            self.assertTrue((dest / "50.5_a.pdf").exists())
            self.assertTrue((dest / "150_b.pdf").exists())
            self.assertTrue((pool / "9_c.pdf").exists())
            self.assertFalse((pool / "50.5_a.pdf").exists())

    def test_insufficient_does_not_move(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            dest = Path(tmp) / "out"
            pool.mkdir()
            (pool / "10_a.pdf").write_bytes(b"a")
            report = pack_invoices(pool, dest, target_fen=20000, dry_run=False)
            self.assertFalse(report.ok)
            self.assertFalse(dest.exists())
            self.assertTrue((pool / "10_a.pdf").exists())

    def test_resolve_dest_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                resolve_dest_dir(root, "..")
            with self.assertRaises(ValueError):
                resolve_dest_dir(root, "a/b")
            dest = resolve_dest_dir(root, "出差报销")
            self.assertEqual(dest, (root / "出差报销").resolve())


if __name__ == "__main__":
    unittest.main()
