"""Change impact analysis: what breaks, what it costs, and what we are unsure of.

Owner: Person B (branch b-graph).

This module answers the one question the project exists for:

    "If Clause 7.2 changes from 30 days to 15 days, what else is affected,
     what does that actually mean in days, and where are we NOT sure?"

Three parts:
    1. IMPACT        - which clauses are hit, and how directly (graph traversal)
    2. COUNTERFACTUAL - recompute the downstream arithmetic old vs new
    3. DOUBTS        - explicit, evidence-linked uncertainties (never guesses)

Every item carries evidence (a clause id) so the reasoning layer on branch C
can cite it.
"""

import adapter
import clauses as clause_tools
import graph


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _clause_index(contract_id):
    rows = adapter.list_clauses(contract_id)
    index = {}
    for row in rows:
        meta = adapter.clause_metadata(row)
        index[row["section_number"]] = {
            "sec_no": row["section_number"],
            "heading": meta.get("heading", ""),
            "type": meta.get("type") or clause_tools.classify_type(
                row["clause_text"], meta.get("heading", "")),
            "text": row["clause_text"] or "",
            "id": row["clause_id"],
            "level": meta.get("level", 1),
        }
    return index


def _days_in(clause):
    return [n["value"] for n in clause_tools.find_numbers(clause.get("text", ""))]


def _pct_in(clause):
    return [n["value"] for n in clause_tools.find_numbers(clause.get("text", ""))
            if n["unit"] == "%"]


def _payment_days(index):
    """The operative payment period: the payment clause with a 'within N days'
    deadline, preferring one headed 'Payment Period'."""
    best = None
    for sec, clause in index.items():
        if clause.get("type") != "payment":
            continue
        within = clause_tools.find_within_days(clause.get("text", ""))
        if within is None:
            continue
        heading = (clause.get("heading") or "").lower()
        if "payment period" in heading:
            return within
        if best is None:
            best = within
    return best


def _fmt_num(value):
    """Format a number without truncating decimals (1.5 must not print as 1)."""
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def _evidence(sec_no, index, why):
    clause = index.get(sec_no, {})
    return {
        "kind": "clause",
        "id": sec_no,
        "type": clause.get("type", "other"),
        "why": why,
        "text": (clause.get("text", "") or "")[:400],
    }


# Which node kinds mean what in the impact report.
# The MVP spec asks for four things the lawyer reads separately:
#   obligations  - what the parties must DO
#   conditions   - what must be true / what triggers
#   consequences - what happens if it goes wrong
#   deadlines    - when it is due
_KIND_BUCKETS = {
    "obligation": "obligations",
    "condition": "conditions",
    "termination": "consequences",
    "deadline": "deadlines",
    "payment": "obligations",
    "right": "rights",
    "definition": "definitions",
}


def affected_semantics(contract_id, impacted_secs):
    """Group the impacted clauses' semantic nodes by what they MEAN.

    This is the difference between "clause 8.1 is affected" (useless) and
    "the Customer's obligation to pay late charges at 1.5% per month is
    affected" (useful to a lawyer).

    Returns {obligations: [...], conditions: [...], consequences: [...],
             deadlines: [...], rights: [...], definitions: [...]}
    """
    wanted = set(impacted_secs)
    out = {"obligations": [], "conditions": [], "consequences": [],
           "deadlines": [], "rights": [], "definitions": []}

    for node in adapter.get_nodes(contract_id):
        ref = node.get("clause_reference") or ""
        if "::" not in ref:
            continue                      # this is the clause node itself
        sec_no, kind = ref.split("::", 1)
        if sec_no not in wanted:
            continue
        bucket = _KIND_BUCKETS.get(node.get("node_type"))
        if not bucket:
            continue
        out[bucket].append({
            "clause": sec_no,
            "kind": node.get("node_type"),
            "detail": node.get("detail", ""),
        })

    for bucket in out:
        out[bucket].sort(key=lambda x: (x["clause"], x["detail"]))
    return out


