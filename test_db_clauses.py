"""Focused tests for db.save_clauses() and db.list_clauses().

Every test runs against an isolated temporary SQLite database: db.get_db_path
is temporarily redirected, so the project's real app.db is never read or
written.

Run:  python -m unittest test_db_clauses -v
"""

import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import db


class ClauseDbTestCase(unittest.TestCase):
    """Base class: fresh temp database + one real contract row per test."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db", prefix="clause_test_")
        os.close(fd)
        self._orig_get_db_path = db.get_db_path
        db.get_db_path = lambda: self.db_path
        self.addCleanup(self._restore)
        db.init_db()
        conn = db.get_conn()
        try:
            cur = conn.execute(
                """INSERT INTO contracts
                       (filename, upload_timestamp, processing_status, raw_text)
                   VALUES (?, ?, ?, ?)""",
                ("sample.pdf", "2026-01-01T00:00:00", "extracted", "raw text"),
            )
            self.contract_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()

    def _restore(self):
        db.get_db_path = self._orig_get_db_path
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _count_clauses(self, contract_id=None):
        conn = db.get_conn()
        try:
            if contract_id is None:
                row = conn.execute("SELECT COUNT(*) FROM clauses").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM clauses WHERE contract_id = ?",
                    (contract_id,),
                ).fetchone()
            return row[0]
        finally:
            conn.close()

    @staticmethod
    def _sample_clauses():
        return [
            {"section_number": "1", "clause_text": "1. SERVICES. Work shall be done."},
            {"section_number": "2", "clause_text": "2. PAYMENTS. Net thirty days."},
            {"section_number": "3", "clause_text": "3.\nTERM. One year from signing."},
        ]


class SaveAndRetrieveTests(ClauseDbTestCase):
    def test_save_and_retrieve_multiple_clauses(self):
        saved = db.save_clauses(self.contract_id, self._sample_clauses())
        self.assertEqual(saved, 3)

        rows = db.list_clauses(self.contract_id)
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(row["contract_id"], self.contract_id)
            self.assertIn("clause_id", row)
            self.assertIn("section_number", row)
            self.assertIn("clause_text", row)
            self.assertEqual(row["metadata"], {})

    def test_section_number_ordering(self):
        shuffled = [
            {"section_number": "10", "clause_text": "10. TENTH."},
            {"section_number": "2", "clause_text": "2. SECOND."},
            {"section_number": "1", "clause_text": "1. FIRST."},
        ]
        db.save_clauses(self.contract_id, shuffled)
        rows = db.list_clauses(self.contract_id)
        self.assertEqual(
            [r["section_number"] for r in rows], ["1", "2", "10"]
        )

    def test_exact_clause_text_preserved(self):
        tricky = (
            "3.\nTERM. This Agreement \u2014 30 days\twith  double  spaces\n"
            "   and trailing spaces   \n\uF0B7 bullet"
        )
        db.save_clauses(
            self.contract_id, [{"section_number": "1", "clause_text": tricky}]
        )
        rows = db.list_clauses(self.contract_id)
        self.assertEqual(rows[0]["clause_text"], tricky)

    def test_empty_list_handled(self):
        self.assertEqual(db.save_clauses(self.contract_id, []), 0)
        self.assertEqual(db.list_clauses(self.contract_id), [])

    def test_empty_list_clears_previous_clauses(self):
        db.save_clauses(self.contract_id, self._sample_clauses())
        self.assertEqual(db.save_clauses(self.contract_id, []), 0)
        self.assertEqual(db.list_clauses(self.contract_id), [])

    def test_metadata_round_trip(self):
        db.save_clauses(
            self.contract_id,
            [
                {
                    "section_number": "1",
                    "clause_text": "1. TEXT.",
                    "metadata": {"start": 10, "end": 99},
                }
            ],
        )
        rows = db.list_clauses(self.contract_id)
        self.assertEqual(rows[0]["metadata"], {"start": 10, "end": 99})


class DuplicatePreventionTests(ClauseDbTestCase):
    def test_resave_same_clauses_creates_no_duplicates(self):
        clauses = self._sample_clauses()
        db.save_clauses(self.contract_id, clauses)
        db.save_clauses(self.contract_id, clauses)
        self.assertEqual(self._count_clauses(self.contract_id), 3)
        rows = db.list_clauses(self.contract_id)
        self.assertEqual(
            [r["clause_text"] for r in rows],
            [c["clause_text"] for c in clauses],
        )

    def test_resave_with_changed_text_updates_rows(self):
        db.save_clauses(self.contract_id, self._sample_clauses())
        updated = self._sample_clauses()
        updated[0]["clause_text"] = "1. SERVICES. Revised text."
        db.save_clauses(self.contract_id, updated)
        self.assertEqual(self._count_clauses(self.contract_id), 3)
        rows = db.list_clauses(self.contract_id)
        self.assertEqual(rows[0]["clause_text"], "1. SERVICES. Revised text.")


class FailureRollbackTests(ClauseDbTestCase):
    def test_invalid_contract_id_leaves_no_rows(self):
        db.save_clauses(self.contract_id, self._sample_clauses())
        with self.assertRaises(sqlite3.IntegrityError):
            db.save_clauses(9999, self._sample_clauses())
        self.assertEqual(self._count_clauses(9999), 0)
        # the real contract's clauses are untouched
        self.assertEqual(self._count_clauses(self.contract_id), 3)

    def test_database_failure_mid_save_leaves_no_rows(self):
        class FailingCommitConn:
            """Proxy around a real connection whose commit() always fails."""

            def __init__(self, real):
                self._real = real

            def cursor(self):
                return self._real.cursor()

            def rollback(self):
                self._real.rollback()

            def close(self):
                self._real.close()

            def commit(self):
                raise sqlite3.OperationalError("simulated disk failure")

        real_conn = db.get_conn()
        with mock.patch.object(db, "get_conn", return_value=FailingCommitConn(real_conn)):
            with self.assertRaises(sqlite3.OperationalError):
                db.save_clauses(self.contract_id, self._sample_clauses())
        self.assertEqual(self._count_clauses(self.contract_id), 0)
        self.assertEqual(db.list_clauses(self.contract_id), [])

    def test_bad_clause_item_rolls_back_all_rows(self):
        broken = self._sample_clauses() + [{"section_number": "4"}]  # missing clause_text
        with self.assertRaises(KeyError):
            db.save_clauses(self.contract_id, broken)
        self.assertEqual(self._count_clauses(self.contract_id), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
