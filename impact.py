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

import hashlib
import json
import math
from decimal import Decimal

import adapter
import clauses as clause_tools
import graph

# The rule catalogue is part of the output contract.  A report names the exact
# rules that produced it, and the fingerprint lets a later reader prove that
# the same engine version produced the same calculation.
IMPACT_RULES_VERSION = "phase7.1"

ANALYSIS_STATUS_SUPPORTED = "supported"
ANALYSIS_STATUS_PARTIAL = "partially_supported"
ANALYSIS_STATUS_UNSUPPORTED = "unsupported"

_RULE_CATALOG = {
    "payment.deadline": "payment deadline = proposed new payment period",
    "payment.default_trigger": "default trigger = payment period + cure period",
    "payment.cure_unchanged": "cure period is read from the stored clause and is unchanged",
    "payment.notice_unchanged": "notice period is read from the stored clause and is unchanged",
    "sla.availability": "service level availability = proposed new percentage",
    "sla.threshold_as_drafted": "credit threshold is read from the stored schedule",
    "sla.threshold_conformed": "conformed credit threshold = proposed new percentage",
    "sla.credit_rate": "credit rate is read from the stored schedule",
    "sla.first_credit": "first credit = monthly charges * credit rate / 100",
    "sla.max_credit": "maximum credit = monthly charges * cap / 100",
    "sla.charges_base": "monthly charges base is read from the stored contract",
    "conflict.definition_drift": "a definition and a clause that references it state different day values",
    "doubt.definition_collision": "a changed clause references a definition that states its own value",
    "doubt.cross_ref_mismatch": "a changed value leaves a referenced definition stating the old value",
    "doubt.unchanged_dependent_param": "an impacted clause still states a figure not changed by the request",
    "doubt.notice_vs_cure": "a shortened payment period compresses the practical notice window",
    "doubt.undefined_term": "a quoted term used by the changed clause has no definition",
    "doubt.amendment_form": "the contract requires amendments to be written and signed",
    "doubt.scope_of_change": "other payment clauses were not recalculated",
    "doubt.sla_schedule_mismatch": "the SLA clause and the credit schedule state different thresholds",
    "evidence.clause": "evidence is a stored clause with its content hash",
}

