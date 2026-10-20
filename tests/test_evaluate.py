"""Tests for the evaluation harness, and for the rules the harness locked in.

Owner: Person B (branch b-graph).

Run:
    py -3.11 -m unittest discover -s tests -v

WHY THESE TESTS EXIST
    The evaluation report claims the engine reproduces the hand-annotated
    ground truth exactly. That claim is only worth something if:

      1. the SCORER is correct (a broken harness would report 1.000 for
         anything), and
      2. the rules it locked in stay locked in.

    So this file tests the harness against deliberately WRONG ground truth
    (the negative control), and then re-checks every headline metric.
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
sys.path.insert(0, os.path.join(ROOT, "tools"))

# The test suite must never call a real API, whatever .env says. This has to
# be set BEFORE llm.py is imported, because llm.py loads .env on import.
os.environ["LLM_PROVIDER"] = "mock"

import db          # noqa: E402  (Person A's module - only for redirecting the db path)
import adapter     # noqa: E402
import clauses     # noqa: E402
import graph       # noqa: E402
import impact      # noqa: E402
import llm         # noqa: E402
import evaluate    # noqa: E402  (tools/evaluate.py)

with open(os.path.join(ROOT, "data", "ground_truth.json"), "r", encoding="utf-8") as fh:
    GT = json.load(fh)

CONTRACT_A = 1
CONTRACT_B = 2


class EvaluationTestCase(unittest.TestCase):
    """Base class: the evaluation runs against a throwaway database."""

    @classmethod
    def setUpClass(cls):
        cls.tmp_dir = tempfile.mkdtemp(prefix="revengg_eval_test_")
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


class TestTheScorerItself(unittest.TestCase):
    """A harness that cannot fail is not a harness.

    These tests check the metric arithmetic and prove that the scorer
    actually REPORTS a wrong answer. Without them, a '1.000 everywhere'
    report would be unfalsifiable.
    """

    def test_prf_arithmetic(self):
        m = evaluate.prf(tp=3, fp=1, fn=1)
        self.assertAlmostEqual(m["precision"], 0.75)
        self.assertAlmostEqual(m["recall"], 0.75)
        self.assertAlmostEqual(m["f1"], 0.75)

    def test_prf_with_nothing_to_find(self):
        m = evaluate.prf(tp=0, fp=0, fn=0)
        self.assertEqual(m["precision"], 1.0)
        self.assertEqual(m["recall"], 1.0)

    def test_set_prf_counts_both_directions(self):
        m = evaluate.set_prf(["a", "b", "c"], ["a", "b", "x"])
        self.assertEqual((m["tp"], m["fp"], m["fn"]), (2, 1, 1))

    def test_scorer_reports_a_wrong_answer(self):
        """NEGATIVE CONTROL. A deliberately wrong expectation must not score
        1.000 - otherwise the headline numbers mean nothing."""
        m = evaluate.set_prf(["a", "b"], ["x", "y"])
        self.assertLess(m["f1"], 0.5)
        self.assertEqual((m["tp"], m["fp"], m["fn"]), (0, 2, 2))

    def test_scorer_reports_a_partly_wrong_answer(self):
        m = evaluate.set_prf(["a", "b"], ["a", "z"])
        self.assertLess(m["f1"], 1.0)
        self.assertEqual(m["tp"], 1)


class TestGroundTruthIsSane(EvaluationTestCase):
    """The ground truth file has to be internally consistent, or the metrics
    are measuring nonsense."""

    def test_clause_count_matches_the_inventory(self):
        for key in ("contract_a", "contract_b"):
            numbers = GT[key]["expected_clause_numbers"]
            self.assertEqual(len(numbers), GT[key]["expected_clause_count"],
                             "%s: inventory and count disagree" % key)
            self.assertEqual(len(set(numbers)), len(numbers),
                             "%s: duplicate clause numbers" % key)

    def test_original_types_agree_with_the_full_annotation(self):
        for key in ("contract_a", "contract_b"):
            full = GT[key]["expected_types_full"]
            for sec, ctype in GT[key]["expected_types"].items():
                self.assertIn(sec, full, "%s: %s missing from the full set" % (key, sec))
                self.assertEqual(full[sec], ctype,
                                 "%s: %s disagrees with the full annotation"
                                 % (key, sec))

    def test_full_annotation_covers_every_extracted_clause(self):
        for cid, key in ((CONTRACT_A, "contract_a"), (CONTRACT_B, "contract_b")):
            full = GT[key]["expected_types_full"]
            actual = {r["section_number"] for r in adapter.list_clauses(cid)}
            self.assertEqual(set(full), actual,
                             "%s: annotation and extraction disagree" % key)

    def test_every_annotated_type_is_in_the_frozen_vocabulary(self):
        for key in ("contract_a", "contract_b"):
            for sec, ctype in GT[key]["expected_types_full"].items():
                self.assertIn(ctype, clauses.CLAUSE_TYPES,
                              "%s %s: '%s' is not a clause type" % (key, sec, ctype))

    def test_annotated_numbers_are_found(self):
        for cid, key in ((CONTRACT_A, "contract_a"), (CONTRACT_B, "contract_b")):
            for sec, expected in GT[key]["expected_number_sets"].items():
                row = adapter.get_clause(cid, sec)
                self.assertIsNotNone(row, "%s %s missing" % (key, sec))
                found = {(n["value"], n["unit"])
                         for n in clauses.find_numbers(row["clause_text"])}
                want = {(n["value"], n["unit"]) for n in expected}
                self.assertEqual(found, want, "%s %s: numbers differ" % (key, sec))


class TestHeadlineMetrics(EvaluationTestCase):
    """Every headline metric, re-checked."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.report = evaluate.build_report(GT)

    def test_segmentation(self):
        for key in ("contract_a", "contract_b"):
            seg = self.report["contracts"][key]["segmentation"]
            self.assertEqual(seg["metric"]["f1"], 1.0, key)
            self.assertTrue(seg["count_ok"], key)
            self.assertEqual(seg["missing"], [], key)
            self.assertEqual(seg["extra"], [], key)

    def test_clause_types(self):
        for key in ("contract_a", "contract_b"):
            types = self.report["contracts"][key]["types"]
            self.assertEqual(types["metric"]["f1"], 1.0,
                             "%s: %s" % (key, types["errors"]))
            self.assertEqual(types["errors"], [], key)

    def test_numbers(self):
        for key in ("contract_a", "contract_b"):
            nums = self.report["contracts"][key]["numbers"]
            self.assertEqual(nums["metric"]["f1"], 1.0, key)

    def test_dependency_edges(self):
        for key in ("contract_a", "contract_b"):
            deps = self.report["contracts"][key]["dependencies"]
            self.assertEqual(deps["metric"]["f1"], 1.0, key)
            self.assertEqual(deps["missing"], [], key)
            self.assertEqual(deps["extra"], [], key)

    def test_reference_edges(self):
        for key in ("contract_a", "contract_b"):
            refs = self.report["contracts"][key]["references"]
            self.assertEqual(refs["metric"]["f1"], 1.0, key)

    def test_impact(self):
        for key in ("contract_a", "contract_b"):
            for r in self.report["contracts"][key]["impact"]:
                self.assertEqual(r["direct"]["f1"], 1.0,
                                 "%s %s: direct" % (key, r["clause"]))
                self.assertEqual(r["indirect"]["f1"], 1.0,
                                 "%s %s: indirect" % (key, r["clause"]))
                self.assertTrue(r["chain_ok"],
                                "%s %s: chain %s != %s"
                                % (key, r["clause"], r["chain_actual"],
                                   r["chain_expected"]))

    def test_counterfactual_arithmetic(self):
        for key in ("contract_a", "contract_b"):
            for r in self.report["contracts"][key]["counterfactual"]:
                for c in r["checks"]:
                    self.assertTrue(c["ok"], "%s %s: %s" % (key, r["clause"], c["check"]))

    def test_sla_counterfactual_arithmetic(self):
        """The service-credit exposure rows for an SLA change."""
        for key in ("contract_a", "contract_b"):
            for r in self.report["contracts"][key]["counterfactual_sla"]:
                for c in r["checks"]:
                    self.assertTrue(c["ok"], "%s %s: %s" % (key, r["clause"], c["check"]))

    def test_sla_counterfactual_has_no_calendar_day_arithmetic(self):
        """A percentage change must not produce 'day 45' style rows."""
        cf = impact.counterfactual(CONTRACT_A, "9.1", 99, 98)
        metrics = [r["metric"] for r in cf["rows"]]
        self.assertTrue(metrics)
        for metric in metrics:
            self.assertNotIn("day ", metric)
        self.assertIn("Service level availability (Clause 9.1)", metrics)

    def test_doubts(self):
        doubts = self.report["contracts"]["contract_a"]["doubts"]
        self.assertEqual(doubts["metric"]["f1"], 1.0)
        self.assertEqual(doubts["missing"], [])
        self.assertTrue(doubts["evidence_complete"],
                        "a doubt was raised without evidence")

    def test_sla_doubts(self):
        """An SLA change must raise the schedule-mismatch doubt."""
        sla = self.report["contracts"]["contract_a"]["doubts"]["sla"]
        self.assertIsNotNone(sla)
        self.assertEqual(sla["metric"]["f1"], 1.0, sla["missing"])
        self.assertIn("sla_schedule_mismatch", sla["codes"])

    def test_a_change_of_an_unmodelled_type_says_so(self):
        """A change to, say, the confidentiality clause must not invent
        payment arithmetic."""
        cf = impact.counterfactual(CONTRACT_A, "17.2", 1, 2)
        self.assertEqual(cf["rows"], [])
        self.assertTrue(cf["notes"])

    def test_definition_drift(self):
        b = self.report["contracts"]["contract_b"]["conflicts"]
        self.assertEqual(b["metric"]["f1"], 1.0)
        self.assertTrue(b["values_ok"])

    def test_cross_contract(self):
        xc = self.report["cross_contract"]
        self.assertEqual(xc["metric"]["f1"], 1.0)
        self.assertTrue(xc["values_ok"])

    def test_element_node_numbers_are_grounded(self):
        """A number stated in an element node must occur in its own clause."""
        for key in ("contract_a", "contract_b"):
            ng = self.report["contracts"][key]["node_grounding"]
            self.assertEqual(ng["errors"], [], key)
            self.assertEqual(ng["metric"]["precision"], 1.0, key)

    def test_every_doubt_carries_a_clause_reference(self):
        report = impact.analyze_change(CONTRACT_A, "7.2", 30, 15)
        self.assertTrue(report["doubts"])
        for doubt in report["doubts"]:
            self.assertTrue(doubt["evidence"])
            for ev in doubt["evidence"]:
                self.assertTrue(ev["id"])
                self.assertTrue(ev["text"], "evidence with no quoted text")


