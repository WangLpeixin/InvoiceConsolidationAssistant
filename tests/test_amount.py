"""测试金额解析与文件名前缀。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from invoice.amount import (
    build_prefixed_filename,
    extract_amount_from_hint,
    extract_amount_from_text,
    fen_to_yuan_text,
    parse_prefixed_filename,
    yuan_to_fen,
)


class FenFormatTests(unittest.TestCase):
    def test_strip_trailing_zeros(self) -> None:
        self.assertEqual(fen_to_yuan_text(5050), "50.5")
        self.assertEqual(fen_to_yuan_text(5000), "50")
        self.assertEqual(fen_to_yuan_text(5055), "50.55")
        self.assertEqual(yuan_to_fen("50.5"), 5050)

    def test_parse_prefixed_filename(self) -> None:
        parsed = parse_prefixed_filename("50.5_滴滴出行.pdf")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed[0], 5050)
        self.assertEqual(parsed[1], "滴滴出行.pdf")

    def test_build_prefixed_filename_avoids_duplicate_prefix(self) -> None:
        self.assertEqual(build_prefixed_filename(5050, "发票.pdf"), "50.5_发票.pdf")
        self.assertEqual(build_prefixed_filename(5050, "50.5_发票.pdf"), "50.5_发票.pdf")
        self.assertEqual(build_prefixed_filename(5050, "50_发票.pdf"), "50.5_发票.pdf")


class ExtractAmountTests(unittest.TestCase):
    def test_jieshui_xiaoxie(self) -> None:
        text = "价税合计（大写）叁拾圆整（小写）¥30.00"
        self.assertEqual(extract_amount_from_text(text), 3000)

    def test_jieshui_yen_only(self) -> None:
        text = "价税合计 ¥128.50"
        self.assertEqual(extract_amount_from_text(text), 12850)

    def test_ambiguous_multiple_yen_without_total(self) -> None:
        text = "单价¥10.00 数量2 其它¥20.00"
        self.assertIsNone(extract_amount_from_text(text))

    def test_hint_from_subject(self) -> None:
        self.assertEqual(extract_amount_from_hint("电子发票金额50.5元"), 5050)
        self.assertEqual(extract_amount_from_hint("滴滴出行 50.5元"), 5050)

    def test_hint_rejects_two_different_yuan(self) -> None:
        self.assertIsNone(extract_amount_from_hint("10元 和 20元"))


if __name__ == "__main__":
    unittest.main()
