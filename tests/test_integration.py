"""Cross-branch integration tests. Owner: Person B (branch b-graph).

Run:
    py -3.11 -m unittest discover -s tests -v

WHY THIS FILE EXISTS
    The three branches only meet at the edges: B's impact engine, C's
    retrieval and reasoning, A's database. Each branch tests its own code
    well. This file tests the SEAMS - the things that break when each side
    changes and the other side does not know.

    It exists because of two failures that only appear once the branches are
    put together:
      - C's retrieval scored against the demo contracts retrieved an
        authority for 2 of 59 clauses, so the screen's "Relevant
        authorities" panel was always empty; and
      - the LAST word of a clause could never match a keyword, because
        'penalty.' is not the token 'penalty'.

    These tests are deliberately about the seams, not about re-testing
    anyone's own module.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

os.environ["LLM_PROVIDER"] = "mock"   # never call a real API from a test

import db          # noqa: E402  (Person A's module - only for redirecting the db path)
import adapter     # noqa: E402
import impact      # noqa: E402
import reasoning   # noqa: E402  (Person C's module)
import retrieval   # noqa: E402  (Person C's module)

with open(os.path.join(ROOT, "data", "ground_truth.json"), "r", encoding="utf-8") as fh:
    GT = json.load(fh)

CONTRACT_A = 1
CONTRACT_B = 2


class IntegrationTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp_dir = tempfile.mkdtemp(prefix="revengg_integration_")
        cls.db_path = os.path.join(cls.tmp_dir, "test.db")
        cls._orig_get_db_path = db.get_db_path
        db.get_db_path = lambda: cls.db_path
        db.init_db()

        import seed
        seed.seed(db_path=cls.db_path)

    @classmethod
    def tearDownClass(cls):
        db.get_db_path = cls._orig_get_db_path
        shutil.rmtree(cls.tmp_dir, ignore_errors=True)


class TestRetrievalMeetsTheDemoContracts(IntegrationTestCase):
    """C's retrieval has to reach the contracts this project actually demos."""

    def test_the_last_word_of_a_clause_is_matchable(self):
        """'... is not a penalty.' must match the keyword 'penalty'.

        A token containing punctuation is matched as a substring, so before
        this was fixed the clause-final word of every clause was invisible to
        the matcher.
        """
        self.assertEqual(retrieval.normalize_text("is not a penalty."),
                         "is not a penalty")

    def test_a_section_reference_is_not_stripped_into_a_bare_number(self):
        """'1.' and '2(h)' must keep their digits: they are section
        references, and turning them into '1' and '2' makes them match
        citation text they have nothing to do with."""
        self.assertIn("1.", retrieval.normalize_text("Clause 1. done"))
        self.assertIn("2(h)", retrieval.normalize_text("Section 2(h) applies"))

    def test_a_real_clause_retrieves_the_right_authority(self):
        """Clause 8.3 says the late charges are not a penalty. Section 74 of
        the Indian Contract Act is the provision that governs a penalty, and
        it is reached only because the trailing full stop is stripped."""
        row = adapter.get_clause(CONTRACT_A, "8.3")
        found = retrieval.search_authorities(row["clause_text"])
        ids = {r["id"] for r in found}
        self.assertIn("ica_1872_s74_penalty", ids,
                      "Clause 8.3 (a not-a-penalty clause) must reach s.74")

    def test_force_majeure_reaches_a_frustration_authority(self):
        row = adapter.get_clause(CONTRACT_A, "15.1")
        found = retrieval.search_authorities(row["clause_text"])
        self.assertTrue(found, "Clause 15.1 (force majeure) retrieves nothing")

    def test_the_written_amendment_clause_reaches_an_amendment_authority(self):
        row = adapter.get_clause(CONTRACT_A, "17.1")
        found = retrieval.search_authorities(row["clause_text"])
        topics = {t for r in found for t in r.get("applicable_topics", [])}
        self.assertIn("amendment", topics,
                      "Clause 17.1 (amendment in writing) must reach an "
                      "authority about amendment formalities")

    def test_the_corpus_is_reachable_from_the_demo_contracts(self):
        """A retrieval layer that never fires on the demo documents is a
        screen with an empty panel."""
        for cid, floor in ((CONTRACT_A, 0.25), (CONTRACT_B, 0.20)):
            rows = adapter.list_clauses(cid)
            hits = sum(1 for r in rows
                       if retrieval.search_authorities(r["clause_text"]))
            self.assertGreaterEqual(
                hits / float(len(rows)), floor,
                "%s: only %d of %d clauses retrieve an authority"
                % (cid, hits, len(rows)))

    def test_no_authority_is_fabricated(self):
        """A clause with no legal question must return nothing, not a
        plausible-looking invention."""
        row = adapter.get_clause(CONTRACT_A, "1.1")
        found = retrieval.search_authorities(row["clause_text"])
        for result in found:
            self.assertTrue(result["matched_keywords"],
                            "an authority matched with no keyword evidence")
            self.assertTrue(result.get("citation"))


