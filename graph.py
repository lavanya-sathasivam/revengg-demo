"""Contract Relationship Model: nodes, typed edges, and impact traversal.

Owner: Person B (branch b-graph).

THE MODEL
    A contract is stored as three things:
        clauses  - the numbered text (Person A's table)
        nodes    - the legal THINGS a clause creates (a payment duty, a
                   deadline, a condition, a termination right)
        edges    - typed LINKS between clauses and nodes

    Edge vocabulary (frozen Day 1):
        creates      clause -> node       '7.2 creates a payment duty'
        depends_on   dependent -> dependency
                     '10.1 depends_on 8.1'  (10.1 relies on 8.1)
        subject_to   node -> node         'payment subject_to 30-day deadline'
        causes       cause -> effect       (reserved; not emitted by default)
        references   referrer -> referenced ('7.2 references 2.4')

    DIRECTION RULE - this is the single easiest thing to get wrong:
        'depends_on' is stored (dependent, dependency).
        Therefore the impact of X is found by walking 'depends_on' BACKWARDS:
        who depends on X? Those are the clauses a change to X hurts.

WHY THE RULES TABLE INSTEAD OF PURE LLM
    The relationship between "a late fee clause" and "a default clause" is not
    a similarity question, it is a legal question. We encode it as a typed
    relation table over clause types. That gives us:
      - reproducible output (same contract -> same graph, every time)
      - an auditable trail (a lawyer can see WHICH rule fired)
      - offline operation (no API key needed for the core demo)
    The LLM is used only to fill gaps the rule table does not cover.
"""

import adapter
import clauses as clause_tools
import llm

# ---------------------------------------------------------------------------
# TYPED RELATION TABLE
# (source clause type, referenced clause type) -> these are true dependencies
# Each reference is judged on its own, so a default clause that mentions both
# the late-fee clause and the payment clause depends on BOTH - which is
# correct: the late payment is what actually triggers the default.
# ---------------------------------------------------------------------------
DEPENDENCY_RULES = [
    ("default", "fee"),
    ("default", "payment"),
    ("default", "interest"),
    ("default", "cure"),
    ("cure", "default"),
    ("cure", "payment"),
    ("termination", "default"),
    ("termination", "cure"),
    ("termination", "payment"),
    ("fee", "payment"),
    ("interest", "payment"),
    ("sla", "payment"),
]

# A definition referenced by an operative clause is an interpretation target,
# not a hard dependency. It becomes a DOUBT, not damage.
REFERENCE_ONLY_RULES = [
    ("payment", "definition"),
    ("fee", "definition"),
    ("termination", "definition"),
    ("interest", "definition"),
    ("default", "definition"),
    ("cure", "definition"),
]

# The relation table above assumes the REFERRING clause is the dependent: a
# default clause that mentions the late-fee clause depends on it, so the
# edge is (referrer -> referenced).
#
# That assumption is wrong for one pair in this corpus. Clause 9.1 says
# 'Failure to achieve the Service Levels entitles the Customer to the service
# credits set out in Clause 9.2' - but it is the SERVICE CREDITS that depend
# on the service level, not the service level that depends on the credits.
# These pairs are listed separately and the edge is emitted the other way
# round, so 'depends_on' stays dependent-first and the impact of the SLA is
# the credit clause.
INVERSE_DEPENDENCY_RULES = [
    ("sla", "fee"),
]

EXTRACT_PROMPT = """
You extract legal structure from ONE clause of an Indian commercial service agreement.
Return STRICT JSON only. No prose. No markdown fence.

Clause number: {sec_no}
Clause type (already assigned by a rule table): {ctype}
Clause text:
{text}

Return exactly this shape:
{{"creates": [{{"kind": "<obligation|condition|right|deadline|payment|termination|definition>",
                "detail": "<max 12 words, only words present in the clause>"}}],
  "depends_on": ["<clause numbers this clause relies on, e.g. 8.1>"]}}

Rules:
- Use ONLY words that appear in the clause text. Never invent a term.
- depends_on = clauses whose trigger, condition, period or remedy this clause uses.
- If the clause creates no obligation, "creates" is [].
- Output JSON only.
"""


