"""Tests for retrieval.py.

Follows the style of existing tests (direct function calls, isolated inputs).
"""

import json
import os
import tempfile
import unittest

import retrieval


# Actual stored text for contract 1, section 1 ("DUTIES") in app.db. It contains
# the word "consent" twice, which previously matched the multiword keyword
# "free consent" on Section 10.
CLAUSE_1_TEXT = """1. DUTIES.
A. CONSULTANT agrees to exercise special skill to accomplish the following results in a manner
reasonably satisfactory to COMMISSION: ______________________________, as specified in Exhibit
A: Scope of Services, which by this reference is incorporated herein.

B. CONSULTANT shall provide the personnel listed below to perform the above-specified services, which
persons are hereby designated as key personnel under this Agreement.

Name


Firm



Function










Principal in Charge










Project Manager




C. No person named in paragraph B of this Section, or his or her successor, shall be removed or replaced by
CONSULTANT, nor shall his or her agreed-upon function hereunder be changed, without the prior
written consent of COMMISSION.  Such consent shall not be unreasonably withheld.

D. CONSULTANT’S PROGRESS REPORTS AND/OR MEETINGS
1) The CONSULTANT shall submit written progress reports with each invoice. The report should be
sufficiently detailed for the Contract Manager to determine if the CONSULTANT is performing to
expectations or is on schedule; to provide communication of interim findings; and to sufficiently
address any difficulties or special problems encountered, so remedies can be developed.
2) The CONSULTANT’s Project Manager shall meet with the COMMISSION’s Contract Manager, as
needed, to discuss progress on the contract.

"""


class RetrievalLoadTests(unittest.TestCase):
    def test_load_authorities_default_path(self):
        # Should load from data/authorities.json relative to retrieval.py
        auths = retrieval.load_authorities()
        self.assertIsInstance(auths, list)
        self.assertEqual(len(auths), 5)
        ids = [a["id"] for a in auths]
        self.assertIn("ica_1872_s2h", ids)
        self.assertIn("ica_1872_s10", ids)
        self.assertIn("ica_1872_s56", ids)
        self.assertIn("ica_1872_s73", ids)
        self.assertIn("satyabrata_v_mugneeram_1954", ids)

    def test_load_authorities_missing_file(self):
        auths = retrieval.load_authorities("/nonexistent/path/authorities.json")
        self.assertEqual(auths, [])

    def test_load_authorities_invalid_json(self):
        fd, path = tempfile.mkstemp(suffix=".json", prefix="bad_auth_")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("{ not valid json ]")
            auths = retrieval.load_authorities(path)
            self.assertEqual(auths, [])
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_load_authorities_invalid_top_level(self):
        fd, path = tempfile.mkstemp(suffix=".json", prefix="bad_top_")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump([1, 2, 3], f)
            auths = retrieval.load_authorities(path)
            self.assertEqual(auths, [])
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_load_authorities_missing_authorities_key(self):
        fd, path = tempfile.mkstemp(suffix=".json", prefix="bad_key_")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"schema_version": "1.0"}, f)
            auths = retrieval.load_authorities(path)
            self.assertEqual(auths, [])
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


class RetrievalNormalizeTests(unittest.TestCase):
    def test_normalize_text_basic(self):
        self.assertEqual(retrieval.normalize_text("Hello   World"), "hello world")
        self.assertEqual(retrieval.normalize_text("  TEST  "), "test")
        self.assertEqual(retrieval.normalize_text(""), "")
        self.assertEqual(retrieval.normalize_text(None), "")

    def test_normalize_text_newlines_tabs(self):
        self.assertEqual(retrieval.normalize_text("a\n\nb\tc"), "a b c")