class TestReasoningMeetsTheEngine(IntegrationTestCase):
    """C's narrative layer sits on top of the clause text; it must never
    invent a legal conclusion it cannot support."""

    def test_explanation_has_the_documented_shape(self):
        row = adapter.get_clause(CONTRACT_A, "7.2")
        result = reasoning.explain_with_retrieval(row["clause_text"])
        for key in ("input", "summary", "key_points", "relevant_authorities",
                    "matched_evidence", "uncertainties_and_missing_information",
                    "disclaimer", "meta"):
            self.assertIn(key, result)

    def test_explanation_always_carries_a_disclaimer(self):
        row = adapter.get_clause(CONTRACT_A, "12.3")
        result = reasoning.explain_with_retrieval(row["clause_text"])
        self.assertTrue(result["disclaimer"])

    def test_explanation_of_a_clause_with_no_authority_says_so(self):
        """Honesty over coverage: if nothing matched, it must say that."""
        row = adapter.get_clause(CONTRACT_A, "17.4")
        result = reasoning.explain_with_retrieval(row["clause_text"])
        if not result["relevant_authorities"]:
            self.assertTrue(result["uncertainties_and_missing_information"])
            self.assertIn("authorit", json.dumps(result).lower())

    def test_explanation_never_asserts_a_legal_outcome(self):
        """The engine's output is decision support, not advice."""
        for sec in ("7.2", "8.1", "12.3", "13.1"):
            row = adapter.get_clause(CONTRACT_A, sec)
            result = reasoning.explain_with_retrieval(row["clause_text"])
            blob = json.dumps(result).lower()
            for phrase in ("is enforceable", "is void", "is invalid",
                           "will succeed", "will be upheld", "is liable for"):
                self.assertNotIn(phrase, blob,
                                 "Clause %s asserts a legal outcome: %s"
                                 % (sec, phrase))


class TestImpactContractIsStable(IntegrationTestCase):
    """The shape of /impact is a contract with the screen and with C's
    reasoning layer. It has to stay put."""

    def test_the_twelve_mvp_keys_are_present(self):
        report = impact.analyze_change(CONTRACT_A, "7.2", 30, 15)
        for key in ("changed_clause", "direct", "indirect", "chain",
                    "obligations", "conditions", "consequences", "deadlines",
                    "counterfactual", "doubts", "conflicts", "evidence",
                    "review_points", "ranked_review_points", "summary"):
            self.assertIn(key, report)

    def test_every_doubt_is_evidence_linked(self):
        report = impact.analyze_change(CONTRACT_A, "7.2", 30, 15)
        for doubt in report["doubts"]:
            self.assertTrue(doubt["evidence"])
            for ev in doubt["evidence"]:
                self.assertTrue(ev["id"])
                self.assertTrue(ev["text"])

    def test_a_change_type_with_no_arithmetic_returns_no_rows_and_says_so(self):
        """The screen must be able to tell 'we do not know' from 'broken'."""
        report = impact.analyze_change(CONTRACT_A, "17.2", 1, 2)
        self.assertEqual(report["counterfactual"]["rows"], [])
        self.assertTrue(report["counterfactual"]["notes"])

    def test_the_sla_change_is_modelled(self):
        report = impact.analyze_change(CONTRACT_A, "9.1", 99, 98)
        self.assertTrue(report["counterfactual"]["rows"])
        metrics = [r["metric"] for r in report["counterfactual"]["rows"]]
        self.assertTrue(any("Service level availability" in m for m in metrics))

    def test_the_seam_still_isolates_the_database(self):
        """adapter is the only thing that knows how the data is stored."""
        self.assertTrue(hasattr(adapter, "list_clauses"))
        self.assertTrue(hasattr(adapter, "get_edges"))
        source = open(os.path.join(ROOT, "adapter.py"), encoding="utf-8").read()
        self.assertIn("import db", source)
        # no other branch-B module may talk to db directly
        for name in ("clauses.py", "graph.py", "impact.py", "api_b.py"):
            text = open(os.path.join(ROOT, name), encoding="utf-8").read()
            self.assertNotIn("\nimport db\n", "\n" + text.replace("\r\n", "\n"),
                             "%s imports db directly - it must go through "
                             "adapter" % name)


