# Design Ref: §2.2 Data Flow (Export + Import) + §7 Security.
# Plan SC: "xlsx round-trip is byte-stable for unmodified files
# (export → import without edits leaves data identical)".

from __future__ import annotations

import unittest

from app.sermon_review import (
    ImportReadError,
    build_xlsx,
    read_workbook,
    validate_workbook,
)
from app.sermon_review.validation import COLUMNS

from tests.fixtures.sermon_review.build_fixtures import (
    bad_csv_bytes,
    bad_duplicate_xlsx,
    bad_missing_column_xlsx,
    bad_missing_row_xlsx,
    bad_mutated_original_xlsx,
    bad_wrong_sermon_xlsx,
    make_golden_sermon,
    valid_unmodified_xlsx,
    valid_with_edits_xlsx,
)


class ExportTests(unittest.TestCase):
    def test_export_produces_nonempty_bytes(self) -> None:
        sermon = make_golden_sermon()
        data = build_xlsx(sermon)
        self.assertIsInstance(data, bytes)
        self.assertGreater(len(data), 100)

    def test_export_starts_with_zip_magic(self) -> None:
        # PK\x03\x04 — .xlsx is a zip container
        sermon = make_golden_sermon()
        data = build_xlsx(sermon)
        self.assertEqual(data[:4], b"PK\x03\x04")

    def test_export_is_deterministic_data(self) -> None:
        sermon = make_golden_sermon()
        data1 = build_xlsx(sermon)
        rows1, headers1 = read_workbook(data1)
        data2 = build_xlsx(sermon)
        rows2, headers2 = read_workbook(data2)
        self.assertEqual(rows1, rows2)
        self.assertEqual(headers1, headers2)


class RoundTripTests(unittest.TestCase):
    def test_unmodified_round_trip_preserves_all_fields(self) -> None:
        sermon = make_golden_sermon()
        data = valid_unmodified_xlsx(sermon)
        rows, headers = read_workbook(data)

        self.assertEqual(list(headers), list(COLUMNS))
        self.assertEqual(len(rows), len(sermon.segments))
        for row, segment in zip(rows, sermon.segments):
            self.assertEqual(row["Sermon ID"], sermon.sermonId)
            self.assertEqual(row["Segment ID"], segment.segmentId)
            self.assertEqual(row["Segment Order"], str(segment.order))
            self.assertEqual(row["Original Text"], segment.original)
            self.assertEqual(row["App Translation"], segment.appTranslation)
            self.assertEqual(
                row["Reviewed Translation"], segment.reviewedTranslation
            )
            self.assertEqual(row["Notes"], segment.notes)
            self.assertEqual(row["Status"], segment.status)

    def test_unmodified_round_trip_validates_clean(self) -> None:
        sermon = make_golden_sermon()
        data = valid_unmodified_xlsx(sermon)
        rows, headers = read_workbook(data)
        report = validate_workbook(rows, sermon, headers=headers)

        self.assertEqual(report.summary.errored, 0)
        self.assertEqual(report.summary.warned, 0)
        self.assertEqual(report.summary.imported, len(sermon.segments))

    def test_edited_round_trip_validates_clean(self) -> None:
        sermon = make_golden_sermon()
        data = valid_with_edits_xlsx(sermon)
        rows, headers = read_workbook(data)
        report = validate_workbook(rows, sermon, headers=headers)

        self.assertEqual(report.summary.errored, 0)
        # Edits change Reviewed Translation, Notes, Status — none warn-worthy.
        self.assertEqual(report.summary.warned, 0)
        self.assertEqual(report.summary.imported, len(sermon.segments))


class BadFixtureTests(unittest.TestCase):
    def test_missing_row_fails_with_missing_segment(self) -> None:
        sermon = make_golden_sermon()
        data = bad_missing_row_xlsx(sermon)
        rows, headers = read_workbook(data)
        report = validate_workbook(rows, sermon, headers=headers)
        codes = {r.code for r in report.rows}
        self.assertIn("MISSING_SEGMENT", codes)
        self.assertTrue(report.has_errors)

    def test_duplicate_fails_with_duplicate_id(self) -> None:
        sermon = make_golden_sermon()
        data = bad_duplicate_xlsx(sermon)
        rows, headers = read_workbook(data)
        report = validate_workbook(rows, sermon, headers=headers)
        codes = {r.code for r in report.rows}
        self.assertIn("DUPLICATE_SEGMENT_ID", codes)
        self.assertTrue(report.has_errors)

    def test_wrong_sermon_fails_with_wrong_sermon_id(self) -> None:
        sermon = make_golden_sermon()
        data = bad_wrong_sermon_xlsx(sermon)
        rows, headers = read_workbook(data)
        report = validate_workbook(rows, sermon, headers=headers)
        codes = {r.code for r in report.rows}
        self.assertIn("WRONG_SERMON_ID", codes)
        self.assertTrue(report.has_errors)
        # Every row should error — no imports.
        self.assertEqual(report.summary.imported, 0)

    def test_mutated_original_fails(self) -> None:
        sermon = make_golden_sermon()
        data = bad_mutated_original_xlsx(sermon)
        rows, headers = read_workbook(data)
        report = validate_workbook(rows, sermon, headers=headers)
        codes = {r.code for r in report.rows}
        self.assertIn("ORIGINAL_TEXT_MUTATED", codes)
        self.assertTrue(report.has_errors)

    def test_missing_column_fails(self) -> None:
        sermon = make_golden_sermon()
        data = bad_missing_column_xlsx(sermon)
        rows, headers = read_workbook(data)
        report = validate_workbook(rows, sermon, headers=headers)
        codes = {r.code for r in report.rows}
        self.assertIn("MISSING_REQUIRED_COLUMN", codes)
        self.assertTrue(report.has_errors)
        self.assertEqual(report.summary.imported, 0)


class ReaderErrorTests(unittest.TestCase):
    def test_empty_bytes_raises(self) -> None:
        with self.assertRaises(ImportReadError):
            read_workbook(b"")

    def test_non_xlsx_csv_raises(self) -> None:
        with self.assertRaises(ImportReadError):
            read_workbook(bad_csv_bytes())

    def test_random_garbage_raises(self) -> None:
        with self.assertRaises(ImportReadError):
            read_workbook(b"\x00\x01\x02\x03this is not a workbook")

    def test_blank_trailing_rows_are_skipped(self) -> None:
        sermon = make_golden_sermon()
        data = valid_unmodified_xlsx(sermon)
        from io import BytesIO

        from openpyxl import load_workbook

        wb = load_workbook(filename=BytesIO(data))
        ws = wb.active
        for _ in range(3):
            ws.append([None] * len(COLUMNS))
        buf = BytesIO()
        wb.save(buf)

        rows, headers = read_workbook(buf.getvalue())
        self.assertEqual(len(rows), len(sermon.segments))


if __name__ == "__main__":
    unittest.main()