# ---------------------------------------------------------------------------
# 1. IMPACT
# ---------------------------------------------------------------------------
def impact_of(contract_id, changed_sec, max_hops=3):
    """Direct and indirect impact of changing one clause."""
    edges = adapter.get_edges(contract_id)
    hits = graph.find_impact(changed_sec, edges, max_hops=max_hops)
    direct = [h for h in hits if h["hops"] == 1]
    indirect = [h for h in hits if h["hops"] >= 2]
    return {
        "direct": direct,
        "indirect": indirect,
        "chain": graph.chain_text(changed_sec, edges, max_hops=max_hops),
        "all": hits,
    }


# ---------------------------------------------------------------------------
# 2. COUNTERFACTUAL
# ---------------------------------------------------------------------------
def counterfactual(contract_id, changed_sec, old_value, new_value):
    """Recompute the downstream numbers with the new value.

    For a payment-period change this yields:
        payment deadline          30 days  ->  15 days
        default can arise on      day 45   ->  day 30
        interest accrual window   30 days  ->  15 days
        cure period               15 days  ->  15 days  (UNCHANGED, flagged)
    """
    index = _clause_index(contract_id)
    changed = index.get(changed_sec, {})
    rows = []
    notes = []

    if changed.get("type") != "payment":
        notes.append(
            "The day-count recalculation below is implemented for payment-period "
            "changes only. Clause %s is a '%s' clause, so the downstream arithmetic "
            "for this change type has not been modelled."
            % (changed_sec, changed.get("type")))

    cure_days = None
    cure_clause = None
    notice_days = None
    notice_clause = None
    for sec, clause in index.items():
        if clause.get("type") == "cure":
            days = _days_in(clause)
            if days:
                cure_days, cure_clause = days[0], sec
        if clause.get("type") == "notice":
            days = _days_in(clause)
            if days:
                notice_days, notice_clause = days[0], sec

    rows.append({
        "metric": "Payment deadline (Clause %s)" % changed_sec,
        "old": "%s days" % old_value,
        "new": "%s days" % new_value,
        "changed": True,
    })

    if cure_days is not None:
        rows.append({
            "metric": "Default can be declared on (payment + cure)",
            "old": "day %d" % (old_value + cure_days),
            "new": "day %d" % (new_value + cure_days),
            "changed": True,
            "detail": "cure period %d days (Clause %s)" % (cure_days, cure_clause),
        })
    else:
        notes.append("No cure period clause was identified, so the default trigger "
                     "date could not be recomputed.")

    has_interest = any(c.get("type") == "interest" for c in index.values())
    if has_interest:
        rows.append({
            "metric": "Interest accrual window",
            "old": "%s days" % old_value,
            "new": "%s days" % new_value,
            "changed": True,
        })

    if cure_days is not None:
        rows.append({
            "metric": "Cure period (Clause %s)" % cure_clause,
            "old": "%d days" % cure_days,
            "new": "%d days" % cure_days,
            "changed": False,
            "detail": "not changed by this amendment - confirm this is intended",
        })

    if notice_days is not None:
        rows.append({
            "metric": "Notice required before default (Clause %s)" % notice_clause,
            "old": "%d days" % notice_days,
            "new": "%d days" % notice_days,
            "changed": False,
            "detail": "notice window is unchanged but the time to serve it is shorter",
        })

    return {
        "rows": rows,
        "notes": notes,
        "old_value": old_value,
        "new_value": new_value,
    }