_RULES_FINGERPRINT = hashlib.sha256(
    json.dumps(
        {"version": IMPACT_RULES_VERSION, "rules": _RULE_CATALOG},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()

_SLA_MIN = Decimal("0")
_SLA_MAX = Decimal("100")
_EVIDENCE_EXCERPT_LIMIT = 400


def rules_fingerprint():
    return _RULES_FINGERPRINT


def _analysis_status(binding):
    """Map the graph binding to the public three-state evidence taxonomy."""
    if binding and binding.get("legacy_unbound"):
        return ANALYSIS_STATUS_PARTIAL
    return ANALYSIS_STATUS_SUPPORTED


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
            "version_id": row.get("version_id"),
            "clause_sha256": row.get("clause_sha256"),
        }
    return index


def _error(code, message, http_status=400):
    """A machine-readable rejection that the HTTP layer can return safely."""
    return {"error": message, "error_code": code, "http_status": http_status}


def _calculation(rule_id, formula, inputs, assumptions):
    """A deterministic trace attached to one counterfactual row."""
    return {
        "rule_id": rule_id,
        "formula": formula,
        "inputs": inputs,
        "assumptions": list(assumptions),
    }


def _with_calculation(row, rule_id, formula, inputs, assumptions):
    row["rule_id"] = rule_id
    row["assumptions"] = list(assumptions)
    row["calculation"] = _calculation(
        rule_id, formula, inputs, assumptions
    )
    return row


def _excerpt(text, limit=_EVIDENCE_EXCERPT_LIMIT):
    """A visible excerpt; the caller always retains the complete ``text``."""
    text = text or ""
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return (
        "%s\n\n[EXPLICIT EXCERPT: %d of %d characters shown; %d omitted. "
        "The full clause text is in the 'text' field.]"
        % (text[:limit].rstrip(), limit, len(text), omitted)
    )


def _as_decimal(value):
    """An exact, finite Decimal for a supported numeric value, else None.

    Decimal(str(float)) is deliberate: Decimal(float) would preserve the
    binary representation error, which is exactly what this comparison is
    meant to avoid. Strings are not accepted here; HTTP/Pydantic owns that
    coercion and direct callers must pass a numeric type.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            return None
        number = Decimal(str(value))
    else:
        return None
    if not number.is_finite():
        return None
    return number


def _same_number(left, right):
    """Compare values as exact decimals, not binary floats."""
    left_number = _as_decimal(left)
    right_number = _as_decimal(right)
    if left_number is None or right_number is None:
        return False
    return left_number == right_number


def _stored_values(clause):
    """The authoritative values a stored clause states, never the request's.

    The API has no unit field, so the modelled change types carry an implicit
    unit: payment periods are days and service levels are percentage points.
    Only values in that unit are returned. A payment clause whose operative
    period is stated in months (or an SLA clause with no percentage) therefore
    yields no comparable value and is rejected as unsupported rather than
    having its number silently reinterpreted in the expected unit.
    """
    text = clause.get("text", "") or ""
    ctype = clause.get("type")
    values = []

    if ctype == "payment":
        within = clause_tools.find_within_days(text)
        if within is not None:
            values.append({"value": Decimal(within), "unit": "days"})
        elif not clause_tools.WITHIN_RE.search(text):
            # No 'within' period at all: day numbers elsewhere in the clause
            # are still usable. A 'within N months' period is deliberately
            # left empty so validation can reject it without inventing days.
            values = [{"value": n["value"], "unit": n["unit"]}
                      for n in clause_tools.find_numbers(text)
                      if n["unit"] == "days"]
    elif ctype == "sla":
        percentages = _pct_in(clause)
        if percentages:
            values.append({"value": percentages[0], "unit": "%"})
    else:
        # No arithmetic is modelled for other types, so there is no unit
        # convention to enforce; preserve the existing number check.
        values = [{"value": n["value"], "unit": n["unit"]}
                  for n in clause_tools.find_numbers(text)]

    unique = []
    seen = set()
    for item in values:
        key = (_as_decimal(item["value"]), item["unit"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _format_stored_values(values):
    return ", ".join(
        "%s%s" % (_fmt_num(v["value"]), v["unit"])
        if v["unit"] == "%"
        else "%s %s" % (_fmt_num(v["value"]), v["unit"])
        for v in values
    )


def _coerce_change_values(old_value, new_value):
    """Return exact Decimal values, or a rejection for invalid input."""
    old_number = _as_decimal(old_value)
    new_number = _as_decimal(new_value)
    if old_number is None or new_number is None:
        return None, None, _error(
            "invalid_change_value",
            "old_value and new_value must be finite numbers.",
        )
    if old_number < 0 or new_number < 0:
        return None, None, _error(
            "invalid_change_value",
            "old_value and new_value must be non-negative.",
        )
    return old_number, new_number, None


def _unsupported_unit(changed_sec, changed):
    """Reject a modelled value stated in a unit the API cannot express.

    No conversion is performed. In particular a 'within 2 months' payment
    period is not treated as 2 or 60 days, and a non-percentage SLA figure is
    not treated as a percentage.
    """
    expected = {"payment": "days", "sla": "%"}.get(changed.get("type"))
    if expected is None:
        return None
    observed = []
    for number in clause_tools.find_numbers(changed.get("text", "") or ""):
        unit = number["unit"]
        if unit != expected and unit not in observed:
            observed.append(unit)
    if not observed:
        return None
    return _error(
        "unsupported_unit",
        "Clause %s states values in %s, but this change type is modelled "
        "only in %s. No unit conversion is performed; submit the change in "
        "%s." % (changed_sec, ", ".join(observed), expected, expected),
    )


def _validate_change_values(changed_sec, changed, old_value, new_value):
    """Reject values that do not match the stored clause.

    old_value is the client's assertion about the current contract. It must be
    checked against the clause row read for THIS contract; otherwise a caller
    could make the report say Clause 7.2 currently reads 999 days and derive
    arithmetic from a value the contract never contained.

    A clause with no parseable value in its modelled unit has nothing to
    compare against. The existing 'no arithmetic modelled' behaviour is
    preserved for other clause types, but a payment/SLA value stated in a
    different unit is rejected rather than converted.
    """
    if old_value is None or new_value is None:
        return _error(
            "missing_change_value",
            "Both old_value and new_value are required.",
        )
    old_number, _new_number, value_error = _coerce_change_values(
        old_value, new_value
    )
    if value_error is not None:
        return value_error

    if changed.get("type") == "sla":
        for label, number in (
            ("old_value", old_number),
            ("new_value", _as_decimal(new_value)),
        ):
            if number is None:
                continue
            if number < _SLA_MIN or number > _SLA_MAX:
                return _error(
                    "sla_out_of_range",
                    "Service-level values must be between %s%% and %s%% "
                    "inclusive; %s is %s%%." % (
                        _fmt_num(_SLA_MIN), _fmt_num(_SLA_MAX),
                        label, _fmt_num(number),
                    ),
                )

    stored = _stored_values(changed)
    if not stored:
        if changed.get("type") not in ("payment", "sla"):
            # No arithmetic is modelled for this type. Preserve the existing
            # behaviour: the request is accepted and the counterfactual says
            # that no calculation was performed.
            return None
        ambiguity = clause_tools.within_period_error(
            changed.get("text", "") or ""
        )
        if ambiguity is not None:
            return _error("ambiguous_clause_value", ambiguity)
        unsupported = _unsupported_unit(changed_sec, changed)
        if unsupported is not None:
            return unsupported
        return _error(
            "unsupported_clause_value",
            "Clause %s does not state a value in the unit modelled for a "
            "'%s' change. No value was inferred; re-read the clause or "
            "request a different change."
            % (changed_sec, changed.get("type") or "unknown"),
        )
    if not any(_same_number(old_number, item["value"]) for item in stored):
        return _error(
            "stale_old_value",
            "old_value %s does not match the stored value for Clause %s "
            "(%s). Re-read the clause and submit its current value."
            % (_fmt_num(old_number), changed_sec,
               _format_stored_values(stored)),
        )
    return None


def _days_in(clause):
    """The single calendar-day value a clause states, or [].

    Percentages, money, months and years are never treated as day counts. A
    clause with competing 'within' periods or multiple distinct day values is
    ambiguous and yields [] so callers cannot silently pick one.
    """
    text = clause.get("text", "") or ""
    period = clause_tools.find_within_period(text)
    if period is not None:
        if not period.get("supported"):
            return []
        return [Decimal(period["days"])]

    values = [
        n["value"] for n in clause_tools.find_numbers(text)
        if n["unit"] == "days"
    ]
    unique = list(dict.fromkeys(values))
    if len(unique) != 1:
        return []
    return unique


def _pct_in(clause):
    return [n["value"] for n in clause_tools.find_numbers(
        clause.get("text", "")) if n["unit"] == "%"]


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
            return Decimal(within)
        if best is None:
            best = Decimal(within)
    return best


def _fmt_num(value):
    """Format a number without truncating decimals (1.5 must not print as 1)."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            return str(value)
        if value == value.to_integral_value():
            return str(int(value))
        return format(value.normalize(), "f")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _fmt_value(value, changed_type):
    """Format a value in the unit implied by the clause type."""
    if changed_type == "payment":
        return "%s days" % _fmt_num(value)
    if changed_type == "sla":
        return "%s%%" % _fmt_num(value)
    return _fmt_num(value)


def _fmt_numbers(numbers):
    """Format parsed number records without losing their unit."""
    parts = []
    for number in numbers:
        if number["unit"] == "%":
            parts.append("%s%%" % _fmt_num(number["value"]))
        else:
            parts.append("%s %s" % (
                _fmt_num(number["value"]), number["unit"]
            ))
    return ", ".join(parts)


def _evidence(sec_no, index, why, status=ANALYSIS_STATUS_SUPPORTED):
    clause = index.get(sec_no, {})
    text = clause.get("text", "") or ""
    return {
        "kind": "clause",
        "id": sec_no,
        "type": clause.get("type", "other"),
        "why": why,
        "text": text,
        "text_length": len(text),
        "excerpt": _excerpt(text),
        "excerpt_truncated": len(text) > _EVIDENCE_EXCERPT_LIMIT,
        "clause_version_id": clause.get("version_id"),
        "clause_sha256": clause.get("clause_sha256"),
        "status": status,
        "rule_id": "evidence.clause",
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
def _reference_section(reference):
    """'7.2::payment' -> '7.2'; '7.2' stays '7.2'."""
    return str(reference or "").split("::", 1)[0]


def _scoped_edges(contract_id, index):
    """Edges that connect only clauses belonging to this contract.

    adapter.get_edges() already enforces the contract on both node rows. This
    second check keeps impact traversal and evidence inside the verified clause
    set even if a node row exists for this contract but points at a section the
    contract does not actually store.
    """
    edges = []
    for edge in adapter.get_edges(contract_id):
        if (_reference_section(edge.get("src")) in index
                and _reference_section(edge.get("dst")) in index):
            edges.append(edge)
    return edges


def impact_of(contract_id, changed_sec, max_hops=3):
    """Direct and indirect impact of changing one clause."""
    index = _clause_index(contract_id)
    edges = _scoped_edges(contract_id, index)
    hits = graph.find_impact(changed_sec, edges, max_hops=max_hops)
    # Never report a graph-derived clause that is not stored in this contract.
    hits = [hit for hit in hits if hit.get("sec_no") in index]
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
def _fmt_inr(value):
    """Format INR with Indian grouping, preserving every exact decimal digit.

    No rounding is performed: a Decimal amount is formatted exactly. The old
    ``int(round(value))`` used Python's banker's rounding and silently changed
    the amount before the lawyer saw it.
    """
    number = _as_decimal(value)
    if number is None:
        return "INR %s" % value
    text = format(number, "f")
    sign = ""
    if text.startswith("-"):
        sign = "-"
        text = text[1:]
    if "." in text:
        whole, fraction = text.split(".", 1)
        fraction = fraction.rstrip("0")
    else:
        whole, fraction = text, ""
    if len(whole) <= 3:
        grouped = whole
    else:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        grouped = ",".join(groups + [tail])
    suffix = ".%s" % fraction if fraction else ""
    return "INR %s%s%s" % (sign, grouped, suffix)


def _label(sec):
    """'Clause 9.1' for a numbered clause, 'Schedule B' for a schedule."""
    text = str(sec)
    if text.upper().startswith("SCHEDULE"):
        return "Schedule %s" % text.split()[-1]
    return "Clause %s" % text


def _find_credit_schedule(index):
    """The schedule that states the service-credit mechanics, or None."""
    for sec, clause in index.items():
        parsed = clause_tools.parse_service_credit_schedule(clause.get("text", ""))
        if parsed:
            return sec, parsed
    return None, None


def _find_charges_base(index):
    """The monthly Charges the credits are calculated on, or None."""
    for sec, clause in index.items():
        text = clause.get("text", "") or ""
        amounts = clause_tools.find_amounts(text)
        if amounts and "charge" in text.lower():
            return sec, amounts[0]
    return None, None


def _payment_counterfactual(index, changed_sec, old_value, new_value):
    """The day arithmetic for a payment-period change.

    Interest is deliberately NOT calculated here. The accrual window in the
    corpus starts at the Payment Due Date, not at the end of the cure period,
    so deriving it from the payment period produced a false row. If an interest
    clause exists, a note names the omission instead of inventing a number.
    """
    rows = []
    notes = []
    old_number = _as_decimal(old_value)
    new_number = _as_decimal(new_value)

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

    rows.append(_with_calculation({
        "metric": "Payment deadline (Clause %s)" % changed_sec,
        "old": "%s days" % _fmt_num(old_number),
        "new": "%s days" % _fmt_num(new_number),
        "changed": True,
    }, "payment.deadline",
       "new payment deadline = submitted new_value",
       {"changed_clause": changed_sec,
        "old_days": _fmt_num(old_number),
        "new_days": _fmt_num(new_number)},
       ["old_value and new_value are calendar-day counts in the clause's "
        "implied unit; no conversion between days, months, years, or "
        "business days is performed."]))

    if cure_days is not None:
        rows.append(_with_calculation({
            "metric": "Default can be declared on (payment + cure)",
            "old": "day %s" % _fmt_num(old_number + cure_days),
            "new": "day %s" % _fmt_num(new_number + cure_days),
            "changed": True,
            "detail": "cure period %s days (Clause %s)"
                      % (_fmt_num(cure_days), cure_clause),
        }, "payment.default_trigger",
           "default trigger day = payment period + cure period",
           {"old_payment_days": _fmt_num(old_number),
            "new_payment_days": _fmt_num(new_number),
            "cure_days": _fmt_num(cure_days),
            "cure_clause": cure_clause},
           ["default may be declared only after the cure period expires.",
            "the cure period runs from receipt of written notice."]))
    else:
        notes.append("No cure period clause was identified, so the default trigger "
                     "date could not be recomputed.")

    if cure_days is not None:
        rows.append(_with_calculation({
            "metric": "Cure period (Clause %s)" % cure_clause,
            "old": "%s days" % _fmt_num(cure_days),
            "new": "%s days" % _fmt_num(cure_days),
            "changed": False,
            "detail": "not changed by this amendment - confirm this is intended",
        }, "payment.cure_unchanged",
           "cure period = stored cure clause value",
           {"cure_days": _fmt_num(cure_days), "cure_clause": cure_clause},
           ["the proposed amendment does not itself amend the cure clause."]))

    if notice_days is not None:
        rows.append(_with_calculation({
            "metric": "Notice required before default (Clause %s)" % notice_clause,
            "old": "%s days" % _fmt_num(notice_days),
            "new": "%s days" % _fmt_num(notice_days),
            "changed": False,
            "detail": "notice window is unchanged but the time to serve it is shorter",
        }, "payment.notice_unchanged",
           "notice period = stored notice clause value",
           {"notice_days": _fmt_num(notice_days),
            "notice_clause": notice_clause},
           ["the proposed amendment does not itself amend the notice clause."]))

    if any(c.get("type") == "interest" for c in index.values()):
        notes.append(
            "An interest clause exists, but interest accrual is not derived "
            "from the payment period. No interest row is calculated until the "
            "accrual start and end events are modelled explicitly."
        )

    return rows, notes


def _valid_pct(value):
    number = _as_decimal(value)
    return (
        number is not None
        and _SLA_MIN <= number <= _SLA_MAX
    )


def _sla_counterfactual(index, changed_sec, old_value, new_value):
    """The service-credit exposure for a service-level change.

    Lowering an SLA does not change what a credit is WORTH - it changes the
    band in which the Customer gets one at all. So the interesting numbers
    are: the credit threshold as drafted, the threshold if the schedule is
    conformed, the value of the first credit, and the cap.
    """
    rows = []
    notes = []
    old_number = _as_decimal(old_value)
    new_number = _as_decimal(new_value)

    rows.append(_with_calculation({
        "metric": "Service level availability (Clause %s)" % changed_sec,
        "old": "%s%%" % _fmt_num(old_number),
        "new": "%s%%" % _fmt_num(new_number),
        "changed": True,
    }, "sla.availability",
       "service level availability = submitted new_value",
       {"changed_clause": changed_sec,
        "old_pct": _fmt_num(old_number),
        "new_pct": _fmt_num(new_number)},
       ["values are percentage points of monthly availability; zero is a "
        "valid value and no day conversion is performed."]))

    schedule_sec, credit = _find_credit_schedule(index)
    charges_sec, charges = _find_charges_base(index)

    if not credit:
        notes.append("No service-credit schedule could be read from this contract, "
                     "so the credit exposure could not be recomputed.")
        return rows, notes

    threshold = credit["threshold_pct"]
    rate = credit["rate_pct"]
    cap = credit["cap_pct"]
    if not all(_valid_pct(v) for v in (threshold, rate, cap)):
        notes.append(
            "The service-credit schedule states a percentage outside 0%% to "
            "100%%, so its credit mechanics were not used."
        )
        return rows, notes

    rows.append(_with_calculation({
        "metric": "Service credit threshold (%s, as drafted)" % _label(schedule_sec),
        "old": "%s%%" % _fmt_num(threshold),
        "new": "%s%%" % _fmt_num(threshold),
        "changed": False,
        "detail": "the schedule still measures credits 'below %s%%'. If the clause is "
                  "amended, the schedule must be conformed - otherwise the clause and "
                  "the schedule contradict each other." % _fmt_num(threshold),
    }, "sla.threshold_as_drafted",
       "credit threshold = stored schedule percentage",
       {"threshold_pct": _fmt_num(threshold),
        "schedule_clause": schedule_sec},
       ["the schedule is read as drafted and is not silently conformed."]))

    rows.append(_with_calculation({
        "metric": "Service credit threshold (%s, if conformed to the amendment)"
                   % _label(schedule_sec),
        "old": "%s%%" % _fmt_num(threshold),
        "new": "%s%%" % _fmt_num(new_number),
        "changed": True,
        "detail": "with the schedule conformed, the Customer earns no credit at all "
                  "between %s%% and %s%% availability"
                  % (_fmt_num(new_number), _fmt_num(threshold)),
    }, "sla.threshold_conformed",
       "conformed credit threshold = submitted new_value",
       {"drafted_threshold_pct": _fmt_num(threshold),
        "new_threshold_pct": _fmt_num(new_number),
        "schedule_clause": schedule_sec},
       ["the conformed-threshold row assumes the parties also amend the "
        "schedule to match the clause."]))

    rows.append(_with_calculation({
        "metric": "Service credit per full percentage point below the threshold "
                   "(%s)" % _label(schedule_sec),
        "old": "%s%% of monthly Charges" % _fmt_num(rate),
        "new": "%s%% of monthly Charges" % _fmt_num(rate),
        "changed": False,
        "detail": "the rate itself is not changed by this amendment",
    }, "sla.credit_rate",
       "credit rate = stored schedule percentage",
       {"rate_pct": _fmt_num(rate), "schedule_clause": schedule_sec},
       ["the credit rate is not part of the requested change."]))

    if charges is None:
        notes.append("No monthly Charges figure could be read from this contract, so "
                     "the credit amounts could not be computed.")
        return rows, notes

    if not _valid_pct(rate) or not _valid_pct(cap):
        notes.append(
            "The service-credit rate or cap is outside 0%% to 100%%, so the "
            "credit amounts were not computed."
        )
        return rows, notes

    first_credit = charges * rate / _SLA_MAX
    max_credit = charges * cap / _SLA_MAX
    first_credit_threshold = threshold - Decimal("1")

    rows.append(_with_calculation({
        "metric": "First service credit earned (%s)" % _label(schedule_sec),
        "old": "%s at %s%% availability" % (
            _fmt_inr(first_credit), _fmt_num(first_credit_threshold)
        ),
        "new": "%s at %s%% availability" % (
            _fmt_inr(first_credit), _fmt_num(new_number - Decimal("1"))
        ),
        "changed": True,
        "detail": "a full percentage point below the threshold earns %s%% of the "
                  "monthly Charges of %s" % (_fmt_num(rate), _fmt_inr(charges)),
    }, "sla.first_credit",
       "first credit = monthly charges * credit rate / 100",
       {"monthly_charges": _fmt_num(charges),
        "rate_pct": _fmt_num(rate),
        "first_credit": _fmt_num(first_credit),
        "old_availability_pct": _fmt_num(first_credit_threshold),
        "new_availability_pct": _fmt_num(new_number - Decimal("1"))},
       ["one full percentage point below the threshold earns one credit step.",
        "the monthly Charges figure is the base and is not itself changed."]))

    rows.append(_with_calculation({
        "metric": "Maximum monthly service credit (%s)" % _label(schedule_sec),
        "old": _fmt_inr(max_credit),
        "new": _fmt_inr(max_credit),
        "changed": False,
        "detail": "the cap is %s%% of the monthly Charges, so the exposure does not "
                  "move - but it takes longer to reach" % _fmt_num(cap),
    }, "sla.max_credit",
       "maximum credit = monthly charges * cap / 100",
       {"monthly_charges": _fmt_num(charges),
        "cap_pct": _fmt_num(cap),
        "max_credit": _fmt_num(max_credit)},
       ["the cap is a percentage of the same monthly Charges base."]))

    rows.append(_with_calculation({
        "metric": "Monthly Charges base (%s)" % _label(charges_sec),
        "old": _fmt_inr(charges),
        "new": _fmt_inr(charges),
        "changed": False,
        "detail": "the credits are calculated on this base",
    }, "sla.charges_base",
       "monthly charges = stored charge amount",
       {"monthly_charges": _fmt_num(charges),
        "charges_clause": charges_sec},
       ["the charge amount is read from the stored clause and is not changed."]))

    return rows, notes


def _counterfactual_status(rows, notes, evidence_status):
    if not rows:
        return ANALYSIS_STATUS_UNSUPPORTED
    if evidence_status == ANALYSIS_STATUS_PARTIAL or notes:
        return ANALYSIS_STATUS_PARTIAL
    return ANALYSIS_STATUS_SUPPORTED


def counterfactual(contract_id, changed_sec, old_value, new_value):
    """Recompute the downstream numbers with the new value.

    For a payment-period change this yields:
        payment deadline          30 days  ->  15 days
        default can arise on      day 45   ->  day 30
        cure period               15 days  ->  15 days  (UNCHANGED, flagged)

    For a service-level change it yields the service-credit exposure instead:
        availability threshold    99%  ->  98%
        credit threshold          99%  ->  99%   (UNCHANGED, flagged)
        first credit earned       INR 22,500 at 98% availability -> at 97%
        maximum monthly credit    INR 1,12,500 (UNCHANGED)

    A change of any other type has no modelled arithmetic, and says so
    rather than inventing rows.
    """
    index = _clause_index(contract_id)
    changed = index.get(changed_sec, {})
    validation_error = _validate_change_values(
        changed_sec, changed, old_value, new_value
    )
    if validation_error is not None:
        # analyze_change() rejects this before calling us. Raising here keeps a
        # direct counterfactual() call from calculating with NaN, infinity,
        # a negative value, or a value in the wrong unit.
        raise ValueError(validation_error["error"])
    ctype = changed.get("type")

    binding = adapter.get_graph_binding(contract_id)
    evidence_status = _analysis_status(binding)

    if ctype == "payment":
        rows, notes = _payment_counterfactual(index, changed_sec, old_value, new_value)
    elif ctype == "sla":
        rows, notes = _sla_counterfactual(index, changed_sec, old_value, new_value)
    else:
        rows = []
        notes = [
            "The day-count and service-credit recalculations are implemented for "
            "payment-period changes and service-level changes only. Clause %s is a "
            "'%s' clause, so the downstream arithmetic for this change type has not "
            "been modelled." % (changed_sec, ctype)
        ]

    status = _counterfactual_status(rows, notes, evidence_status)
    return {
        "rows": rows,
        "notes": notes,
        "old_value": old_value,
        "new_value": new_value,
        "status": status,
        "rule_ids": sorted({row["rule_id"] for row in rows}),
        "assumptions": sorted({
            assumption
            for row in rows
            for assumption in row.get("assumptions", [])
        }),
        "rules_fingerprint": _RULES_FINGERPRINT,
    }


# ---------------------------------------------------------------------------
# 3. DOUBTS  (uncertainty-aware reasoning - Deep feature 5)
# ---------------------------------------------------------------------------
def detect_doubts(
    contract_id,
    changed_sec,
    old_value,
    new_value,
    evidence_status=ANALYSIS_STATUS_SUPPORTED,
):
    """Rules that produce explicit doubts instead of confident guesses.

    Each doubt: {code, severity, question, evidence:[...], review_point,
                 rule_id, status}
    """
    index = _clause_index(contract_id)
    edges = adapter.get_edges(contract_id)
    changed = index.get(changed_sec, {})
    changed_text = changed.get("text", "")
    changed_type = changed.get("type", "other")
    hits = impact_of(contract_id, changed_sec)
    impacted = [h["sec_no"] for h in hits["all"]]

    doubts = []

    def evidence(sec_no, why):
        return _evidence(sec_no, index, why, status=evidence_status)

    def doubt(code, severity, question, evidence_items, review_point):
        return {
            "code": code,
            "severity": severity,
            "question": question,
            "evidence": evidence_items,
            "review_point": review_point,
            "rule_id": "doubt.%s" % code,
            "status": evidence_status,
        }

    # -- D1 definition collision -------------------------------------------
    type_phrase = {
        "payment": "payment period",
        "sla": "service level",
        "fee": "rate",
        "interest": "interest rate",
    }.get(changed_type, "value")
    changed_phrase = _fmt_value(new_value, changed_type)
    ref_edges = [e for e in edges
                 if e["src"] == changed_sec and e["rel"] == "references"
                 and index.get(e["dst"], {}).get("type") == "definition"]
    for e in ref_edges:
        def_clause = index[e["dst"]]
        def_numbers = clause_tools.find_numbers(def_clause.get("text", ""))
        def_phrase = (
            _fmt_numbers(def_numbers)
            if def_numbers else "a separate period"
        )
        doubts.append(doubt(
            "definition_collision",
            "high",
            "Clause %s changes the %s to %s, but Definition %s separately "
            "defines the term and states %s. The system cannot determine from "
            "the text alone whether amending %s alone also moves Definition %s."
            % (changed_sec, type_phrase, changed_phrase, e["dst"], def_phrase,
               changed_sec, e["dst"]),
            [
                evidence(changed_sec, "the clause being changed"),
                evidence(e["dst"], "the definition it depends on"),
            ],
            "Confirm the interaction between Clause %s and Definition %s."
            % (changed_sec, e["dst"]),
        ))
        if changed_type in ("payment", "sla"):
            comparable = (
                _days_in(def_clause)
                if changed_type == "payment" else _pct_in(def_clause)
            )
            if (comparable
                    and _same_number(comparable[0], old_value)
                    and not _same_number(comparable[0], new_value)):
                doubts.append(doubt(
                    "cross_ref_mismatch",
                    "high",
                    "Clause %s now states %s but Definition %s still states %s. "
                    "On one reading the contract is self-contradictory."
                    % (changed_sec, changed_phrase, e["dst"],
                       _fmt_value(comparable[0], changed_type)),
                    [
                        evidence(changed_sec, "new value"),
                        evidence(e["dst"], "old value still stated"),
                    ],
                    "Decide whether Definition %s is amended at the same time."
                    % e["dst"],
                ))

    # -- D2 unchanged dependent parameters ---------------------------------
    for sec in impacted:
        clause = index.get(sec)
        if not clause:
            continue
        nums = [n for n in clause_tools.find_numbers(clause["text"])
                if not any(_same_number(n["value"], value)
                           for value in (old_value, new_value))]
        if not nums:
            continue
        first = nums[0]
        doubts.append(doubt(
            "unchanged_dependent_param",
            "medium",
            "Clause %s is affected by this change but still states %s %s. "
            "The system cannot infer whether that figure should move as well."
            % (sec, _fmt_num(first["value"]), first["unit"]),
            [evidence(sec, "figure left unchanged")],
            "Confirm whether the %s %s in Clause %s should change."
            % (_fmt_num(first["value"]), first["unit"], sec),
        ))
        break  # one representative example is enough; more just adds noise

    # -- D3 notice vs the shortened window ----------------------------------
    if changed_type == "payment":
        notice_secs = [s for s, c in index.items() if c.get("type") == "notice"]
        term_secs = [s for s in impacted
                     if index.get(s, {}).get("type") == "termination"]
        # Pick the notice clause that actually STATES a period. The section
        # anchor ('14. NOTICES') is typed 'notice' but carries only a heading,
        # and reading the anchor first used to silence this doubt completely.
        notice_with_days = [s for s in notice_secs if _days_in(index.get(s, {}))]
        if notice_with_days and term_secs:
            n_days = _days_in(index[notice_with_days[0]])
            if n_days:
                doubts.append(doubt(
                    "notice_vs_cure",
                    "medium",
                    "Termination for non-payment (Clause %s) requires notice "
                    "under Clause %s, which is deemed served only %s days after "
                    "despatch. The shortened payment period reduces the practical "
                    "window for serving that notice."
                    % (term_secs[0], notice_with_days[0], _fmt_num(n_days[0])),
                    [
                        evidence(term_secs[0],
                                 "termination right in the impact chain"),
                        evidence(notice_with_days[0], "notice mechanics"),
                    ],
                    "Check the notice can be served in time under Clause %s."
                    % notice_with_days[0],
                ))

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
        doubts.append(doubt(
            "undefined_term",
            "low",
            'The term "%s" is used in Clause %s but no definition clause '
            "in this contract defines it." % (term, changed_sec),
            [evidence(changed_sec, "term used without definition")],
            'Locate the definition of "%s".' % term,
        ))
        break

    # -- D5 formalities of amendment ---------------------------------------
    for s, c in index.items():
        low = c["text"].lower()
        if "amendment" in low and "writing" in low and "signed" in low:
            doubts.append(doubt(
                "amendment_form",
                "medium",
                "Clause %s requires amendments to be in writing and signed. If the "
                "proposed change is agreed by email or in a meeting, it may not take "
                "effect." % s,
                [evidence(s, "formalities of amendment")],
                "Check the proposed change is executed in the form "
                "Clause %s requires." % s,
            ))
            break

    # -- D6 what the change does not obviously touch ------------------------
    if changed_type == "payment":
        siblings = [s for s, c in index.items()
                    if c.get("type") == "payment" and s != changed_sec
                    and c.get("level", 1) == 1]
        if siblings:
            doubts.append(doubt(
                "scope_of_change",
                "low",
                "The system was asked about Clause %s only. The other payment "
                "clauses (%s) were not recalculated and may also be affected."
                % (changed_sec,
                   ", ".join("Clause %s" % s for s in siblings[:3])),
                [evidence(changed_sec, "scope of the question")],
                "Confirm whether %s need conforming changes."
                % ", ".join("Clause %s" % s for s in siblings[:3]),
            ))

    # -- D7 service level vs the credit schedule ---------------------------
    if changed_type == "sla":
        schedule_sec, credit = _find_credit_schedule(index)
        if credit and not _same_number(credit["threshold_pct"], new_value):
            threshold = credit["threshold_pct"]
            doubts.append(doubt(
                "sla_schedule_mismatch",
                "high",
                "Clause %s now requires %s%% availability, but %s still "
                "provides service credits for availability below %s%%. As drafted, "
                "a supplier delivering %s%% availability would still earn a credit. "
                "The system cannot determine whether the schedule is meant to move "
                "with the clause."
                % (changed_sec, _fmt_num(new_value), _label(schedule_sec),
                   _fmt_num(threshold),
                   _fmt_num(threshold - Decimal("1"))),
                [
                    evidence(changed_sec, "the clause being changed"),
                    evidence(schedule_sec,
                             "the credit schedule it contradicts"),
                ],
                "Decide whether %s is conformed at the same "
                "time as Clause %s." % (_label(schedule_sec), changed_sec),
            ))

    return doubts


def _rank_review_points(doubts, impact_hits):
    """Order review points by existing severity and clause-linked evidence.

    Severity is the primary factor. Ties prefer evidence from clauses closer
    to the changed clause, then a broader set of distinct evidence clauses;
    original doubt order is the final stable tie-breaker.
    """
    severity_order = {"high": 0, "medium": 1, "low": 2}
    hops_by_clause = {hit["sec_no"]: hit["hops"] for hit in impact_hits}
    ranked = []
    for original_order, doubt in enumerate(doubts):
        evidence = doubt.get("evidence") or []
        evidence_ids = list(dict.fromkeys(
            item.get("id") for item in evidence if item.get("id")
        ))
        matching_hops = [hops_by_clause[sec] for sec in evidence_ids
                         if sec in hops_by_clause]
        nearest_hop = min(matching_hops) if matching_hops else None
        severity = doubt.get("severity", "low")
        severity_rank = severity_order.get(severity, severity_order["low"])
        ranked.append({
            "_sort_key": (
                severity_rank,
                nearest_hop if nearest_hop is not None else float("inf"),
                -len(evidence_ids),
                original_order,
            ),
            "_original_order": original_order,
            "code": doubt.get("code", ""),
            "severity": severity,
            "review_point": doubt.get("review_point", ""),
            "evidence": evidence,
            "rule_id": doubt.get("rule_id", ""),
            "status": doubt.get("status", ANALYSIS_STATUS_SUPPORTED),
            "impact_hops": nearest_hop,
            "evidence_clause_count": len(evidence_ids),
        })

    ranked.sort(key=lambda item: item["_sort_key"])
    result = []
    for position, item in enumerate(ranked, start=1):
        hops = item["impact_hops"]
        if hops == 1:
            impact_basis = "directly impacted clause evidence"
        elif hops is not None:
            impact_basis = "impact evidence at %d hops" % hops
        else:
            impact_basis = "no impacted clause in the evidence"
        result.append({
            "rank": position,
            "code": item["code"],
            "severity": item["severity"],
            "review_point": item["review_point"],
            "evidence": item["evidence"],
            "rule_id": item["rule_id"],
            "status": item["status"],
            "impact_hops": hops,
            "evidence_clause_count": item["evidence_clause_count"],
            "priority_reason": (
                "%s severity; %s; %d evidence clause(s)."
                % (item["severity"].capitalize(), impact_basis,
                   item["evidence_clause_count"])
            ),
        })
    return result


# ---------------------------------------------------------------------------
# 4. CONFLICTS inside one contract (thin feature: conflict detection)
# ---------------------------------------------------------------------------
def clash_check(contract_id, evidence_status=ANALYSIS_STATUS_SUPPORTED):
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
            if not _same_number(p_days[0], d_days[0]):
                findings.append({
                    "code": "definition_drift",
                    "severity": "high",
                    "rule_id": "conflict.definition_drift",
                    "status": evidence_status,
                    "definition_clause": dsec,
                    "using_clause": psec,
                    "definition_value": d_days[0],
                    "using_value": p_days[0],
                    "message": ("Definition %s states %s days but Clause %s, "
                                "which uses that definition, states %s days."
                                % (dsec, _fmt_num(d_days[0]), psec,
                                   _fmt_num(p_days[0]))),
                    "evidence": [
                        _evidence(
                            dsec, index, "the definition states this value",
                            status=evidence_status,
                        ),
                        _evidence(
                            psec, index, "the clause that uses it states another value",
                            status=evidence_status,
                        ),
                    ],
                    "review_point": (
                        "Conform Definition %s and Clause %s to one day count."
                        % (dsec, psec)
                    ),
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
def _analysis_binding(contract_id, changed_sec, changed, binding,
                      expected_clause_sha256=None):
    return {
        "contract_id": contract_id,
        "clause_id": changed_sec,
        "clause_version_id": changed.get("version_id"),
        "clause_sha256": changed.get("clause_sha256"),
        "contract_version_id": binding.get("current_version_id"),
        "graph_build_id": binding.get("current_graph_build_id"),
        "graph_build_version_id": binding.get("build_version_id"),
        "graph_build_version": binding.get("build_version"),
        "graph_build_hash": binding.get("build_hash"),
        "graph_state": binding.get("state"),
        "graph_reason_code": binding.get("reason_code"),
        "rules_fingerprint": _RULES_FINGERPRINT,
        "expected_clause_sha256": expected_clause_sha256,
    }


def analyze_change(contract_id, changed_sec, old_value, new_value,
                   expected_clause_sha256=None):
    """Full impact report for one proposed change.

    The request is only a pointer: the authoritative clause is read from the
    database for the requested contract, and old_value must match that stored
    clause. Client-supplied values never become the baseline for arithmetic.

    When ``expected_clause_sha256`` is supplied, it must equal the stored hash
    of the clause being changed. A mismatch (or a legacy clause without a
    hash) is a stale analysis request and fails with HTTP 409 semantics.
    """
    if adapter.get_contract(contract_id) is None:
        return _error(
            "unknown_contract",
            "Contract %s not found." % contract_id,
            http_status=404,
        )

    index = _clause_index(contract_id)
    changed = index.get(changed_sec)
    if not changed:
        return _error(
            "unknown_clause",
            "Clause %s not found in contract %s." % (changed_sec, contract_id),
        )

    if expected_clause_sha256 is not None:
        expected = str(expected_clause_sha256).strip().lower()
        stored_hash = changed.get("clause_sha256")
        if not stored_hash:
            return _error(
                "clause_hash_unavailable",
                "Clause %s has no stored content hash, so the expected hash "
                "cannot be verified. Re-read the contract and retry." % changed_sec,
                http_status=409,
            )
        if stored_hash.lower() != expected:
            return _error(
                "clause_hash_mismatch",
                "Clause %s has changed since this analysis request was prepared. "
                "Re-read the clause and retry." % changed_sec,
                http_status=409,
            )

    binding = adapter.get_graph_binding(contract_id)
    if binding["state"] == adapter.GRAPH_STATE_STALE:
        return _error(
            "graph_stale",
            "Impact analysis is unavailable because the contract's graph is "
            "stale: the clause set changed after the last graph publication. "
            "Rebuild the graph and retry.",
            http_status=409,
        )
    if binding["state"] == adapter.GRAPH_STATE_MISSING:
        return _error(
            "graph_not_built",
            "Impact analysis is unavailable because no published graph is "
            "bound to this contract. Build the graph and retry.",
            http_status=409,
        )

    evidence_status = _analysis_status(binding)

    validation_error = _validate_change_values(
        changed_sec, changed, old_value, new_value
    )
    if validation_error is not None:
        return validation_error

    imp = impact_of(contract_id, changed_sec)
    cfact = counterfactual(contract_id, changed_sec, old_value, new_value)
    doubts = detect_doubts(
        contract_id, changed_sec, old_value, new_value,
        evidence_status=evidence_status,
    )
    clashes = clash_check(contract_id, evidence_status=evidence_status)

    # what the impacted clauses MEAN, not just which numbers they are
    impacted_secs = [h["sec_no"] for h in imp["all"]]
    semantics = affected_semantics(contract_id, impacted_secs)
    semantics["of_changed_clause"] = affected_semantics(contract_id, [changed_sec])

    evidence = [_evidence(
        changed_sec, index, "the clause being changed",
        status=evidence_status,
    )]
    for h in imp["all"]:
        evidence.append(_evidence(
            h["sec_no"], index, "impacted at hop %d" % h["hops"],
            status=evidence_status,
        ))

    ranked_review_points = _rank_review_points(doubts, imp["all"])
    review_points = [item["review_point"] for item in ranked_review_points]

    changed_text = changed.get("text", "") or ""
    binding_payload = _analysis_binding(
        contract_id, changed_sec, changed, binding,
        expected_clause_sha256=expected_clause_sha256,
    )
    cfact["analysis_binding"] = binding_payload
    cfact["rules_fingerprint"] = _RULES_FINGERPRINT

    return {
        "changed_clause": {
            "sec_no": changed_sec,
            "type": changed.get("type"),
            "heading": changed.get("heading"),
            "old_value": old_value,
            "new_value": new_value,
            "text": changed_text,
            "text_length": len(changed_text),
            "text_excerpt": _excerpt(changed_text, limit=600),
            "text_excerpt_truncated": len(changed_text) > 600,
            "version_id": changed.get("version_id"),
            "clause_sha256": changed.get("clause_sha256"),
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
        "ranked_review_points": ranked_review_points,
        "analysis_status": evidence_status,
        "analysis_binding": binding_payload,
        "rules_fingerprint": _RULES_FINGERPRINT,
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