class TestClassificationRules(unittest.TestCase):
    """The rules the evaluation locked in, tested one by one."""

    def test_bare_heading_names_the_subject(self):
        self.assertEqual(clauses.classify_type("", "PAYMENT"), "payment")
        self.assertEqual(clauses.classify_type("", "TERM"), "term")
        self.assertEqual(clauses.classify_type("", "INDEMNITY"), "indemnity")
        self.assertEqual(clauses.classify_type("", "LATE PAYMENT"), "fee")
        self.assertEqual(clauses.classify_type("", "TERM AND TERMINATION"),
                         "termination")

    def test_a_mention_of_termination_is_not_a_termination_right(self):
        """Clause 17.4 says 'shall survive termination'. It grants nothing."""
        self.assertEqual(
            clauses.classify_type(
                "Clauses which by their nature are intended to survive, "
                "including Clause 11, shall survive termination.", "Survival"),
            "other")

    def test_a_schedule_is_not_an_operative_clause(self):
        self.assertEqual(
            clauses.classify_type(
                "SCHEDULE A - SERVICES, CHARGES AND ADDRESSES", "SERVICES"),
            "other")

    def test_a_tax_clause_is_not_a_payment_clause(self):
        """The vocabulary has no tax type, so a tax clause is 'other' - even
        though its body mentions an Invoice."""
        self.assertEqual(
            clauses.classify_type(
                "All amounts payable under this Agreement are exclusive of "
                "applicable taxes, which shall be added to the relevant Invoice.",
                "Taxes"),
            "other")

    def test_an_sla_clause_must_state_a_level(self):
        """Clause 3.2 points at the Service Levels but states none."""
        self.assertEqual(
            clauses.classify_type(
                "The Supplier shall perform the Services so as to achieve the "
                "Service Levels set out in Clause 9.1 and shall maintain the "
                "records necessary to demonstrate compliance.",
                "Standard of Performance"),
            "other")
        self.assertEqual(
            clauses.classify_type(
                "The Supplier shall achieve a monthly service availability of "
                "not less than 99% for the Services.", "Service Levels"),
            "sla")

    def test_keywords_match_whole_words_only(self):
        """'term' must not match 'termination'."""
        self.assertEqual(
            clauses.classify_type(
                "The Supplier may terminate this Agreement immediately if the "
                "other Party commits an act of insolvency.", "Insolvency"),
            "termination")
        self.assertNotEqual(
            clauses.classify_type(
                "The Supplier shall provide the Services with reasonable skill "
                "and care and in accordance with the Service Levels.",
                "Standard of Performance"),
            "sla")

    def test_a_definition_is_recognised_by_its_quoted_term(self):
        self.assertEqual(clauses.classify_type(
            '"Payment Due Date" means, in respect of any Invoice, the date '
            'falling 30 (thirty) days after the date of that Invoice.',
            '"Payment Due Date"'),
            "definition")


