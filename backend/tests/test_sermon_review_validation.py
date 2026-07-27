# Design Ref: §6.2 — exhaustive coverage of the 11-code rule catalog.
# Plan SC: "Unit tests cover happy-path import, missing-column import,
# duplicate-Segment-ID import, wrong-sermon-ID import, empty-Reviewed
# -Translation import."

from __future__ import annotations

import unittest

from app.sermon_review import (
    Sermon,
    validate_workbook,
)
from app.sermon_review.validation import COLUMNS, PRE_TRANSLATED_COLUMNS

from tests.fixtures.sermon_review.build_fixtures import make_golden_sermon


def _rows_from_sermon(sermon: Sermon) -> list[dict[str, str]]:
    return [
        {
            "Sermon ID": sermon.sermonId,
            "Segment ID": s.segmentId,
            "Segment Order": str(s.order),
            "Original Text": s.original,
            "App Translation": s.appTranslation,
            "Reviewed Translation": s.reviewedTranslation,
            "Notes": s.notes,
            "Status": s.status,
        }
        for s in sermon.segments
    ]


def _codes(report) -> set[str]:
    return {r.code for r in report.rows}


class HappyPathTests(unittest.TestCase):
    def test_unmodified_round_trip_is_all_ok(self) -> None:
        sermon = make_golden_sermon()
        rows = _rows_from_sermon(sermon)
        report = validate_workbook(rows, sermon, headers=list(COLUMNS))

        self.assertEqual(report.summary.total, len(sermon.segments))
        self.assertEqual(report.summary.errored, 0)
        self.assertEqual(report.summary.warned, 0)
        self.assertEqual(report.summary.imported, len(sermon.segments))

    def test_pre_translated_three_column_imports_clean(self) -> None:
        sermon = make_golden_sermon()
        sermon.reviewMode = "pre_translated"
        rows = [
            {
                "Segment ID": s.segmentId,
                "Original Text": s.original,
                "Reviewed Translation": f"Reviewed {s.order}",
            }
            for s in sermon.segments
        ]

        report = validate_workbook(
            rows,
            sermon,
            headers=list(PRE_TRANSLATED_COLUMNS),
            template="pre_translated",
        )

        self.assertEqual(report.summary.errored, 0)
        self.assertEqual(report.summary.warned, 0)
        self.assertEqual(report.summary.imported, len(sermon.segments))
        self.assertEqual(_codes(report), {"OK"})
        self.assertFalse(report.has_errors)

    def test_edited_reviewed_translation_imports_clean(self) -> None:
        sermon = make_golden_sermon()
        rows = _rows_from_sermon(sermon)
        rows[0]["Reviewed Translation"] = "Today, we will look together at the grace of God."
        rows[0]["Status"] = "Reviewed"

        report = validate_workbook(rows, sermon, headers=list(COLUMNS))
        self.assertEqual(report.summary.errored, 0)
        self.assertEqual(report.summary.warned, 0)
        self.assertEqual(report.summary.imported, len(sermon.segments))


class WorkbookLevelErrorTests(unittest.TestCase):
    def test_missing_required_column(self) -> None:
        sermon = make_golden_sermon()
        rows = _rows_from_sermon(sermon)
        for r in rows:
            r.pop("Reviewed Translation", None)
        headers = [c for c in COLUMNS if c != "Reviewed Translation"]

        report = validate_workbook(rows, sermon, headers=headers)
        self.assertIn("MISSING_REQUIRED_COLUMN", _codes(report))
        self.assertTrue(report.has_errors)
        self.assertEqual(report.summary.imported, 0)

    def test_missing_segment_when_row_deleted(self) -> None:
        sermon = make_golden_sermon()
        rows = _rows_from_sermon(sermon)
        rows.pop(2)  # delete S003

        report = validate_workbook(rows, sermon, headers=list(COLUMNS))
        self.assertIn("MISSING_SEGMENT", _codes(report))
        self.assertTrue(report.has_errors)
        missing_segments = [
            r for r in report.rows if r.code == "MISSING_SEGMENT"
        ]
        self.assertEqual(len(missing_segments), 1)
        self.assertEqual(missing_segments[0].segmentId, "S003")


