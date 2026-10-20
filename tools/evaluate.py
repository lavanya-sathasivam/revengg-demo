"""Evaluation harness: score the engine against data/ground_truth.json.

Owner: Person B (branch b-graph).

WHY THIS FILE EXISTS
    "The demo works" is not evidence. "The system finds 58 of 59 clauses,
    gets 57 clause types right, and recalls 6 of 6 required doubts" IS
    evidence - it is the difference between a working demo and a measurable
    system, and it is the first thing an examiner and a patent agent ask for.

    This script scores the DETERMINISTIC parts of the engine (segmentation,
    clause typing, numbers, dependency edges, impact, counterfactual,
    doubts, conflicts, cross-contract comparison) against the hand-annotated
    truth set. It measures the rule table, not the LLM.

    It reads the same code path the demo uses - there is no separate
    "evaluation mode" that could drift away from production behaviour.

USAGE
    py -3.11 tools/evaluate.py                # score everything, print tables
    py -3.11 tools/evaluate.py --json         # also write results/evaluation.json
    py -3.11 tools/evaluate.py --out FILE.md  # write the markdown report to a file
    py -3.11 tools/evaluate.py --db demo.db   # score an existing database

    By default it seeds a THROWAWAY database in a temp folder, so it never
    touches demo.db and never touches Person A's app.db.

WHAT IT DOES NOT DO
    It does not score the LLM (the LLM only fills gaps the rule table
    leaves, and the core demo needs no API key), and it does not score the
    legal retrieval or the reasoning narrative - those are Person C's.

HOW TO READ THE OUTPUT
    Precision = of the things the system claims, how many are right.
    Recall    = of the things that matter, how many the system found.
    F1        = the harmonic mean; the headline number for the report.
    Every number below is computed, never asserted by hand.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import db          # noqa: E402  (Person A's module - only to redirect the db path)
import adapter     # noqa: E402
import clauses     # noqa: E402
import impact      # noqa: E402

GT_PATH = os.path.join(ROOT, "data", "ground_truth.json")

CONTRACT_A = 1
CONTRACT_B = 2


# ---------------------------------------------------------------------------
# metric primitives
# ---------------------------------------------------------------------------
def prf(tp, fp, fn):
    """Precision / recall / F1 from counts. 0/0 counts score 1.0 (nothing to
    find and nothing found is a perfect result, not a failure)."""
    precision = tp / float(tp + fp) if (tp + fp) else 1.0
    recall = tp / float(tp + fn) if (tp + fn) else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "tp": tp,
        "fp": fp,
        "fn": fn,
    }


def set_prf(expected, actual):
    """Set-based precision / recall / F1."""
    expected, actual = set(expected), set(actual)
    return prf(len(expected & actual), len(actual - expected), len(expected - actual))


def fmt_pct(value):
    return "%.3f" % value


def fmt_metric(name, scope, m, notes=""):
    return "| %s | %s | %s | %s | %s | %d/%d/%d | %s |" % (
        name, scope, fmt_pct(m["precision"]), fmt_pct(m["recall"]), fmt_pct(m["f1"]),
        m["tp"], m["fp"], m["fn"], notes)


# ---------------------------------------------------------------------------
# database setup
# ---------------------------------------------------------------------------
def prepare_db(explicit_path=None):
    """Return (db_path, cleanup_callable).

    With no explicit path we seed a throwaway database so the evaluation is
    reproducible on a fresh clone and never disturbs demo.db.
    """
    if explicit_path:
        return explicit_path, (lambda: None)

    tmp_dir = tempfile.mkdtemp(prefix="revengg_eval_")
    path = os.path.join(tmp_dir, "eval.db")
    original = db.get_db_path
    db.get_db_path = lambda: path
    db.init_db()

    import seed
    seed.seed(db_path=path)

    def cleanup():
        db.get_db_path = original
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return path, cleanup


# ---------------------------------------------------------------------------
# collectors: what the system actually produced
# ---------------------------------------------------------------------------
def actual_clause_numbers(contract_id):
    return [r["section_number"] for r in adapter.list_clauses(contract_id)]


def actual_clause_types(contract_id):
    out = {}
    for row in adapter.list_clauses(contract_id):
        meta = adapter.clause_metadata(row)
        out[row["section_number"]] = meta.get("type") or clauses.classify_type(
            row["clause_text"], meta.get("heading", ""))
    return out


def actual_clause_edges(contract_id, rel):
    """Clause-level edges of one relation type, as (src, dst) pairs.

    Element nodes are referenced as '7.2::payment'; clause-level metrics only
    look at plain clause numbers.
    """
    out = set()
    for e in adapter.get_edges(contract_id):
        if e["rel"] != rel:
            continue
        if "::" in e["src"] or "::" in e["dst"]:
            continue
        out.add((e["src"], e["dst"]))
    return out


def actual_numbers(contract_id):
    """(value, unit) pairs found in each clause."""
    out = {}
    for row in adapter.list_clauses(contract_id):
        pairs = clauses.find_numbers(row["clause_text"] or "")
        out[row["section_number"]] = {(n["value"], n["unit"]) for n in pairs}
    return out


# ---------------------------------------------------------------------------
# the metrics themselves
# ---------------------------------------------------------------------------
def evaluate_segmentation(gt, contract_id, key):
    """Clause segmentation: did we find exactly the right clauses?"""
    expected = gt[key]["expected_clause_numbers"]
    actual = actual_clause_numbers(contract_id)
    m = set_prf(expected, actual)
    count_ok = len(actual) == gt[key]["expected_clause_count"]
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    return {
        "metric": m,
        "count_expected": gt[key]["expected_clause_count"],
        "count_actual": len(actual),
        "count_ok": count_ok,
        "missing": missing,
        "extra": extra,
    }


def evaluate_types(gt, contract_id, key):
    """Clause type classification: micro + per class."""
    expected = gt[key]["expected_types_full"]
    actual = actual_clause_types(contract_id)
    classes = sorted(set(expected.values()) | set(actual.values()))

    tp = fp = fn = 0
    per_class = {}
    for c in classes:
        e = {s for s, t in expected.items() if t == c}
        a = {s for s, t in actual.items() if t == c}
        hit = len(e & a)
        tp += hit
        fp += len(a - e)
        fn += len(e - a)
        if e or a:
            per_class[c] = prf(hit, len(a - e), len(e - a))

    macro = sum(per_class[c]["f1"] for c in per_class) / len(per_class)
    errors = []
    for sec in sorted(set(expected) | set(actual)):
        want, got = expected.get(sec), actual.get(sec)
        if want != got:
            errors.append({"clause": sec, "expected": want, "actual": got})
    return {
        "metric": prf(tp, fp, fn),
        "per_class": per_class,
        "macro_f1": macro,
        "annotated": len(expected),
        "errors": errors,
    }


def evaluate_numbers(gt, contract_id, key):
    """Number extraction, scored on the hand-annotated clauses only.

    We only score clauses that a human annotated. Scoring the whole contract
    would mix in the rupee-amount gap (see expected_amounts_not_extracted),
    which is a different and separately reported limitation.
    """
    expected_sets = gt[key].get("expected_number_sets", {})
    actual = actual_numbers(contract_id)

    tp = fp = fn = 0
    errors = []
    for sec, expected_list in sorted(expected_sets.items()):
        exp = {(n["value"], n["unit"]) for n in expected_list}
        act = actual.get(sec, set())
        tp += len(exp & act)
        fp += len(act - exp)
        fn += len(exp - act)
        if act != exp:
            errors.append({
                "clause": sec,
                "expected": sorted("%s %s" % (v, u) for v, u in exp),
                "actual": sorted("%s %s" % (v, u) for v, u in act),
            })
    return {
        "metric": prf(tp, fp, fn),
        "annotated_clauses": len(expected_sets),
        "errors": errors,
    }


def evaluate_edges(gt, contract_id, key, rel, gt_key):
    """Dependency or reference edges: (dependent, dependency) pairs."""
    if gt_key not in gt.get(key, {}):
        return None
    expected = [tuple(pair) for pair in gt[key][gt_key]]
    actual = actual_clause_edges(contract_id, rel)
    m = set_prf(expected, actual)
    return {
        "metric": m,
        "missing": sorted(set(expected) - actual),
        "extra": sorted(actual - set(expected)),
    }


def evaluate_impact(gt, contract_id, key):
    """Impact sets and causal chains for every annotated change."""
    results = []
    for sec, expected in sorted(gt[key]["expected_impact"].items()):
        imp = impact.impact_of(contract_id, sec)
        direct = [h["sec_no"] for h in imp["direct"]]
        indirect = [h["sec_no"] for h in imp["indirect"]]
        d = set_prf(expected["direct"], direct)
        i = set_prf(expected["indirect"], indirect)
        chain_ok = imp["chain"] == expected["chain"]
        results.append({
            "clause": sec,
            "direct": d,
            "indirect": i,
            "chain_ok": chain_ok,
            "chain_actual": imp["chain"],
            "chain_expected": expected["chain"],
            "direct_order_ok": direct == expected["direct"],
            "direct_missing": sorted(set(expected["direct"]) - set(direct)),
            "direct_extra": sorted(set(direct) - set(expected["direct"])),
            "indirect_missing": sorted(set(expected["indirect"]) - set(indirect)),
            "indirect_extra": sorted(set(indirect) - set(expected["indirect"])),
        })
    return results


def evaluate_counterfactual(gt, contract_id, key):
    """The day arithmetic: the numbers a lawyer would actually rely on."""
    results = []
    for sec, expected in sorted(gt[key].get("expected_counterfactual", {}).items()):
        cf = impact.counterfactual(contract_id, sec, expected["old"], expected["new"])
        rows = {r["metric"]: r for r in cf["rows"]}
        default_row = rows.get("Default can be declared on (payment + cure)")
        cure_row = [r for r in cf["rows"] if r["metric"].startswith("Cure period")]
        notice_row = [r for r in cf["rows"] if r["metric"].startswith("Notice")]
        checks = []
        checks.append({
            "check": "default trigger old",
            "expected": expected["default_trigger_old"],
            "actual": (default_row or {}).get("old"),
            "ok": bool(default_row) and default_row["old"] == expected["default_trigger_old"],
        })
        checks.append({
            "check": "default trigger new",
            "expected": expected["default_trigger_new"],
            "actual": (default_row or {}).get("new"),
            "ok": bool(default_row) and default_row["new"] == expected["default_trigger_new"],
        })
        cure_ok = (bool(cure_row)
                   and cure_row[0]["changed"] is False
                   and cure_row[0]["old"] == "%d days" % expected["cure_days_unchanged"])
        checks.append({
            "check": "cure period unchanged",
            "expected": "%d days, unchanged" % expected["cure_days_unchanged"],
            "actual": (cure_row[0]["old"] if cure_row else None),
            "ok": cure_ok,
        })
        notice_ok = (bool(notice_row)
                     and notice_row[0]["changed"] is False
                     and notice_row[0]["old"] == "%d days" % expected["notice_days_unchanged"])
        checks.append({
            "check": "notice period unchanged",
            "expected": "%d days, unchanged" % expected["notice_days_unchanged"],
            "actual": (notice_row[0]["old"] if notice_row else None),
            "ok": notice_ok,
        })
        results.append({"clause": sec, "checks": checks,
                        "all_ok": all(c["ok"] for c in checks)})
    return results


def evaluate_counterfactual_sla(gt, contract_id, key):
    """The service-credit exposure rows for a service-level change.

    Scored row by row, because the rows are what a lawyer reads.
    """
    results = []
    for sec, expected in sorted(gt[key].get("expected_counterfactual_sla", {}).items()):
        cf = impact.counterfactual(contract_id, sec, expected["old"], expected["new"])
        actual = {r["metric"]: r for r in cf["rows"]}
        checks = []
        for want in expected["rows"]:
            got = actual.get(want["metric"])
            ok = bool(got) and got["old"] == want["old"] \
                and got["new"] == want["new"] and got["changed"] == want["changed"]
            checks.append({
                "check": want["metric"],
                "expected": "%s -> %s%s" % (want["old"], want["new"],
                                           "" if want["changed"] else " (unchanged)"),
                "actual": ("%s -> %s%s" % (got["old"], got["new"],
                                           "" if got["changed"] else " (unchanged)")
                           if got else "row missing"),
                "ok": ok,
            })
        results.append({"clause": sec, "checks": checks,
                        "all_ok": all(c["ok"] for c in checks),
                        "notes": cf["notes"]})
    return results


def evaluate_doubts(gt, contract_id, key):
    """Doubt detection: are the required uncertainties raised?"""
    results = []
    expected_codes = set(gt[key].get("expected_doubt_codes", []))
    change = gt[key].get("expected_counterfactual", {}).get("7.2") or {}
    old = change.get("old", 30)
    new = change.get("new", 15)
    doubts = impact.detect_doubts(contract_id, "7.2", old, new)
    codes = {d["code"] for d in doubts}
    m = set_prf(expected_codes, codes) if expected_codes else None

    evidence_complete = True
    for d in doubts:
        if not d.get("evidence"):
            evidence_complete = False
            break
        for ev in d["evidence"]:
            if not ev.get("id"):
                evidence_complete = False
                break

    # the same check for a service-level change
    sla_expected = set(gt[key].get("expected_doubt_codes_sla", []))
    sla_result = None
    if sla_expected:
        sla_change = gt[key].get("expected_counterfactual_sla", {})
        for sec, exp in sorted(sla_change.items()):
            sla_doubts = impact.detect_doubts(contract_id, sec, exp["old"], exp["new"])
            sla_codes = {d["code"] for d in sla_doubts}
            sla_result = {
                "clause": sec,
                "metric": set_prf(sla_expected, sla_codes),
                "codes": sorted(sla_codes),
                "expected": sorted(sla_expected),
                "missing": sorted(sla_expected - sla_codes),
                "extra": sorted(sla_codes - sla_expected),
            }
            break

    return {
        "metric": m,
        "codes": sorted(codes),
        "expected": sorted(expected_codes),
        "missing": sorted(expected_codes - codes),
        "extra": sorted(codes - expected_codes),
        "evidence_complete": evidence_complete,
        "count": len(doubts),
        "sla": sla_result,
    }


def evaluate_conflicts(gt, contract_id, key):
    """Definition drift inside one contract."""
    expected = gt[key].get("expected_definition_drift")
    clashes = impact.clash_check(contract_id)
    actual = {(c["definition_clause"], c["using_clause"]) for c in clashes}
    if expected:
        exp = {(expected["definition_clause"], expected["using_clause"])}
    else:
        exp = set()
    m = set_prf(exp, actual)
    values_ok = True
    if expected:
        for c in clashes:
            if (c["definition_clause"] == expected["definition_clause"]
                    and c["using_clause"] == expected["using_clause"]):
                values_ok = (c["definition_value"] == expected["definition_value"]
                             and c["using_value"] == expected["using_value"])
    return {
        "metric": m,
        "values_ok": values_ok,
        "actual": sorted("%s -> %s" % (a, b) for a, b in actual),
    }


def evaluate_cross_contract(gt):
    """Cross-contract comparison: are the right metrics flagged?"""
    expected = gt["cross_contract"]
    result = impact.compare_contracts(CONTRACT_A, CONTRACT_B)
    actual_metrics = {m["metric"] for m in result["mismatches"]}
    m = set_prf(expected["expected_mismatch_metrics"], actual_metrics)

    values_ok = (
        result["contract_a"]["payment_days"] == expected["expected_payment_days"]["A"]
        and result["contract_b"]["payment_days"] == expected["expected_payment_days"]["B"]
        and result["contract_a"]["sla_pct"] == expected["expected_sla_pct"]["A"]
        and result["contract_b"]["sla_pct"] == expected["expected_sla_pct"]["B"]
    )
    return {
        "metric": m,
        "values_ok": values_ok,
        "missing": sorted(set(expected["expected_mismatch_metrics"]) - actual_metrics),
        "extra": sorted(actual_metrics - set(expected["expected_mismatch_metrics"])),
        "profile_a": result["contract_a"],
        "profile_b": result["contract_b"],
    }


def evaluate_node_grounding(contract_id):
    """Every number an element node states must occur in its own clause.

    An element node is rendered as a sentence in the lawyer-facing output,
    so a number in it that is not in the clause is a false statement. This
    check catches the class of bug where a template is attached to a clause
    it does not describe.
    """
    index = {r["section_number"]: (r["clause_text"] or "")
             for r in adapter.list_clauses(contract_id)}
    tp = fp = 0
    errors = []
    for node in adapter.get_nodes(contract_id):
        ref = node.get("clause_reference") or ""
        if "::" not in ref:
            continue                       # this is the clause node itself
        sec = ref.split("::", 1)[0]
        clause_nums = {(n["value"], n["unit"])
                       for n in clauses.find_numbers(index.get(sec, ""))}
        detail_nums = {(n["value"], n["unit"])
                       for n in clauses.find_numbers(node.get("detail", "") or "")}
        for value, unit in sorted(detail_nums):
            if (value, unit) in clause_nums:
                tp += 1
            else:
                fp += 1
                errors.append({"clause": sec, "node": ref,
                               "number": "%s %s" % (value, unit)})
    return {"metric": prf(tp, fp, 0), "checked": tp + fp, "errors": errors}


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def build_report(gt):
    """Run every metric and return a dict (also the JSON payload)."""
    rep = {"generated_at": datetime.now().isoformat(timespec="seconds"),
           "contracts": {}}

    for cid, key, label in ((CONTRACT_A, "contract_a", "Contract A"),
                            (CONTRACT_B, "contract_b", "Contract B")):
        rep["contracts"][key] = {
            "label": label,
            "segmentation": evaluate_segmentation(gt, cid, key),
            "types": evaluate_types(gt, cid, key),
            "numbers": evaluate_numbers(gt, cid, key),
            "dependencies": evaluate_edges(gt, cid, key, "depends_on",
                                           "expected_dependencies"),
            "references": evaluate_edges(gt, cid, key, "references",
                                         "expected_references"),
            "impact": evaluate_impact(gt, cid, key),
            "counterfactual": evaluate_counterfactual(gt, cid, key),
            "counterfactual_sla": evaluate_counterfactual_sla(gt, cid, key),
            "doubts": evaluate_doubts(gt, cid, key),
            "conflicts": evaluate_conflicts(gt, cid, key),
            "node_grounding": evaluate_node_grounding(cid),
        }

    rep["cross_contract"] = evaluate_cross_contract(gt)
    return rep


def render_markdown(gt, rep):
    out = []
    a = rep["contracts"]["contract_a"]
    b = rep["contracts"]["contract_b"]

    out.append("# Evaluation results")
    out.append("")
    out.append("Generated by `tools/evaluate.py` on %s against "
               "`data/ground_truth.json`." % rep["generated_at"])
    out.append("")
    out.append("Ground truth was hand-annotated from the contract texts before "
               "the system was run against it, so these numbers measure the "
               "rule table, not the model's output. They are also a "
               "self-consistency measure: the two contracts are synthetic, so "
               "this is NOT a claim about accuracy on real contracts.")
    out.append("")
    out.append("## Method, and the honest limits of these numbers")
    out.append("")
    out.append("- The annotations in `data/ground_truth.json` were written by "
               "reading the two contract texts clause by clause, before "
               "running the system against them.")
    out.append("- The rule table and the annotations were written by the same "
               "team, on two synthetic contracts. A perfect score here means "
               "*internal consistency and rule coverage*, NOT validity on real "
               "contracts. Expect these numbers to fall on a real, messier "
               "document.")
    out.append("- What these numbers DO give the report: a regression guard, "
               "and a named list of the places where the engine is known to "
               "fail (below).")
    out.append("- These metrics cover the deterministic engine only. The LLM "
               "gap-filling path, the legal retrieval and the reasoning "
               "narrative are not scored here.")
    out.append("- For the record: the LLM path was exercised live on "
               "2026-10-20. Asked about Clause 3.3, whose type pair "
               "(other, other) is not in the relation table, the model "
               "returned a `depends_on` edge to Clause 17.1 plus a condition "
               "node - a real dependency the typed table could not produce, "
               "and legally the right one. Those edges are NOT in the "
               "numbers below: the structural engine is deterministic and "
               "the LLM only ever adds to it.")
    out.append("")
    out.append("### Defects this harness has already caught")
    out.append("")
    out.append("- 2026-10-20 - `notice_vs_cure` never fired: the doubt rule "
               "read the heading-only section anchor '14. NOTICES' instead of "
               "Clause 14.1, found no day count, and skipped the doubt "
               "silently. Fixed in `impact.py`.")
    out.append("- 2026-10-20 - 15 clause-type errors: keyword matching was "
               "substring-based, so 'term' matched 'termination' and the bare "
               "word 'payment' could not be used as a heading cue. Fixed in "
               "`clauses.py` (word-boundary matching + heading cues).")
    out.append("- 2026-10-20 - a 'fee'-typed clause that imposes no fee "
               "(Clause 8.3) created a 'pay late charges' obligation, putting "
               "a false statement about the contract into the impact report. "
               "Fixed in `graph.py`.")
    out.append("- 2026-10-20 - every payment-typed clause was given the same "
               "'pay the amount stated in each undisputed Invoice' node, which "
               "is false for Clause 7.1 (an invoicing duty) and for Clause "
               "7.3 (where the Customer may withhold the disputed part), and "
               "Clause 7.2 was given a 'notify the Supplier of a dispute' "
               "deadline because 'undisputed' contains 'dispute'. Fixed in "
               "`graph.py` and `clauses.py`.")
    out.append("")
    out.append("## Headline metrics")
    out.append("")
    out.append("| # | Metric | Scope | Precision | Recall | F1 | tp/fp/fn |")
    out.append("|---|--------|-------|-----------|--------|----|----------|")

    rows = []
    for key, label in (("contract_a", "A"), ("contract_b", "B")):
        c = rep["contracts"][key]
        rows.append(fmt_metric("1. Clause segmentation (number)", label,
                               c["segmentation"]["metric"]))
        rows.append(fmt_metric("2. Clause type classification", label,
                               c["types"]["metric"]))
        rows.append(fmt_metric("3. Number extraction (annotated clauses)", label,
                               c["numbers"]["metric"]))
        if c["dependencies"] is not None:
            rows.append(fmt_metric("4. Dependency edges (depends_on)", label,
                                   c["dependencies"]["metric"]))
        if c["references"] is not None:
            rows.append(fmt_metric("5. Reference edges (references)", label,
                                   c["references"]["metric"]))
        if c["doubts"]["metric"] is not None:
            rows.append(fmt_metric("7. Doubt detection (code set)", label,
                                   c["doubts"]["metric"]))
        rows.append(fmt_metric("8. Definition-drift detection", label,
                               c["conflicts"]["metric"]))
        rows.append(fmt_metric("10. Element-node number grounding", label,
                               c["node_grounding"]["metric"]))

    impact_direct = prf(
        sum(r["direct"]["tp"] for r in a["impact"] + b["impact"]),
        sum(r["direct"]["fp"] for r in a["impact"] + b["impact"]),
        sum(r["direct"]["fn"] for r in a["impact"] + b["impact"]))
    impact_indirect = prf(
        sum(r["indirect"]["tp"] for r in a["impact"] + b["impact"]),
        sum(r["indirect"]["fp"] for r in a["impact"] + b["impact"]),
        sum(r["indirect"]["fn"] for r in a["impact"] + b["impact"]))
    rows.append(fmt_metric("6a. Impact - direct clauses", "A + B", impact_direct))
    rows.append(fmt_metric("6b. Impact - indirect clauses", "A + B", impact_indirect))
    rows.append(fmt_metric("9. Cross-contract mismatch detection", "A vs B",
                           rep["cross_contract"]["metric"]))
    rows.append(fmt_metric("10. Element-node number grounding", "A + B", prf(
        sum(c["node_grounding"]["metric"]["tp"]
            for c in (a, b)),
        sum(c["node_grounding"]["metric"]["fp"]
            for c in (a, b)),
        0)))

    out.extend(rows)
    out.append("")

    out.append("## Counts and coverage")
    out.append("")
    out.append("| Quantity | Contract A | Contract B |")
    out.append("|----------|-----------|-----------|")
    out.append("| Clauses expected | %d | %d |" % (
        a["segmentation"]["count_expected"], b["segmentation"]["count_expected"]))
    out.append("| Clauses extracted | %d | %d |" % (
        a["segmentation"]["count_actual"], b["segmentation"]["count_actual"]))
    out.append("| Clauses with a hand-written type label | %d | %d |" % (
        a["types"]["annotated"], b["types"]["annotated"]))
    out.append("| Clauses with a hand-written number label | %d | %d |" % (
        a["numbers"]["annotated_clauses"], b["numbers"]["annotated_clauses"]))
    out.append("| Macro F1 over clause types | %s | %s |" % (
        fmt_pct(a["types"]["macro_f1"]), fmt_pct(b["types"]["macro_f1"])))
    out.append("")

    out.append("## Clause type, per class")
    out.append("")
    out.append("| Clause type | A precision | A recall | A F1 | B precision | B recall | B F1 |")
    out.append("|-------------|-------------|----------|-------|-------------|----------|-------|")
    for c in sorted(set(a["types"]["per_class"]) | set(b["types"]["per_class"])):
        ma = a["types"]["per_class"].get(c)
        mb = b["types"]["per_class"].get(c)

        def cell(m):
            if not m:
                return "- | - | -"
            return "%s | %s | %s" % (fmt_pct(m["precision"]), fmt_pct(m["recall"]),
                                     fmt_pct(m["f1"]))
        out.append("| %s | %s | %s |" % (c, cell(ma), cell(mb)))
    out.append("")

    out.append("## Impact, per change")
    out.append("")
    out.append("| Contract | Clause | Direct P | Direct R | Direct F1 | Indirect F1 | Chain exact |")
    out.append("|----------|--------|----------|----------|-----------|--------------|-------------|")
    for key, label in (("contract_a", "A"), ("contract_b", "B")):
        for r in rep["contracts"][key]["impact"]:
            out.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                label, r["clause"],
                fmt_pct(r["direct"]["precision"]), fmt_pct(r["direct"]["recall"]),
                fmt_pct(r["direct"]["f1"]), fmt_pct(r["indirect"]["f1"]),
                "yes" if r["chain_ok"] else "NO"))
    out.append("")

    out.append("## Counterfactual arithmetic")
    out.append("")
    out.append("| Contract | Clause | Check | Expected | Actual | OK |")
    out.append("|----------|--------|-------|----------|--------|----|")
    for key, label in (("contract_a", "A"), ("contract_b", "B")):
        for r in rep["contracts"][key]["counterfactual"]:
            for c in r["checks"]:
                out.append("| %s | %s | %s | %s | %s | %s |" % (
                    label, r["clause"], c["check"], c["expected"],
                    c["actual"], "yes" if c["ok"] else "NO"))
        for r in rep["contracts"][key]["counterfactual_sla"]:
            for c in r["checks"]:
                out.append("| %s | %s | %s | %s | %s | %s |" % (
                    label, r["clause"], c["check"], c["expected"],
                    c["actual"], "yes" if c["ok"] else "NO"))
    out.append("")

    out.append("## Doubts raised")
    out.append("")
    for key, label in (("contract_a", "A"), ("contract_b", "B")):
        d = rep["contracts"][key]["doubts"]
        if d["expected"]:
            out.append("- **Contract %s, payment change**: %d doubt(s), codes %s, "
                       "expected %s%s" % (
                           label, d["count"],
                           ", ".join("`%s`" % c for c in d["codes"]) or "none",
                           ", ".join("`%s`" % c for c in d["expected"]) or "none",
                           ", every doubt carries evidence" if d["evidence_complete"]
                           else ", **a doubt is missing evidence**"))
        if d.get("sla"):
            s = d["sla"]
            out.append("- **Contract %s, service-level change (Clause %s)**: codes %s, "
                       "expected %s%s" % (
                           label, s["clause"],
                           ", ".join("`%s`" % c for c in s["codes"]) or "none",
                           ", ".join("`%s`" % c for c in s["expected"]) or "none",
                           "" if s["metric"]["f1"] == 1.0 else " - **MISMATCH**"))
    out.append("")

    out.append("## Errors found")
    out.append("")
    any_error = False
    for key, label in (("contract_a", "A"), ("contract_b", "B")):
        c = rep["contracts"][key]
        if c["segmentation"]["missing"] or c["segmentation"]["extra"]:
            any_error = True
            out.append("- **%s segmentation**: missing %s, unexpected %s" % (
                label, c["segmentation"]["missing"] or "none",
                c["segmentation"]["extra"] or "none"))
        if c["types"]["errors"]:
            any_error = True
            out.append("- **%s clause type**: %s" % (
                label, "; ".join("%s expected %s, got %s"
                                 % (e["clause"], e["expected"], e["actual"])
                                 for e in c["types"]["errors"])))
        if c["numbers"]["errors"]:
            any_error = True
            out.append("- **%s numbers**: %s" % (
                label, "; ".join("%s expected [%s], got [%s]"
                                 % (e["clause"], ", ".join(e["expected"]),
                                    ", ".join(e["actual"]))
                                 for e in c["numbers"]["errors"])))
        if c["dependencies"] and (c["dependencies"]["missing"]
                                  or c["dependencies"]["extra"]):
            any_error = True
            out.append("- **%s dependencies**: missing %s, unexpected %s" % (
                label, c["dependencies"]["missing"] or "none",
                c["dependencies"]["extra"] or "none"))
        if c["references"] and (c["references"]["missing"]
                                 or c["references"]["extra"]):
            any_error = True
            out.append("- **%s references**: missing %s, unexpected %s" % (
                label, c["references"]["missing"] or "none",
                c["references"]["extra"] or "none"))
        for r in c["impact"]:
            if r["direct_missing"] or r["direct_extra"] or r["indirect_missing"] \
                    or r["indirect_extra"] or not r["chain_ok"]:
                any_error = True
                out.append("- **%s impact of %s**: direct missing %s, extra %s; "
                           "indirect missing %s, extra %s; chain %s" % (
                               label, r["clause"], r["direct_missing"] or "none",
                               r["direct_extra"] or "none",
                               r["indirect_missing"] or "none",
                               r["indirect_extra"] or "none",
                               "ok" if r["chain_ok"] else "expected %s, got %s"
                               % (r["chain_expected"], r["chain_actual"])))
        if c["doubts"]["missing"] or c["doubts"]["extra"]:
            any_error = True
            out.append("- **%s doubts**: missing %s, unexpected %s" % (
                label, c["doubts"]["missing"] or "none",
                c["doubts"]["extra"] or "none"))
        if c["node_grounding"]["errors"]:
            any_error = True
            out.append("- **%s node grounding**: %s" % (
                label, "; ".join("%s states %s, which is not in the clause"
                                 % (e["node"], e["number"])
                                 for e in c["node_grounding"]["errors"])))
    xc = rep["cross_contract"]
    if xc["missing"] or xc["extra"]:
        any_error = True
        out.append("- **cross-contract**: missing %s, unexpected %s" % (
            xc["missing"] or "none", xc["extra"] or "none"))
    if not any_error:
        out.append("- none - every annotated item was reproduced exactly.")
    out.append("")

    out.append("## Known gaps (measured, not assumed)")
    out.append("")
    out.append("- Rupee amounts are parsed only where the service-credit "
               "counterfactual needs them (`clauses.find_amounts`). The general "
               "number parser still understands only days / months / years / %, so "
               "a rupee figure is not surfaced as a clause number anywhere else.")
    out.append("- The inverse dependency rule (where the REFERENCED clause is the "
               "dependent) currently covers one type pair, sla -> fee. It exists "
               "because the relation table otherwise assumes the referring clause "
               "is always the dependent.")
    out.append("- Clauses typed 'other' create no node, so a duty stated "
               "outside the typed families (for example Clause 3.1, 'provide "
               "the Services with reasonable skill and care') is not surfaced "
               "as an obligation.")
    out.append("- The counterfactual is implemented for payment-period changes "
               "and service-level changes. Any other change type says so instead "
               "of inventing rows.")
    out.append("- The doubt engine is a rule engine with a fixed set of doubt "
               "codes, not a general defeasible-reasoning system.")
    out.append("")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="Score the engine against the ground truth.")
    ap.add_argument("--db", help="existing database to score (default: seed a temp one)")
    ap.add_argument("--out", help="write the markdown report to this file")
    ap.add_argument("--json", dest="json_out", help="write the raw results as JSON")
    args = ap.parse_args()

    with open(GT_PATH, "r", encoding="utf-8") as fh:
        gt = json.load(fh)

    _, cleanup = prepare_db(args.db)
    try:
        report = build_report(gt)
    finally:
        cleanup()

    markdown = render_markdown(gt, report)
    print(markdown)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(markdown + "\n")
        print("\nwrote %s" % args.out)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
        print("wrote %s" % args.json_out)


if __name__ == "__main__":
    main()