# ---------------------------------------------------------------------------
# 3. DOUBTS  (uncertainty-aware reasoning - Deep feature 5)
# ---------------------------------------------------------------------------
def detect_doubts(contract_id, changed_sec, old_value, new_value):
    """Rules that produce explicit doubts instead of confident guesses.

    Each doubt: {code, severity, question, evidence:[...], review_point}
    """
    index = _clause_index(contract_id)
    edges = adapter.get_edges(contract_id)
    changed = index.get(changed_sec, {})
    changed_text = changed.get("text", "")
    changed_type = changed.get("type", "other")
    hits = impact_of(contract_id, changed_sec)
    impacted = [h["sec_no"] for h in hits["all"]]

    doubts = []

    # -- D1 definition collision -------------------------------------------
    ref_edges = [e for e in edges
                 if e["src"] == changed_sec and e["rel"] == "references"
                 and index.get(e["dst"], {}).get("type") == "definition"]
    for e in ref_edges:
        def_clause = index[e["dst"]]
        def_days = _days_in(def_clause)
        doubts.append({
            "code": "definition_collision",
            "severity": "high",
            "question": (
                "Clause %s changes the payment period to %s days, but Definition %s "
                "separately defines the term and states %s. The system cannot determine "
                "from the text alone whether amending %s alone also moves Definition %s."
                % (changed_sec, new_value, e["dst"],
                   ("%d days" % def_days[0]) if def_days else "a separate period",
                   changed_sec, e["dst"])),
            "evidence": [
                _evidence(changed_sec, index, "the clause being changed"),
                _evidence(e["dst"], index, "the definition it depends on"),
            ],
            "review_point": "Confirm the interaction between Clause %s and Definition %s."
                            % (changed_sec, e["dst"]),
        })
        if def_days and def_days[0] == old_value and def_days[0] != new_value:
            doubts.append({
                "code": "cross_ref_mismatch",
                "severity": "high",
                "question": (
                    "Clause %s now says %s days but Definition %s still says %d days. "
                    "On one reading the contract is self-contradictory."
                    % (changed_sec, new_value, e["dst"], def_days[0])),
                "evidence": [
                    _evidence(changed_sec, index, "new value"),
                    _evidence(e["dst"], index, "old value still stated"),
                ],
                "review_point": "Decide whether Definition %s is amended at the same time."
                                % e["dst"],
            })

    # -- D2 unchanged dependent parameters ---------------------------------
    for sec in impacted:
        clause = index.get(sec)
        if not clause:
            continue
        nums = [n for n in clause_tools.find_numbers(clause["text"])
                if n["value"] not in (old_value, new_value)]
        if not nums:
            continue
        first = nums[0]
        doubts.append({
            "code": "unchanged_dependent_param",
            "severity": "medium",
            "question": (
                "Clause %s is affected by this change but still states %s %s. "
                "The system cannot infer whether that figure should move as well."
                % (sec, _fmt_num(first["value"]), first["unit"])),
            "evidence": [_evidence(sec, index, "figure left unchanged")],
            "review_point": "Confirm whether the %s %s in Clause %s should change."
                            % (_fmt_num(first["value"]), first["unit"], sec),
        })
        break  # one representative example is enough; more just adds noise

    # -- D3 notice vs the shortened window ----------------------------------
    notice_secs = [s for s, c in index.items() if c.get("type") == "notice"]
    term_secs = [s for s in impacted if index.get(s, {}).get("type") == "termination"]
    if notice_secs and term_secs:
        n_days = _days_in(index[notice_secs[0]])
        if n_days:
            doubts.append({
                "code": "notice_vs_cure",
                "severity": "medium",
                "question": (
                    "Termination for non-payment (Clause %s) requires notice under Clause %s, "
                    "which is deemed served only %d days after despatch. The shortened payment "
                    "period reduces the practical window for serving that notice."
                    % (term_secs[0], notice_secs[0], n_days[0])),
                "evidence": [
                    _evidence(term_secs[0], index, "termination right in the impact chain"),
                    _evidence(notice_secs[0], index, "notice mechanics"),
                ],
                "review_point": "Check the notice can be served in time under Clause %s."
                                % notice_secs[0],
            })

    # -- D4 undefined capitalised term -------------------------------------
    defined_here = set()
    defined_elsewhere = set()
    for s, c in index.items():
        for term in clause_tools.defined_terms_by_clause(c["text"]):
            if s == changed_sec:
                defined_here.add(term.lower())
            else:
                defined_elsewhere.add(term.lower())
    for term in clause_tools.find_quoted_terms(changed_text):
        low = term.lower()
        if low in defined_here or low in defined_elsewhere:
            continue
        doubts.append({
            "code": "undefined_term",
            "severity": "low",
            "question": ('The term "%s" is used in Clause %s but no definition clause '
                         "in this contract defines it." % (term, changed_sec)),
            "evidence": [_evidence(changed_sec, index, "term used without definition")],
            "review_point": 'Locate the definition of "%s".' % term,
        })
        break

    # -- D5 formalities of amendment ---------------------------------------
    for s, c in index.items():
        low = c["text"].lower()
        if "amendment" in low and "writing" in low and "signed" in low:
            doubts.append({
                "code": "amendment_form",
                "severity": "medium",
                "question": (
                    "Clause %s requires amendments to be in writing and signed. If the "
                    "proposed change is agreed by email or in a meeting, it may not take "
                    "effect." % s),
                "evidence": [_evidence(s, index, "formalities of amendment")],
                "review_point": "Check the proposed change is executed in the form "
                                "Clause %s requires." % s,
            })
            break

    # -- D6 what the change does not obviously touch ------------------------
    if changed_type == "payment":
        siblings = [s for s, c in index.items()
                    if c.get("type") == "payment" and s != changed_sec
                    and c.get("level", 1) == 1]
        if siblings:
            doubts.append({
                "code": "scope_of_change",
                "severity": "low",
                "question": (
                    "The system was asked about Clause %s only. The other payment "
                    "clauses (%s) were not recalculated and may also be affected."
                    % (changed_sec, ", ".join("Clause %s" % s for s in siblings[:3]))),
                "evidence": [_evidence(changed_sec, index, "scope of the question")],
                "review_point": "Confirm whether %s need conforming changes."
                                % ", ".join("Clause %s" % s for s in siblings[:3]),
            })

    return doubts


