from __future__ import annotations

import unittest
from io import BytesIO

from openpyxl import load_workbook

from app.sermon_review import build_xlsx
from tests.fixtures.sermon_review.build_fixtures import make_golden_sermon


class XlsxExportTests(unittest.TestCase):
    def test_strips_xml_forbidden_control_characters(self) -> None:
        sermon = make_golden_sermon()
        sermon.segments[0].original = "first\x00 second\x0b third\x1f"
        sermon.segments[0].notes = "line one\nline two\tkept"

        workbook = load_workbook(BytesIO(build_xlsx(sermon)))
        sheet = workbook["Sermon Review"]

        self.assertEqual(sheet["D2"].value, "first second third")
        self.assertEqual(sheet["G2"].value, "line one\nline two\tkept")


if __name__ == "__main__":
    unittest.main()