class TestTheLlmPath(EvaluationTestCase):
    """The LLM fills gaps the rule table leaves.

    These tests drive the REAL ask_json() code path - provider dispatch, JSON
    extraction, parsing, error handling - with the network call replaced. So
    the LLM branch of the graph builder is covered by the suite even with no
    API key, and a regression in it cannot hide until someone has a key.
    """

    def setUp(self):
        self._old_env = dict(os.environ)
        self._old_openai = llm._call_openai
        os.environ["LLM_PROVIDER"] = "openai"
        os.environ["OPENAI_API_KEY"] = "not-a-real-key"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old_env)
        llm._call_openai = self._old_openai

    @staticmethod
    def _stub(answer_for="3.3"):
        """A fake provider: answers for one clause, stays quiet otherwise."""
        def fake_call(prompt):
            if "Clause number: %s" % answer_for in prompt:
                return ('{"depends_on": ["17.1"], "creates": '
                        '[{"kind": "obligation", "detail": "agree changes in writing"}]}')
            return '{"depends_on": [], "creates": []}'
        return fake_call

    def test_the_llm_can_add_a_dependency_the_rules_missed(self):
        """Clause 3.3 references 17.1, and (other, other) is not in the
        relation table - so this is exactly where the LLM is asked."""
        llm._call_openai = self._stub()
        stats = graph.build_graph(CONTRACT_A, use_llm=True)
        edges = {(e["src"], e["dst"], e["rel"])
                 for e in adapter.get_edges(CONTRACT_A)}
        self.assertIn(("3.3", "17.1", "depends_on"), edges,
                      "the LLM's dependency edge was not inserted")
        self.assertGreater(stats["llm_fills"], 0)

    def test_the_llm_can_add_a_node(self):
        llm._call_openai = self._stub()
        graph.build_graph(CONTRACT_A, use_llm=True)
        refs = {n["clause_reference"] for n in adapter.get_nodes(CONTRACT_A)}
        self.assertIn("3.3::obligation", refs)

    def test_ask_json_returns_the_parsed_object(self):
        llm._call_openai = lambda prompt: 'Here you go: {"depends_on": ["8.1"]} done'
        payload = llm.ask_json("give me json")
        self.assertEqual(payload.get("depends_on"), ["8.1"])

    def test_ask_json_never_raises(self):
        def exploding(prompt):
            raise RuntimeError("the network is down")
        llm._call_openai = exploding
        self.assertEqual(llm.ask_json("give me json"), {})

    def test_ask_json_survives_a_useless_answer(self):
        llm._call_openai = lambda prompt: "I am a language model, not a JSON API."
        self.assertEqual(llm.ask_json("give me json"), {})

    def test_the_graph_still_builds_when_the_llm_fails(self):
        def exploding(prompt):
            raise RuntimeError("the network is down")
        llm._call_openai = exploding
        stats = graph.build_graph(CONTRACT_A, use_llm=True)
        self.assertEqual(stats["clauses"], 59)
        edges = {(e["src"], e["dst"], e["rel"])
                 for e in adapter.get_edges(CONTRACT_A)}
        self.assertIn(("8.1", "7.2", "depends_on"), edges)

    def test_with_no_key_the_demo_is_unchanged(self):
        """Remove the key and the rule table does all the work."""
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ["LLM_PROVIDER"] = "mock"
        self.assertEqual(llm.ask_json("give me json"), {})
        self.assertFalse(llm.provider_available())
        stats = graph.build_graph(CONTRACT_A, use_llm=False)
        self.assertEqual(stats["llm_fills"], 0)
        edges = {(e["src"], e["dst"], e["rel"])
                 for e in adapter.get_edges(CONTRACT_A)}
        self.assertIn(("8.1", "7.2", "depends_on"), edges)

    def test_the_llm_is_only_asked_about_the_target_clause(self):
        """A live key must not mean dozens of API calls."""
        llm._call_openai = self._stub()
        stats = graph.build_graph(CONTRACT_A, use_llm=True, llm_target="3.3")
        self.assertEqual(stats["llm_calls"], 1)

    def test_the_llm_call_count_is_capped(self):
        llm._call_openai = self._stub()
        stats = graph.build_graph(CONTRACT_A, use_llm=True, llm_limit=2)
        self.assertLessEqual(stats["llm_calls"], 2)