def _element_ref(sec_no, kind):
    return "%s::%s" % (sec_no, kind)


# ---------------------------------------------------------------------------
# 1. segmentation -> clauses in the database (Person A's storage)
# ---------------------------------------------------------------------------
def ingest_contract(contract_id, raw_text):
    """Split raw contract text into fine-grained clauses and store them.

    Top-level section detection is Person A's extract.split_clauses();
    we add the sub-clause hierarchy on top. Returns the clause count.
    """
    items = clause_tools.split_clauses(raw_text)
    if not items:
        return 0

    rows = []
    for item in items:
        meta = dict(item.get("metadata") or {})
        meta["type"] = clause_tools.classify_type(
            item["clause_text"], meta.get("heading", "")
        )
        rows.append({
            "section_number": item["section_number"],
            "clause_text": item["clause_text"],
            "metadata": meta,
        })

    adapter.save_clauses(contract_id, rows)
    return len(rows)


# ---------------------------------------------------------------------------
# 2. deterministic nodes (what a clause CREATES)
# ---------------------------------------------------------------------------
def _deterministic_nodes(clause):
    """Return [(kind, detail)] created by a clause, decided by rule."""
    ctype = clause["type"]
    text = clause["text"]
    out = []

    if ctype == "payment":
        # The node is a legal statement about the contract, so it has to be
        # true OF THIS CLAUSE. One generic 'pay the amount stated in each
        # undisputed Invoice' template would be false for Clause 7.1 (which
        # imposes an invoicing duty, not a payment duty) and for Clause 7.3
        # (which lets the Customer WITHHOLD the disputed part).
        low = text.lower()
        if clause_tools.mentions_dispute(text):
            out.append(("payment", "pay the undisputed balance of any disputed Invoice"))
        elif "issue" in low and "invoice" in low:
            out.append(("obligation", "issue an Invoice for the Services"))
        elif "charge" in low:
            out.append(("payment", "pay the Charges"))
        else:
            out.append(("payment", "pay the amount stated in each undisputed Invoice"))

        within = clause_tools.find_within_days(text)
        if within and "invoice" in low:
            # Careful: a plain substring test for 'disput' also matches
            # 'undisputed', which is in EVERY payment clause - it would give
            # Clause 7.2 a deadline saying 'notify the Supplier of a dispute
            # within 30 days'. A dispute-notice deadline only belongs to a
            # clause that actually speaks about a dispute.
            if clause_tools.mentions_dispute(text):
                out.append(("deadline", "notify the Supplier of a dispute within %d days"
                            % within))
            else:
                out.append(("deadline", "pay within %d days of the Invoice date" % within))
    elif ctype == "fee":
        # A clause in the late-payment family does not necessarily IMPOSE a
        # charge. Clause 8.3 ('... are not a penalty') creates no obligation,
        # and Clause 9.2 (Service Credits) creates a RIGHT, not a duty to pay
        # late charges. Creating the wrong node here would put a false
        # statement about the contract into the impact report.
        low = text.lower()
        if "late charge" in low or "late fee" in low:
            out.append(("obligation", "pay late charges on any overdue Invoice"))
        elif "service credit" in low:
            out.append(("right", "claim service credits for the Service Levels not met"))
    elif ctype == "interest":
        out.append(("obligation", "pay interest on any amount paid late"))
    elif ctype == "default":
        out.append(("condition", "Customer remains in default until the failure is cured"))
    elif ctype == "cure":
        days = [n["value"] for n in clause["numbers"] if n["unit"].startswith("day")]
        if days:
            out.append(("deadline", "Cure Period of %d days after notice" % days[0]))
        else:
            out.append(("deadline", "Cure Period after notice"))
    elif ctype == "termination":
        out.append(("termination", "terminate the Agreement on the stated ground"))
    elif ctype == "notice":
        out.append(("obligation", "serve every notice in writing"))
    elif ctype == "sla":
        out.append(("obligation", "achieve the Service Levels"))
    elif ctype == "definition":
        terms = clause_tools.find_quoted_terms(text)
        detail = 'define "%s"' % terms[0] if terms else "define a term"
        out.append(("definition", detail))
    elif ctype == "confidentiality":
        out.append(("obligation", "keep the other Party's information confidential"))
    elif ctype == "indemnity":
        out.append(("obligation", "indemnify the other Party"))
    elif ctype == "limitation":
        out.append(("right", "cap and exclude liability"))

    return out