class TestTheUiServesTheEngine(IntegrationTestCase):
    """The landing page must show the impact analysis, not just the API.

    The screen used to be a single-clause explainer that never called the
    impact engine, so the project's flagship output was invisible in the
    browser. These tests keep the page and the engine tied together.
    """

    def _client(self):
        from fastapi.testclient import TestClient
        from application import create_app

        # Build an isolated demo-mode app; never use the process-wide default
        # database path or an environment-selected upload database.
        return TestClient(create_app(mode="demo", database_path=self.db_path))

    def test_the_homepage_is_the_contract_intelligence_overview(self):
        page = self._client().get("/")
        self.assertEqual(page.status_code, 200)
        body = page.text
        self.assertIn("Contract Intelligence", body)
        self.assertIn('href="/impact-analysis"', body)
        self.assertIn('href="/explain"', body)
        self.assertIn("read-only", body)
        self.assertNotIn('id="upload-form"', body)

    def test_the_impact_workspace_serves_the_engine(self):
        page = self._client().get("/impact-analysis")
        self.assertEqual(page.status_code, 200)
        body = page.text
        self.assertIn("Conflict &amp; Change Impact", body)
        self.assertIn("Analyze change", body)
        self.assertIn("/impact", body)          # it calls the impact engine
        self.assertIn("Uncertainty", body)      # it renders the doubts
        self.assertIn("ranked_review_points", body)
        self.assertIn("priority_reason", body)

    def test_the_impact_page_uploads_and_selects_the_returned_contract(self):
        page = self._client().get("/impact-analysis")
        self.assertEqual(page.status_code, 200)
        body = page.text
        self.assertIn('id="upload-form"', body)
        self.assertIn('id="upload-file"', body)
        self.assertIn('fetch("/upload"', body)
        self.assertIn("loadContracts(payload.contract_id)", body)
        self.assertIn('fetch("/clauses/" + encodeURIComponent(cid)', body)
        self.assertIn("contract_id: parseInt(requestedContractId, 10)", body)
        self.assertIn('id="clause-preview-text"', body)
        self.assertIn("No numbered clauses were detected", body)
        self.assertIn("ask an administrator to rebuild this contract's graph", body)
        self.assertIn("Results from another selection were cleared.", body)

    def test_clause_explainer_carries_selected_contract_to_impact_page(self):
        html = open(os.path.join(ROOT, "templates", "explain.html"),
                    encoding="utf-8").read()
        self.assertIn('id="impact-analysis-link"', html)
        self.assertIn('"/impact-analysis?contract=" + encodeURIComponent(String(selectedContractId))',
                      html)

    def test_the_clause_explainer_is_still_reachable(self):
        """The existing explanation workspace is preserved, not replaced."""
        page = self._client().get("/explain")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Clause Explanation", page.text)
        self.assertIn('href="/impact-analysis"', page.text)

    def test_the_page_exposes_shareable_parameters(self):
        """?clause=7.2&old=30&new=15 must load and run the analysis."""
        html = open(os.path.join(ROOT, "templates", "index.html"),
                    encoding="utf-8").read()
        self.assertIn("clause", html)
        self.assertIn("URLSearchParams", html)

    def test_the_page_and_the_engine_agree_on_the_keys(self):
        """Every field the page renders must exist in the engine's payload."""
        report = impact.analyze_change(CONTRACT_A, "7.2", 30, 15)
        for key in ("changed_clause", "direct", "indirect", "chain",
                    "obligations", "conditions", "consequences", "deadlines",
                    "rights", "definitions", "counterfactual", "doubts",
                    "evidence", "review_points", "ranked_review_points",
                    "summary"):
            self.assertIn(key, report, "the page renders '%s'" % key)


if __name__ == "__main__":
    unittest.main(verbosity=2)