class TestSlaMechanics(unittest.TestCase):
    """The service-credit mechanics: parsers and the inverse dependency rule."""

    def test_rupee_amounts_are_parsed_with_indian_grouping(self):
        self.assertEqual(clauses.find_amounts("Charges: INR 4,50,000 per month"),
                         [450000])
        self.assertEqual(clauses.find_amounts("Charges: INR 7,80,000 per month"),
                         [780000])
        self.assertEqual(clauses.find_amounts("no money here"), [])

    def test_service_credit_schedule_is_parsed(self):
        parsed = clauses.parse_service_credit_schedule(
            "Service credits for availability below 99%: 5% of the monthly "
            "Charges for each full percentage point below 99%, capped at 25% "
            "of the monthly Charges.")
        self.assertEqual(parsed["threshold_pct"], 99)
        self.assertEqual(parsed["rate_pct"], 5)
        self.assertEqual(parsed["cap_pct"], 25)

    def test_an_unreadable_schedule_returns_none_not_a_guess(self):
        self.assertIsNone(clauses.parse_service_credit_schedule("some other text"))
        self.assertIsNone(clauses.parse_service_credit_schedule(""))

    def test_rupee_formatting_uses_indian_grouping(self):
        self.assertEqual(impact._fmt_inr(450000), "INR 4,50,000")
        self.assertEqual(impact._fmt_inr(112500), "INR 1,12,500")
        self.assertEqual(impact._fmt_inr(22500), "INR 22,500")

    def test_mentions_dispute_ignores_undisputed(self):
        self.assertFalse(clauses.mentions_dispute(
            "the amount stated in each undisputed Invoice within 30 days"))
        self.assertTrue(clauses.mentions_dispute(
            "stating the grounds of dispute"))