# ---------------------------------------------------------------------------
# 3. build the whole graph
# ---------------------------------------------------------------------------
def build_graph(contract_id, use_llm=True, llm_limit=None, llm_target=None):
    """Build nodes and edges for a contract. Idempotent: rebuilds from scratch.

    use_llm     - allow the LLM to fill gaps the relation table left.
    llm_limit   - maximum number of LLM calls (None = no limit). A real key
                  on a 59-clause contract means one call per undecided
                  clause, so a limit keeps the live demo quick and cheap.
    llm_target  - only ask the LLM about this one clause. Useful to show
                  exactly what the model would contribute.

    Returns a summary dict with counts.
    """
    raw_clauses = adapter.list_clauses(contract_id)
    if not raw_clauses:
        return {"clauses": 0, "nodes": 0, "edges": 0, "llm_fills": 0,
                "llm_calls": 0}

    enriched = []
    for row in raw_clauses:
        meta = adapter.clause_metadata(row)
        c = clause_tools.enrich({
            "section_number": row["section_number"],
            "clause_text": row["clause_text"],
            "heading": meta.get("heading", ""),
        })
        if meta.get("type"):
            c["type"] = meta["type"]
        c["id"] = row["clause_id"]
        enriched.append(c)
    enriched.sort(key=lambda c: clause_tools.sec_sort_key(c["sec_no"]))

    by_sec = {c["sec_no"]: c for c in enriched}
    types = {c["sec_no"]: c["type"] for c in enriched}

    adapter.clear_graph(contract_id)

    node_count = 0
    edge_count = 0
    llm_fills = 0
    llm_calls = 0

    # pass 1: one node per clause, so every edge can point at a real node
    clause_node = {}
    for clause in enriched:
        clause_node[clause["sec_no"]] = adapter.insert_clause_node(
            contract_id, clause["sec_no"], clause["type"], clause["heading"]
        )
        node_count += 1

    # pass 2: the things each clause creates, and the typed links
    for clause in enriched:
        sec = clause["sec_no"]
        ctype = clause["type"]
        text = clause["text"]

        element_nodes = []
        for kind, detail in _deterministic_nodes(clause):
            ref = _element_ref(sec, kind)
            nid = adapter.insert_element_node(contract_id, ref, kind, detail)
            node_count += 1
            element_nodes.append((ref, kind, nid))
            adapter.insert_edge(contract_id, clause_node[sec], nid, "creates")
            edge_count += 1

        # a payment duty is conditional on its deadline
        for ref, kind, nid in element_nodes:
            if kind == "payment":
                for dref, dkind, dnid in element_nodes:
                    if dkind == "deadline":
                        adapter.insert_edge(contract_id, nid, dnid, "subject_to")
                        edge_count += 1

        # edges from explicit clause references
        refs = clause_tools.find_references(text)
        rule_dep_made = False
        for ref in refs:
            if ref == sec or ref not in by_sec:
                continue
            rtype = types[ref]
            if (ctype, rtype) in DEPENDENCY_RULES:
                adapter.insert_edge(contract_id, clause_node[sec], clause_node[ref],
                                    "depends_on")
                rule_dep_made = True
                edge_count += 1
            elif (ctype, rtype) in INVERSE_DEPENDENCY_RULES:
                # the REFERENCED clause is the dependent here
                adapter.insert_edge(contract_id, clause_node[ref], clause_node[sec],
                                    "depends_on")
                rule_dep_made = True
                edge_count += 1
            else:
                adapter.insert_edge(contract_id, clause_node[sec], clause_node[ref],
                                    "references")
                edge_count += 1

        # the LLM only fills gaps the rule table left
        if use_llm and not rule_dep_made and _llm_allowed(sec, llm_target,
                                                           llm_calls, llm_limit):
            llm_calls += 1
            payload = llm.ask_json(
                EXTRACT_PROMPT.format(sec_no=sec, ctype=ctype, text=text[:4000])
            )
            if payload:
                llm_fills += 1
                for ref in payload.get("depends_on", []) or []:
                    ref = str(ref).strip()
                    if ref and ref != sec and ref in by_sec:
                        adapter.insert_edge(contract_id, clause_node[sec],
                                            clause_node[ref], "depends_on")
                        edge_count += 1
                for item in payload.get("creates", []) or []:
                    kind = item.get("kind")
                    detail = (item.get("detail") or "").strip()
                    if kind and detail:
                        ref = _element_ref(sec, kind)
                        nid = adapter.insert_element_node(contract_id, ref, kind,
                                                         detail[:120])
                        node_count += 1
                        adapter.insert_edge(contract_id, clause_node[sec], nid, "creates")
                        edge_count += 1

    return {"clauses": len(enriched), "nodes": node_count, "edges": edge_count,
            "llm_fills": llm_fills, "llm_calls": llm_calls}