class RowLevelErrorTests(unittest.TestCase):
    def test_wrong_sermon_id(self) -> None:
        sermon = make_golden_sermon()
        rows = _rows_from_sermon(sermon)
        for r in rows:
            r["Sermon ID"] = "srm_different_other"

        report = validate_workbook(rows, sermon, headers=list(COLUMNS))
        self.assertIn("WRONG_SERMON_ID", _codes(report))
        self.assertTrue(report.has_errors)
        self.assertEqual(report.summary.imported, 0)

    def test_unknown_segment_id(self) -> None:
        sermon = make_golden_sermon()
        rows = _rows_from_sermon(sermon)
        rows[0]["Segment ID"] = "S999"

        report = validate_workbook(rows, sermon, headers=list(COLUMNS))
        codes = _codes(report)
        self.assertIn("UNKNOWN_SEGMENT_ID", codes)
        self.assertIn("MISSING_SEGMENT", codes)  # S001 still expected by sermon
        self.assertTrue(report.has_errors)

    def test_duplicate_segment_id(self) -> None:
        sermon = make_golden_sermon()
        rows = _rows_from_sermon(sermon)
        rows.append(dict(rows[1]))  # duplicate S002

        report = validate_workbook(rows, sermon, headers=list(COLUMNS))
        self.assertIn("DUPLICATE_SEGMENT_ID", _codes(report))
        self.assertTrue(report.has_errors)

    def test_invalid_status(self) -> None:
        sermon = make_golden_sermon()
        rows = _rows_from_sermon(sermon)
        rows[0]["Status"] = "Approved"

        report = validate_workbook(rows, sermon, headers=list(COLUMNS))
        self.assertIn("INVALID_STATUS", _codes(report))
        self.assertTrue(report.has_errors)

    def test_status_skip_is_valid(self) -> None:
        sermon = make_golden_sermon()
        rows = _rows_from_sermon(sermon)
        rows[0]["Status"] = "Skip"

        report = validate_workbook(rows, sermon, headers=list(COLUMNS))
        self.assertNotIn("INVALID_STATUS", _codes(report))
        self.assertEqual(report.summary.errored, 0)

    def test_original_text_mutated(self) -> None:
        sermon = make_golden_sermon()
        rows = _rows_from_sermon(sermon)
        rows[2]["Original Text"] = "사용자가 실수로 수정한 원문"

        report = validate_workbook(rows, sermon, headers=list(COLUMNS))
        self.assertIn("ORIGINAL_TEXT_MUTATED", _codes(report))
        self.assertTrue(report.has_errors)


class WarningLevelTests(unittest.TestCase):
    def test_empty_review_is_warning_not_error(self) -> None:
        sermon = make_golden_sermon()
        rows = _rows_from_sermon(sermon)
        rows[0]["Reviewed Translation"] = ""

        report = validate_workbook(rows, sermon, headers=list(COLUMNS))
        self.assertIn("EMPTY_REVIEW", _codes(report))
        self.assertEqual(report.summary.errored, 0)
        self.assertEqual(report.summary.warned, 1)
        self.assertEqual(report.summary.imported, len(sermon.segments))

    def test_empty_pre_translated_review_is_error(self) -> None:
        sermon = make_golden_sermon()
        sermon.reviewMode = "pre_translated"
        rows = [
            {
                "Segment ID": s.segmentId,
                "Original Text": s.original,
                "Reviewed Translation": "Filled",
            }
            for s in sermon.segments
        ]
        rows[0]["Reviewed Translation"] = ""

        report = validate_workbook(
            rows,
            sermon,
            headers=list(PRE_TRANSLATED_COLUMNS),
            template="pre_translated",
        )

        self.assertIn("EMPTY_REVIEW", _codes(report))
        self.assertTrue(report.has_errors)
        self.assertEqual(report.summary.imported, 0)

    def test_whitespace_only_review_is_warning(self) -> None:
        sermon = make_golden_sermon()
        rows = _rows_from_sermon(sermon)
        rows[0]["Reviewed Translation"] = "   \t\n  "

        report = validate_workbook(rows, sermon, headers=list(COLUMNS))
        self.assertIn("EMPTY_REVIEW", _codes(report))

    def test_app_translation_mutated_is_warning(self) -> None:
        sermon = make_golden_sermon()
        rows = _rows_from_sermon(sermon)
        rows[0]["App Translation"] = "Mutated machine output."

        report = validate_workbook(rows, sermon, headers=list(COLUMNS))
        self.assertIn("APP_TRANSLATION_MUTATED", _codes(report))
        self.assertEqual(report.summary.errored, 0)

    def test_excessive_length_warning(self) -> None:
        sermon = make_golden_sermon()
        rows = _rows_from_sermon(sermon)
        rows[0]["Reviewed Translation"] = "a" * 2500

        report = validate_workbook(rows, sermon, headers=list(COLUMNS))
        self.assertIn("EXCESSIVE_LENGTH", _codes(report))
        self.assertEqual(report.summary.errored, 0)


class AtomicityTests(unittest.TestCase):
    def test_any_error_blocks_import_overall(self) -> None:
        sermon = make_golden_sermon()
        rows = _rows_from_sermon(sermon)
        rows[0]["Status"] = "Approved"  # one row errors

        report = validate_workbook(rows, sermon, headers=list(COLUMNS))
        self.assertGreater(report.summary.errored, 0)
        self.assertEqual(report.summary.imported, 0)

    def test_only_warnings_means_import_succeeds_count(self) -> None:
        sermon = make_golden_sermon()
        rows = _rows_from_sermon(sermon)
        rows[0]["Reviewed Translation"] = ""
        rows[1]["Reviewed Translation"] = ""

        report = validate_workbook(rows, sermon, headers=list(COLUMNS))
        self.assertEqual(report.summary.errored, 0)
        self.assertEqual(report.summary.warned, 2)
        self.assertEqual(report.summary.imported, len(sermon.segments))


if __name__ == "__main__":
    unittest.main()