# ---------------------------------------------------------------------------
# 4. CONFLICTS inside one contract (thin feature: conflict detection)
# ---------------------------------------------------------------------------
def clash_check(contract_id):
    """Find a definition that no longer matches the clause that uses it.

    We only compare a definition with clauses that actually REFERENCE it.
    Comparing every clause with every definition produces nonsense (a 12 month
    renewal period is not a payment period).
    """
    index = _clause_index(contract_id)
    edges = adapter.get_edges(contract_id)

    referrers = {}
    for e in edges:
        if e["rel"] in ("references", "depends_on"):
            referrers.setdefault(e["dst"], []).append(e["src"])

    findings = []
    for dsec, dclause in index.items():
        if dclause.get("type") != "definition":
            continue
        d_days = _days_in(dclause)
        if not d_days:
            continue
        for psec in referrers.get(dsec, []):
            pclause = index.get(psec)
            if not pclause:
                continue
            p_days = _days_in(pclause)
            if not p_days:
                continue
            if p_days[0] != d_days[0]:
                findings.append({
                    "code": "definition_drift",
                    "definition_clause": dsec,
                    "using_clause": psec,
                    "definition_value": d_days[0],
                    "using_value": p_days[0],
                    "message": ("Definition %s states %d days but Clause %s, which uses "
                                "that definition, states %d days."
                                % (dsec, d_days[0], psec, p_days[0])),
                })
    return findings