def _llm_allowed(sec, llm_target, calls, llm_limit):
    """Should the LLM be asked about this clause?"""
    if llm_target is not None and sec != llm_target:
        return False
    if llm_limit is not None and calls >= llm_limit:
        return False
    return True


# ---------------------------------------------------------------------------
# 4. traversal
# ---------------------------------------------------------------------------
def impact_adjacency(edges):
    """Turn the edge list into 'who is hurt if I change X'.

    depends_on (dependent, dependency): walking BACKWARDS.
    causes    (cause, effect):           walking FORWARDS.
    references: NOT walked - a reference is an interpretation target and
                becomes a DOUBT, not a hard impact.
    """
    adj = {}
    for e in edges:
        src, dst, rel = e["src"], e["dst"], e["rel"]
        if rel == "depends_on":
            adj.setdefault(dst, []).append(src)
        elif rel == "causes":
            adj.setdefault(src, []).append(dst)
    return adj


def find_impact(start_sec, edges, max_hops=3):
    """Breadth-first search over the impact adjacency.

    Returns [{'sec_no', 'rel', 'hops'}] ordered by hop count.
    """
    adj = impact_adjacency(edges)
    seen = {start_sec}
    frontier = [start_sec]
    out = []
    hop = 0
    while frontier and hop < max_hops:
        nxt = []
        for cur in frontier:
            for neighbour in adj.get(cur, []):
                if neighbour in seen:
                    continue
                seen.add(neighbour)
                nxt.append(neighbour)
                out.append({"sec_no": neighbour, "hops": hop + 1})
        frontier = nxt
        hop += 1
    return out


def chain_text(start_sec, edges, max_hops=3):
    """A readable causal chain: '8.1 -> 10.1 -> 12.3'."""
    adj = impact_adjacency(edges)
    path = [start_sec]
    cur = start_sec
    used = {cur}
    for _ in range(max_hops):
        neighbours = [n for n in adj.get(cur, []) if n not in used]
        if not neighbours:
            break
        cur = neighbours[0]
        used.add(cur)
        path.append(cur)
    return path


def get_graph(contract_id):
    """Everything the UI needs to draw the relationship model."""
    return {
        "nodes": adapter.get_nodes(contract_id),
        "edges": adapter.get_edges(contract_id),
    }


def clause_type_map(contract_id):
    return {c["sec_no"]: c["type"] for c in adapter.list_clauses(contract_id)}
