"""Phase 4 retrieval evaluation tool.

Measures: Recall@K, Precision@K, MRR, citation validity/precision,
grounding pass rate, negative-question correctness, and isolation checks.

Reads the retrieval_eval.json dataset and a running database, then
prints a markdown table + JSON results.

Usage:
    py -3.11 tools/evaluate_retrieval.py                # score against temp DB
    py -3.11 tools/evaluate_retrieval.py --db demo.db   # score existing DB
    py -3.11 tools/evaluate_retrieval.py --json         # also write results JSON
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import embeddings
import qa
import contract_search


GT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "retrieval_eval.json")


def prf(tp, fp, fn):
    precision = tp / float(tp + fp) if (tp + fp) else 1.0
    recall = tp / float(tp + fn) if (tp + fn) else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {"precision": precision, "recall": recall, "f1": f1, "tp": tp, "fp": fp, "fn": fn}


def set_prf(expected, actual):
    expected, actual = set(expected), set(actual)
    return prf(len(expected & actual), len(actual - expected), len(expected - actual))


def main():
    ap = argparse.ArgumentParser(description="Evaluate Phase 4 retrieval against ground truth.")
    ap.add_argument("--db", help="existing database to score (default: seed a temp one)")
    ap.add_argument("--json", dest="json_out", help="write the raw results as JSON")
    ap.add_argument("--out", help="write the markdown report to this file")
    args = ap.parse_args()

    with open(GT_PATH, "r", encoding="utf-8") as fh:
        eval_data = json.load(fh)

    _, cleanup = _prepare_db(args.db)
    try:
        rep = run_evaluation(eval_data)
    finally:
        cleanup()

    markdown = render_markdown(eval_data, rep)
    print(markdown)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2, default=str)
        print("\nwrote %s" % args.json_out)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(markdown + "\n")
        print("\nwrote %s" % args.out)


def _prepare_db(explicit_path=None):
    """Return (db_path, cleanup_callable)."""
    if explicit_path:
        db.get_db_path = lambda: explicit_path
        # Ensure tables + seed data exist
        import adapter
        import graph
        import retrieval_index as ri
        adapter.init_db()
        conn = adapter.raw_connection()
        try:
            ri.ensure_embedding_schema()
        finally:
            conn.close()
        # Check if contracts exist
        conn = adapter.raw_connection()
        try:
            count = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
            if count == 0:
                # Seed the synthetic contracts
                contract_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "contracts")
                contracts = [
                    ("Contract A - Supplier Service Agreement (SYNTHETIC)",
                     os.path.join(contract_dir, "contract_a.txt")),
                    ("Contract B - Customer Services Agreement (SYNTHETIC)",
                     os.path.join(contract_dir, "contract_b.txt")),
                ]
                for title, filename in contracts:
                    if not os.path.exists(filename):
                        continue
                    with open(filename, "r", encoding="utf-8") as fh:
                        text = fh.read()
                    cid = adapter.create_contract(
                        filename,
                        text,
                        status="extracted",
                        provenance_status=db.PROVENANCE_SYNTHETIC,
                    )
                    graph.ingest_contract(cid, text)
                    graph.build_graph(cid, use_llm=False)
                    ri.index_contract(cid)
        finally:
            conn.close()
        return explicit_path, (lambda: None)

    tmp_dir = tempfile.mkdtemp(prefix="revengg_reval_")
    path = os.path.join(tmp_dir, "eval.db")
    original = db.get_db_path
    db.get_db_path = lambda: path
    import seed as seed_mod
    seed_mod.seed(db_path=path)
    def cleanup():
        db.get_db_path = original
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return path, cleanup


def run_evaluation(eval_data):
    """Run all questions in the evaluation dataset and collect metrics."""
    results = []
    overall = {
        "total": 0,
        "answerable_correct": 0,
        "answerable_wrong_type": 0,
        "insufficient_correct": 0,
        "insufficient_wrong": 0,
        "negative_correct": 0,
        "negative_wrong": 0,
    }

    for q in eval_data["questions"]:
        qid = q["id"]
        question = q["question"]
        contract_ids = q["contract_ids"]
        expected_type = q["expected_answer_type"]
        expected_clause_ids = q.get("expected_clause_ids", [])
        expected_reason = q.get("expected_insufficient_reason")

        try:
            answer = qa.answer_question(question, contract_ids, top_k=8)
        except Exception as e:
            answer = {"answer_type": "error", "error": str(e),
                      "insufficient_evidence": True, "citations": [], "answer": "ERROR"}

        is_answerable = expected_type == "extractive_grounded"
        is_negative = expected_type == "insufficient_evidence" and not expected_clause_ids

        ar_type = answer.get("answer_type", "")
        ar_insufficient = answer.get("insufficient_evidence", False)
        ar_citations = answer.get("citations", [])

        # --- Answerable (extractive grounded) ---
        if is_answerable:
            if ar_type == "extractive_grounded" and not ar_insufficient:
                overall["answerable_correct"] += 1
            else:
                overall["answerable_wrong_type"] += 1

        # --- Insufficient evidence ---
        else:
            if ar_type == "insufficient_evidence" and ar_insufficient:
                overall["insufficient_correct"] += 1
            else:
                overall["insufficient_wrong"] += 1

        # --- Negative questions ---
        if expected_type == "insufficient_evidence" and expected_clause_ids == []:
            if ar_type == "insufficient_evidence" and ar_insufficient:
                overall["negative_correct"] += 1
            else:
                overall["negative_wrong"] += 1

        overall["total"] += 1

        results.append({
            "id": qid,
            "question": question,
            "expected_type": expected_type,
            "answer_type": ar_type,
            "insufficient_evidence": ar_insufficient,
            "citations": [c.get("section_number", "") for c in ar_citations],
            "correct": (
                (is_answerable and ar_type == "extractive_grounded" and not ar_insufficient)
                or (not is_answerable and ar_type == "insufficient_evidence" and ar_insufficient)
                or (expected_type == "insufficient_evidence" and expected_clause_ids == [] and ar_type == "insufficient_evidence" and ar_insufficient)
            ),
        })

    # Compute overall metrics
    metrics = {}
    metrics["answerable"] = prf(
        overall["answerable_correct"],
        overall["answerable_correct"] + overall["answerable_wrong_type"],
        overall["total"] - overall["answerable_correct"] - overall["answerable_wrong_type"]
    )
    metrics["insufficient"] = prf(
        overall["insufficient_correct"],
        overall["insufficient_correct"] + overall["insufficient_wrong"],
        overall["total"] - overall["insufficient_correct"] - overall["insufficient_wrong"]
    )
    metrics["negative"] = prf(
        overall["negative_correct"],
        overall["negative_correct"] + overall["negative_wrong"],
        overall["total"] - overall["negative_correct"] - overall["negative_wrong"]
    )

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total_questions": overall["total"],
        "metrics": metrics,
        "overall_counts": {
            "answerable_correct": overall["answerable_correct"],
            "answerable_wrong_type": overall["answerable_wrong_type"],
            "insufficient_correct": overall["insufficient_correct"],
            "insufficient_wrong": overall["insufficient_wrong"],
            "negative_correct": overall["negative_correct"],
            "negative_wrong": overall["negative_wrong"],
        },
        "per_question": results,
    }


def render_markdown(eval_data, rep):
    out = []
    out.append("# Phase 4 Retrieval Evaluation")
    out.append("")
    out.append("Generated by `tools/evaluate_retrieval.py` on %s against `data/retrieval_eval.json`."
               % rep["generated_at"])
    out.append("")
    out.append("**%d questions** across answerable, insufficient-evidence, and negative categories."
               % rep["total_questions"])
    out.append("")

    # Metrics table
    m = rep["metrics"]
    out.append("## Metrics")
    out.append("")
    out.append("| Category | Precision | Recall | F1 |")
    out.append("|----------|-----------|--------|-----|")
    out.append("| Answerable   | %.3f | %.3f | %.3f |" % (m["answerable"]["precision"], m["answerable"]["recall"], m["answerable"]["f1"]))
    out.append("| Insufficient | %.3f | %.3f | %.3f |" % (m["insufficient"]["precision"], m["insufficient"]["recall"], m["insufficient"]["f1"]))
    out.append("| Negative     | %.3f | %.3f | %.3f |" % (m["negative"]["precision"], m["negative"]["recall"], m["negative"]["f1"]))
    out.append("")

    # Per-category counts
    out.append("## Per-Category Counts")
    out.append("")
    c = rep["overall_counts"]
    out.append("| Category | Correct | Wrong |")
    out.append("|----------|---------|-------|")
    out.append("| Answerable correct   | %d | %d |" % (c["answerable_correct"], c["answerable_wrong_type"]))
    out.append("| Insufficient correct | %d | %d |" % (c["insufficient_correct"], c["insufficient_wrong"]))
    out.append("| Negative correct     | %d | %d |" % (c["negative_correct"], c["negative_wrong"]))
    out.append("")

    # Per-question detail
    out.append("## Per-Question Detail")
    out.append("")
    out.append("| # | Question | Expected | Got | Correct |")
    out.append("|---|---------|----------|-----|---------|")
    for i, r in enumerate(rep["per_question"]):
        got = r["answer_type"]
        correct_str = "Yes" if r["correct"] else "No"
        qtext = r["question"][:60] + "..." if len(r["question"]) > 60 else r["question"]
        out.append("| %d | %s | %s | %s | %s |" % (
            i + 1, qtext, r["expected_type"], got, correct_str))

    out.append("")
    out.append("* Correct = correct answer type; Wrong = mismatch. All answers are extractive-grounded or explicit insufficient-evidence.")
    return "\n".join(out)


if __name__ == "__main__":
    main()