# ---------------------------------------------------------------------------
# 5. CROSS-CONTRACT comparison (thin feature: cross-contract analysis)
# ---------------------------------------------------------------------------
def compare_contracts(contract_id_a, contract_id_b):
    """Compare two contracts on the numbers that matter."""
    def profile(cid):
        index = _clause_index(cid)
        pay_days = _payment_days(index)
        sla_pct = None
        cure_days = None
        for s, c in index.items():
            if c.get("type") == "sla" and sla_pct is None:
                p = _pct_in(c)
                sla_pct = p[0] if p else None
            if c.get("type") == "cure" and cure_days is None:
                d = _days_in(c)
                cure_days = d[0] if d else None
        return {"payment_days": pay_days, "sla_pct": sla_pct, "cure_days": cure_days}

    a, b = profile(contract_id_a), profile(contract_id_b)
    mismatches = []
    for key, label in (("payment_days", "Payment period (days)"),
                       ("sla_pct", "Service level availability (%)"),
                       ("cure_days", "Cure period (days)")):
        if a[key] is not None and b[key] is not None and a[key] != b[key]:
            mismatches.append({
                "metric": label,
                "contract_a": a[key],
                "contract_b": b[key],
                "message": "%s differs: Contract A = %s, Contract B = %s."
                           % (label, a[key], b[key]),
            })
    return {"contract_a": a, "contract_b": b, "mismatches": mismatches}


def portfolio_anomaly(contract_ids):
    """Which contract is the odd one out on payment terms? (thin feature 13)"""
    rows = []
    for cid in contract_ids:
        p = compare_contracts(cid, cid)["contract_a"]
        rows.append({"contract_id": cid, "payment_days": p["payment_days"],
                     "sla_pct": p["sla_pct"]})
    days = [r["payment_days"] for r in rows if r["payment_days"] is not None]
    if len(days) < 2:
        return {"rows": rows, "outliers": []}
    lo, hi = min(days), max(days)
    outliers = [r for r in rows if r["payment_days"] is not None
                and r["payment_days"] not in (lo, hi)]
    return {"rows": rows, "min": lo, "max": hi, "outliers": outliers}


# ---------------------------------------------------------------------------
# 6. THE MAIN ENTRY POINT
# ---------------------------------------------------------------------------
def analyze_change(contract_id, changed_sec, old_value, new_value):
    """Full impact report for one proposed change."""
    index = _clause_index(contract_id)
    changed = index.get(changed_sec)
    if not changed:
        return {"error": "Clause %s not found in this contract." % changed_sec}

    imp = impact_of(contract_id, changed_sec)
    cfact = counterfactual(contract_id, changed_sec, old_value, new_value)
    doubts = detect_doubts(contract_id, changed_sec, old_value, new_value)
    clashes = clash_check(contract_id)

    # what the impacted clauses MEAN, not just which numbers they are
    impacted_secs = [h["sec_no"] for h in imp["all"]]
    semantics = affected_semantics(contract_id, impacted_secs)
    semantics["of_changed_clause"] = affected_semantics(contract_id, [changed_sec])

    evidence = [_evidence(changed_sec, index, "the clause being changed")]
    for h in imp["all"]:
        evidence.append(_evidence(h["sec_no"], index,
                                  "impacted at hop %d" % h["hops"]))

    review_points = [d["review_point"] for d in doubts]

    return {
        "changed_clause": {
            "sec_no": changed_sec,
            "type": changed.get("type"),
            "heading": changed.get("heading"),
            "old_value": old_value,
            "new_value": new_value,
            "text": changed.get("text", "")[:600],
        },
        "direct": imp["direct"],
        "indirect": imp["indirect"],
        "chain": imp["chain"],
        "obligations": semantics["obligations"],
        "conditions": semantics["conditions"],
        "consequences": semantics["consequences"],
        "deadlines": semantics["deadlines"],
        "rights": semantics["rights"],
        "definitions": semantics["definitions"],
        "changed_clause_effects": semantics["of_changed_clause"],
        "counterfactual": cfact,
        "doubts": doubts,
        "conflicts": clashes,
        "evidence": evidence,
        "review_points": review_points,
        "summary": (
            "Changing Clause %s from %s to %s affects %d clause(s) directly and "
            "%d indirectly. It touches %d obligation(s) and %d condition(s). "
            "The system has %d open question(s) that a lawyer should "
            "resolve before this change is accepted."
            % (changed_sec, old_value, new_value, len(imp["direct"]),
               len(imp["indirect"]), len(semantics["obligations"]),
               len(semantics["conditions"]), len(doubts))
        ),
    }
