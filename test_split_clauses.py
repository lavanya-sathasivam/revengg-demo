"""Focused tests for extract.split_clauses().

Covers: section detection (1-4), separate-line headings, invoice list false
positives, signature-block exclusion, the original-text preservation
invariant, and defined failure cases (missing section 1, forward gap, empty
text). Also runs a read-only check against contract 1 in app.db when present.

Run:  python -m unittest test_split_clauses -v
"""

import os
import unittest

import db
from extract import split_clauses

# Synthetic sample mirroring the inspected real contract format:
# - sections 1-2 with heading on the same line as the number
# - section 3 with the number alone on its line, heading on the next line
# - section 4 in the combined 'N. HEADING.' form
# - lettered subsections (A./B.) that must NOT split
# - a nested all-caps numbered item inside section 2 (number < expected)
# - sentence-case invoice list items 1.-2. under section 2 (as in the real
#   contract, they are payment-schedule content INSIDE clause 2, never split)
# - signature block after 'IN WITNESS WHEREOF' that must NOT split
SAMPLE = """\
SCHEDULE OF SERVICES
This preamble must not be part of any clause.

1. SERVICES. Consultant shall provide the services described herein.
   A. Subsection lettered line, never a section.
   B. Another subsection.
2. PAYMENTS. Company shall pay within thirty days.
   1. TOTAL AMOUNT DUE (nested all-caps item, must be ignored).

1. Total invoice/payment requested;
2. CONSULTANT's final invoice due upon completion.

3.
TERM. This Agreement shall commence on the Effective Date.
4. EARLY TERMINATION. Either party may terminate with notice.

IN WITNESS WHEREOF, the parties have executed this Agreement.

1.  CONSULTANT
2.  SANTA CRUZ PROJECTS, INC.
3. APPROVED AS TO INSURANCE:
4. APPROVED AS TO FORM:
"""


class SplitClausesSyntheticTests(unittest.TestCase):
    def test_detects_sections_1_to_4(self):
        clauses = split_clauses(SAMPLE)
        self.assertEqual(
            [c["section_number"] for c in clauses], ["1", "2", "3", "4"]
        )

    def test_separate_line_heading_number_alone_on_line(self):
        clauses = split_clauses(SAMPLE)
        clause3 = clauses[2]
        self.assertTrue(clause3["clause_text"].startswith("3."))
        self.assertIn("TERM.", clause3["clause_text"].split("\n", 1)[1][:30])
        self.assertIn("Effective Date", clause3["clause_text"])

    def test_invoice_list_and_signature_block_not_split(self):
        clauses = split_clauses(SAMPLE)
        joined = "".join(c["clause_text"] for c in clauses)
        # Invoice items are payment content inside clause 2, not new clauses.
        self.assertIn("Total invoice", clauses[1]["clause_text"])
        self.assertEqual(len(clauses), 4)
        self.assertNotIn("IN WITNESS", joined)
        for c in clauses:
            self.assertNotIn("APPROVED AS TO", c["clause_text"])

    def test_lettered_subsections_stay_inside_clause(self):
        clauses = split_clauses(SAMPLE)
        self.assertIn("Subsection lettered line", clauses[0]["clause_text"])
        self.assertIn("Another subsection", clauses[0]["clause_text"])

    def test_clause_boundaries_cover_expected_text(self):
        clauses = split_clauses(SAMPLE)
        joined = "".join(c["clause_text"] for c in clauses)
        # Slices must come from the original string (contiguous, verbatim).
        self.assertEqual(SAMPLE.find(joined) >= 0, True)
        self.assertIn("1. SERVICES.", joined)
        self.assertIn("4. EARLY TERMINATION.", joined)

    def test_preamble_excluded(self):
        clauses = split_clauses(SAMPLE)
        self.assertNotIn("SCHEDULE OF SERVICES", clauses[0]["clause_text"])


class SplitClausesFailureTests(unittest.TestCase):
    def test_missing_section_1_returns_empty(self):
        text = "2. PAYMENTS. Company shall pay.\n3. TERM. Duration.\n"
        self.assertEqual(split_clauses(text), [])

    def test_forward_gap_returns_empty(self):
        text = (
            "1. SERVICES. A.\n"
            "2. PAYMENTS. B.\n"
            "4. TERMINATION. C.\n"
        )
        self.assertEqual(split_clauses(text), [])

    def test_empty_and_whitespace_text(self):
        self.assertEqual(split_clauses(""), [])
        self.assertEqual(split_clauses("   \n\t\n"), [])

    def test_no_numbered_sections_returns_empty(self):
        self.assertEqual(split_clauses("Just a paragraph of text.\nNo numbers."), [])


@unittest.skipUnless(os.path.isfile(db.get_db_path()), "app.db not present")
class SplitClausesRealDocumentTests(unittest.TestCase):
    """Read-only check against contract 1 in app.db."""

    @classmethod
    def setUpClass(cls):
        cls.contract = db.get_contract(1)
        if cls.contract is None:
            raise unittest.SkipTest("contract 1 not in app.db")

    def test_contract_1_yields_24_sections(self):
        clauses = split_clauses(self.contract["raw_text"])
        self.assertEqual(len(clauses), 24)
        self.assertEqual(
            [c["section_number"] for c in clauses],
            [str(i) for i in range(1, 25)],
        )

    def test_contract_1_clause_3_is_term(self):
        clauses = split_clauses(self.contract["raw_text"])
        head = clauses[2]["clause_text"][:60]
        self.assertTrue(head.startswith("3."))
        self.assertIn("TERM.", head)

    def test_contract_1_no_signature_text_in_clauses(self):
        clauses = split_clauses(self.contract["raw_text"])
        for c in clauses:
            self.assertNotIn("IN WITNESS WHEREOF", c["clause_text"])

    def test_contract_1_slices_come_from_original_text(self):
        text = self.contract["raw_text"]
        clauses = split_clauses(text)
        for c in clauses:
            self.assertIn(c["clause_text"], text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