class RetrievalSearchTests(unittest.TestCase):
    def setUp(self):
        # Use default authorities
        self.auths = retrieval.load_authorities()

    def test_empty_query(self):
        self.assertEqual(retrieval.search_authorities(""), [])
        self.assertEqual(retrieval.search_authorities("   "), [])
        self.assertEqual(retrieval.search_authorities("\t\n"), [])

    def test_case_insensitive_matching(self):
        results = retrieval.search_authorities("BREACH")
        self.assertTrue(len(results) > 0)
        ids = [r["id"] for r in results]
        self.assertIn("ica_1872_s73", ids)

    def test_repeated_whitespace(self):
        r1 = retrieval.search_authorities("breach   damages")
        r2 = retrieval.search_authorities("breach damages")
        self.assertEqual([r["id"] for r in r1], [r["id"] for r in r2])

    def test_citation_matching_section_2h(self):
        results = retrieval.search_authorities("Section 2(h)")
        self.assertEqual([r["id"] for r in results], ["ica_1872_s2h"])

    def test_citation_matching_section_10(self):
        results = retrieval.search_authorities("Section 10")
        self.assertEqual([r["id"] for r in results], ["ica_1872_s10"])

    def test_citation_matching_section_73(self):
        results = retrieval.search_authorities("Section 73")
        self.assertEqual([r["id"] for r in results], ["ica_1872_s73"])

    def test_citation_matching_section_56(self):
        results = retrieval.search_authorities("Section 56")
        ids = [r["id"] for r in results]
        self.assertIn("ica_1872_s56", ids)
        # The numeric token "56" matches the citation.
        s56 = [r for r in results if r["id"] == "ica_1872_s56"][0]
        self.assertEqual(s56["relevance_score"], 1)

    def test_keyword_matching_frustration(self):
        results = retrieval.search_authorities("frustration")
        self.assertTrue(len(results) > 0)
        ids = [r["id"] for r in results]
        self.assertIn("ica_1872_s56", ids)
        self.assertIn("satyabrata_v_mugneeram_1954", ids)

    def test_topic_only_words_return_no_results(self):
        # "formation" and "definitions" appear in applicable_topics only, so
        # they are not evidence of relevance on their own.
        self.assertEqual(retrieval.search_authorities("formation"), [])
        self.assertEqual(retrieval.search_authorities("definitions"), [])

    def test_summary_only_word_returns_no_results(self):
        # "unlawful" appears in a summary but in no curated keyword.
        self.assertEqual(retrieval.search_authorities("unlawful"), [])

    def test_non_numeric_citation_word_returns_no_results(self):
        # A bare "section" is not a reference to a particular provision.
        self.assertEqual(retrieval.search_authorities("section"), [])

    def test_multiword_keyword_requires_full_phrase(self):
        # "consent" alone must not match the curated keyword "free consent".
        self.assertEqual(retrieval.search_authorities("consent"), [])
        results = retrieval.search_authorities("free consent")
        self.assertEqual([r["id"] for r in results], ["ica_1872_s10"])
        self.assertIn("free consent", results[0]["matched_keywords"])

    def test_clause_without_keyword_evidence_returns_no_results(self):
        self.assertEqual(retrieval.search_authorities(CLAUSE_1_TEXT), [])

    def test_summary_and_topics_do_not_score(self):
        custom = [
            {
                "id": "test1",
                "keywords": ["indemnity"],
                "summary": "breach compensation frustration formation",
                "citation": "Section 99, Example Act, 1872",
                "applicable_topics": ["breach", "formation", "definitions"],
            },
        ]
        for query in ["breach", "compensation", "frustration", "formation",
                      "definitions"]:
            self.assertEqual(
                retrieval.search_authorities(query, authorities=custom), [], query
            )
        # The curated keyword still works.
        results = retrieval.search_authorities("indemnity", authorities=custom)
        self.assertEqual([r["id"] for r in results], ["test1"])

    def test_numeric_citation_token_matches_custom_authority(self):
        custom = [
            {
                "id": "test1",
                "keywords": ["indemnity"],
                "citation": "Section 56, Example Act, 1872",
            },
        ]
        results = retrieval.search_authorities("Section 56", authorities=custom)
        self.assertEqual([r["id"] for r in results], ["test1"])
        self.assertEqual(results[0]["relevance_score"], 1)
        # Citation matches do not fabricate keyword evidence.
        self.assertEqual(results[0]["matched_keywords"], [])
        # A non-numeric citation word on its own must not match.
        self.assertEqual(
            retrieval.search_authorities("example", authorities=custom), []
        )

    def test_keyword_matches_report_keyword_evidence(self):
        for query in ["breach", "frustration", "lawful enforceable"]:
            results = retrieval.search_authorities(query)
            self.assertTrue(results, query)
            for result in results:
                self.assertTrue(result["matched_keywords"], (query, result["id"]))

    def test_scoring_with_meaningful_terms(self):
        results = retrieval.search_authorities("breach compensation")
        # Only s73 covers both terms; generic words are not needed to score.
        self.assertEqual([r["id"] for r in results], ["ica_1872_s73"])
        self.assertEqual(results[0]["relevance_score"], 2)
        self.assertIn("breach", results[0]["matched_keywords"])
        self.assertIn("compensation", results[0]["matched_keywords"])

    def test_generic_only_queries_return_no_results(self):
        # "contract" and "agreement" describe nearly every contract, so on
        # their own they must not retrieve the whole dataset.
        for query in ["contract", "agreement", "CONTRACT", "Agreement",
                      "contract agreement", "agreement contract"]:
            self.assertEqual(retrieval.search_authorities(query), [], query)

    def test_meaningful_query_still_works_with_generic_word(self):
        # A generic word may accompany a meaningful term; it must not add noise.
        results = retrieval.search_authorities("contract breach")
        self.assertEqual([r["id"] for r in results], ["ica_1872_s73"])

        results = retrieval.search_authorities("agreement enforceable")
        self.assertEqual(
            [r["id"] for r in results], ["ica_1872_s2h", "ica_1872_s10"]
        )

    def test_stopwords_do_not_inflate_scores(self):
        plain = retrieval.search_authorities("lawful enforceable")
        padded = retrieval.search_authorities(
            "this contract is lawful and of the enforceable"
        )
        self.assertEqual(
            [(r["id"], r["relevance_score"]) for r in plain],
            [(r["id"], r["relevance_score"]) for r in padded],
        )
        # "lawful" + "enforceable" = 2, no point from "this", "is" or "the".
        self.assertEqual(plain[0]["id"], "ica_1872_s10")
        self.assertEqual(plain[0]["relevance_score"], 2)

    def test_stopword_only_query_returns_no_results(self):
        self.assertEqual(
            retrieval.search_authorities("the of and to this is that"), []
        )

    def test_stable_tie_order_frustration(self):
        results = retrieval.search_authorities("frustration")
        # Both s56 and satyabrata score 1; original order is s56 then satyabrata
        ids = [r["id"] for r in results]
        self.assertEqual(ids[0], "ica_1872_s56")
        self.assertEqual(ids[1], "satyabrata_v_mugneeram_1954")

    def test_no_matches(self):
        results = retrieval.search_authorities("arbitration")
        self.assertEqual(results, [])

    def test_custom_authority_lists(self):
        custom = [
            {"id": "test1", "keywords": ["breach"], "summary": "test"},
            {"id": "test2", "keywords": ["contract"], "summary": "test"},
        ]
        results = retrieval.search_authorities("breach", authorities=custom)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "test1")

    def test_minimum_score(self):
        # Two meaningful tokens produce score 2 for s73; min_score=3 filters it.
        results = retrieval.search_authorities("breach compensation", min_score=3)
        self.assertEqual(results, [])
        # With min_score=1 the same query returns the matching authority.
        results2 = retrieval.search_authorities("breach compensation", min_score=1)
        self.assertEqual([r["id"] for r in results2], ["ica_1872_s73"])
        # Generic-only queries stay empty regardless of min_score.
        self.assertEqual(retrieval.search_authorities("contract", min_score=1), [])

    def test_matched_keywords(self):
        results = retrieval.search_authorities("contract enforceable")
        s2h = [r for r in results if r["id"] == "ica_1872_s2h"][0]
        self.assertIn("matched_keywords", s2h)
        self.assertIsInstance(s2h["matched_keywords"], list)
        self.assertTrue(len(s2h["matched_keywords"]) > 0)
        # Should include contract and/or enforceable
        mk = [k.lower() for k in s2h["matched_keywords"]]
        self.assertTrue("contract" in mk or any("enforceable" in k for k in mk))

    def test_preservation_of_original_fields(self):
        results = retrieval.search_authorities("frustration")
        s56 = [r for r in results if r["id"] == "ica_1872_s56"][0]
        # Check original fields preserved
        self.assertEqual(s56["title"], "The Indian Contract Act, 1872")
        self.assertEqual(s56["authority_type"], "statute_section")
        self.assertEqual(s56["citation"], "Section 56, Indian Contract Act, 1872")
        self.assertEqual(s56["jurisdiction"], "India")
        self.assertEqual(s56["verification_status"], "needs_verification")
        self.assertIn("source_url", s56)
        self.assertIn("source_granularity", s56)
        self.assertIn("keywords", s56)
        self.assertIn("summary", s56)
        self.assertIn("applicable_topics", s56)
        # Check added fields
        self.assertIn("relevance_score", s56)
        self.assertIn("matched_keywords", s56)
        self.assertIn("query_tokens", s56)

    def test_no_mutation_of_supplied_records(self):
        custom = [
            {"id": "test1", "keywords": ["breach"], "summary": "test breach"},
        ]
        orig = dict(custom[0])
        results = retrieval.search_authorities("breach", authorities=custom)
        self.assertEqual(len(results), 1)
        # Original record should not be mutated
        self.assertEqual(custom[0], orig)
        # Result should have added fields
        self.assertIn("relevance_score", results[0])

    def test_missing_optional_fields_handled(self):
        custom = [
            {"id": "test1", "keywords": ["indemnity"]},  # no summary, citation, topics
        ]
        results = retrieval.search_authorities("indemnity", authorities=custom)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["relevance_score"], 1)

    def test_query_tokens_in_result(self):
        results = retrieval.search_authorities("breach damages")
        s73 = [r for r in results if r["id"] == "ica_1872_s73"][0]
        self.assertEqual(s73["query_tokens"], ["breach", "damages"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