class TestNodePrecision(EvaluationTestCase):
    """A node is a legal statement about the contract. It must be true."""

    def test_a_clause_that_imposes_no_fee_creates_no_charge_obligation(self):
        """Clause 8.3 ('... are not a penalty') creates no obligation."""
        node_83 = [n for n in adapter.get_nodes(CONTRACT_A)
                   if n["clause_reference"].startswith("8.3")]
        for n in node_83:
            self.assertNotIn("late charges", n.get("detail", ""))

    def test_service_credits_create_a_right_not_a_charge_obligation(self):
        nodes = [n for n in adapter.get_nodes(CONTRACT_A)
                 if n["clause_reference"].startswith("9.2")]
        self.assertTrue(nodes, "Clause 9.2 creates no node at all")
        for n in nodes:
            self.assertNotIn("late charges", n.get("detail", ""))
        self.assertTrue(any(n["node_type"] == "right" for n in nodes))

    def test_late_charges_clause_still_creates_its_obligation(self):
        nodes = [n for n in adapter.get_nodes(CONTRACT_A)
                 if n["clause_reference"].startswith("8.1")]
        self.assertTrue(any("late charge" in n.get("detail", "") for n in nodes))

    def test_a_payment_clause_does_not_get_a_dispute_deadline(self):
        """Clause 7.2 pays 'each undisputed Invoice'. The word 'undisputed'
        contains 'dispute', but Clause 7.2 imposes no dispute-notice duty -
        only Clause 7.3 does. A node is a legal statement, so it must be
        true."""
        nodes = [n for n in adapter.get_nodes(CONTRACT_A)
                 if n["clause_reference"].startswith("7.2::")]
        deadlines = [n for n in nodes if n["node_type"] == "deadline"]
        self.assertTrue(deadlines, "Clause 7.2 has no deadline node")
        for n in deadlines:
            self.assertNotIn("dispute", n.get("detail", ""))
        self.assertTrue(any("pay within" in n.get("detail", "") for n in deadlines))

    def test_the_dispute_clause_keeps_its_dispute_deadline(self):
        nodes = [n for n in adapter.get_nodes(CONTRACT_A)
                 if n["clause_reference"].startswith("7.3::")]
        deadlines = [n for n in nodes if n["node_type"] == "deadline"]
        self.assertTrue(any("dispute" in n.get("detail", "") for n in deadlines))


class TestTheReportRenders(EvaluationTestCase):
    """The report is the deliverable. It must render and contain the
    headline table."""

    def test_markdown_report(self):
        report = evaluate.build_report(GT)
        md = evaluate.render_markdown(GT, report)
        self.assertIn("| # | Metric | Scope | Precision | Recall | F1 |", md)
        self.assertIn("Clause segmentation", md)
        self.assertIn("Known gaps", md)
        self.assertIn("Method, and the honest limits", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
