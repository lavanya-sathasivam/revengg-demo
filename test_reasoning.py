import unittest
from unittest import mock

import reasoning


class TestReasoning(unittest.TestCase):

    def test_empty_text(self):
        res = reasoning.explain_clause("")
        self.assertEqual(res["summary"], "No clause text provided.")
        self.assertEqual(res["relevant_authorities"], [])
        self.assertIn("This is not legal advice", res["disclaimer"])
        self.assertFalse(res["meta"]["retrieval_error"])

    def test_whitespace_only(self):
        res = reasoning.explain_clause("   \n\t  ")
        self.assertEqual(res["summary"], "No clause text provided.")
        self.assertEqual(res["relevant_authorities"], [])
        self.assertIn("This is not legal advice", res["disclaimer"])

    def test_no_matches(self):
        res = reasoning.explain_clause("arbitration clause", retrieved_authorities=[])
        self.assertEqual(res["relevant_authorities"], [])
        self.assertIn("No matching authorities", res["summary"])
        self.assertTrue(any("No authorities retrieved by keyword matching" in u for u in res["uncertainties_and_missing_information"]))
        self.assertFalse(res["meta"]["retrieval_error"])

    def test_key_point_reflects_current_matching(self):
        ra = [{"id": "k1", "matched_keywords": ["free consent"]}]
        res = reasoning.explain_clause("free consent", retrieved_authorities=ra)
        expected = (
            "Matching uses curated keywords and numeric section citations; "
            "summaries and topics are not scored."
        )
        self.assertIn(expected, res["key_points"])
        old = (
            "Keyword matching performed across keywords, summary, citation, "
            "and applicable topics."
        )
        self.assertNotIn(old, res["key_points"])

    def test_citation_only_match_why_matched(self):
        ra = [
            {
                "id": "c1",
                "matched_keywords": [],
                "relevance_score": 1,
                "applicable_topics": ["formation"],
                "query_tokens": ["10"],
            }
        ]
        res = reasoning.explain_clause("Section 10", retrieved_authorities=ra)
        auth = res["relevant_authorities"][0]
        self.assertEqual(
            auth["why_matched"], "Matched via numeric section citation."
        )
        self.assertNotIn("topic", auth["why_matched"].lower())
        self.assertNotIn("summary", auth["why_matched"].lower())
        # The summary must not claim a topic merely because it is listed.
        self.assertNotIn("formation", res["summary"])
        self.assertIn("numeric section citation", res["summary"])

    def test_keyword_match_does_not_cite_topics(self):
        ra = [
            {
                "id": "k1",
                "matched_keywords": ["free consent"],
                "relevance_score": 2,
                "applicable_topics": ["formation"],
            }
        ]
        res = reasoning.explain_clause("free consent", retrieved_authorities=ra)
        auth = res["relevant_authorities"][0]
        self.assertEqual(
            auth["why_matched"], "Matched curated keyword(s): free consent."
        )
        self.assertNotIn("topic", auth["why_matched"].lower())
        self.assertNotIn("formation", auth["why_matched"])
        self.assertNotIn("formation", res["summary"])
        self.assertIn("free consent", res["summary"])

    def test_no_authorities_summary_is_neutral(self):
        res = reasoning.explain_clause("unmatched text", retrieved_authorities=[])
        self.assertEqual(
            res["summary"],
            "No matching authorities found in the dataset for the provided text.",
        )
        self.assertNotIn("topic", res["summary"].lower())

    def test_trace_describes_why_matched_accurately(self):
        ra = [{"id": "t1", "matched_keywords": ["breach"]}]
        res = reasoning.explain_clause("breach", retrieved_authorities=ra)
        trace = " ".join(res["reasoning_trace"])
        self.assertIn("numeric section-citation description", trace)
        self.assertNotIn("matched_keywords/applicable_topics", trace)

    def test_multiple_authorities_preserved_order(self):
        ra = [
            {"id": "a1", "keywords": ["contract"], "matched_keywords": ["contract"], "relevance_score": 1, "query_tokens": ["contract"]},
            {"id": "a2", "keywords": ["contract"], "matched_keywords": ["contract"], "relevance_score": 1, "query_tokens": ["contract"]},
        ]
        res = reasoning.explain_clause("contract", retrieved_authorities=ra)
        self.assertEqual(res["relevant_authorities"][0]["id"], "a1")
        self.assertEqual(res["relevant_authorities"][1]["id"], "a2")
        self.assertEqual(res["meta"]["retrieved_count"], 2)

    def test_verification_flags(self):
        ra = [
            {"id": "v1", "verification_status": "confirmed", "matched_keywords": ["contract"]},
            {"id": "v2", "verification_status": "needs_verification", "matched_keywords": ["contract"]},
            {"id": "v3", "matched_keywords": ["contract"]},  # missing
        ]
        res = reasoning.explain_clause("contract", retrieved_authorities=ra)
        self.assertTrue(res["relevant_authorities"][0]["is_verified"])
        self.assertTrue(res["relevant_authorities"][0]["is_retrieved"])
        self.assertFalse(res["relevant_authorities"][1]["is_verified"])
        self.assertTrue(res["relevant_authorities"][1]["is_retrieved"])
        self.assertFalse(res["relevant_authorities"][2]["is_verified"])
        self.assertTrue(res["relevant_authorities"][2]["is_retrieved"])

    def test_source_details(self):
        ra = [{"id": "s1", "matched_keywords": ["x"], "source_name": "SN", "source_url": "http://x", "source_granularity": "act_pdf_only", "source_record_url": "http://r", "additional_source_name": "AS", "additional_source_url": "http://as", "verification_status": "needs_verification"}]
        res = reasoning.explain_clause("x", retrieved_authorities=ra, include_sources=True)
        sd = res["relevant_authorities"][0]["source_details"]
        self.assertIsNotNone(sd)
        self.assertEqual(sd["source_name"], "SN")
        res2 = reasoning.explain_clause("x", retrieved_authorities=ra, include_sources=False)
        self.assertIsNone(res2["relevant_authorities"][0]["source_details"])
        self.assertFalse(res2["meta"]["include_sources"])

    def test_missing_optional_fields(self):
        ra = [{"id": "m1"}]
        res = reasoning.explain_clause("test", retrieved_authorities=ra)
        self.assertEqual(res["meta"]["retrieved_count"], 1)

    def test_ignore_non_dict(self):
        ra = [{"id": "ok"}, "bad", 123, None]
        res = reasoning.explain_clause("x", retrieved_authorities=ra)
        self.assertEqual(res["meta"]["retrieved_count"], 1)
        self.assertEqual(res["relevant_authorities"][0]["id"], "ok")

    def test_input_non_mutation(self):
        orig = {"id": "t1", "keywords": ["breach"], "matched_keywords": ["breach"]}
        ra = [dict(orig)]
        res = reasoning.explain_clause("breach", retrieved_authorities=ra)
        self.assertEqual(ra[0], orig)
        res["relevant_authorities"][0]["id"] = "changed"
        self.assertEqual(ra[0]["id"], "t1")

    def test_matched_evidence_structure(self):
        ra = [{"id": "e1", "matched_keywords": ["contract", "agreement"]}]
        res = reasoning.explain_clause("contract agreement", retrieved_authorities=ra)
        self.assertTrue(len(res["matched_evidence"]) >= 1)
        ev = res["matched_evidence"][0]
        self.assertIn("authority_id", ev)
        self.assertEqual(ev["evidence_type"], "keyword_match")
        self.assertEqual(ev["matched_terms"], ["contract", "agreement"])
        self.assertIsNone(ev["field_matched"])

    def test_reasoning_trace_length(self):
        ra = [{"id": "r1", "matched_keywords": ["x"]}]
        res = reasoning.explain_clause("x", retrieved_authorities=ra)
        self.assertGreaterEqual(len(res["reasoning_trace"]), 8)

    def test_disclaimer_exact(self):
        res = reasoning.explain_clause("x", retrieved_authorities=[])
        self.assertEqual(res["disclaimer"], "This explanation is for informational/demo purposes only and does not constitute legal advice. This is not legal advice.")

    def test_long_input_preview(self):
        longtext = "a " * 120  # long
        res = reasoning.explain_clause(longtext)
        # empty input? "a a ..." becomes non-empty? "a " * 120 normalizes to many a's separated by space; non-empty -> may have authorities empty
        self.assertLessEqual(len(res["input"]["normalized_preview"]), 200)
        self.assertEqual(res["input"]["text"], longtext)

    def test_explain_with_retrieval_mock(self):
        with mock.patch("reasoning.retrieval.search_authorities") as m:
            m.return_value = [{"id": "m1", "matched_keywords": ["contract"], "relevance_score": 1}]
            res = reasoning.explain_with_retrieval("contract", min_score=1, include_sources=True)
            m.assert_called_once()
            self.assertTrue(res["meta"]["used_internal_retrieval"])
            self.assertEqual(res["relevant_authorities"][0]["id"], "m1")

    def test_retrieval_exception(self):
        with mock.patch("reasoning.retrieval.search_authorities") as m:
            m.side_effect = Exception("boom")
            res = reasoning.explain_with_retrieval("contract")
            self.assertTrue(res["meta"]["retrieval_error"])
            self.assertEqual(res["relevant_authorities"], [])

    def test_min_score_passed(self):
        with mock.patch("reasoning.retrieval.search_authorities") as m:
            m.return_value = []
            reasoning.explain_with_retrieval("x", min_score=2)
            m.assert_called_once()
            args, kwargs = m.call_args
            # check min_score passed
            self.assertIn("min_score", kwargs or {})

if __name__ == "__main__":
    unittest.main(verbosity=2)
