"""Exercise the LLM path - the one place a language model is used.

Owner: Person B (branch b-graph).

WHY THIS SCRIPT EXISTS
    The system uses the LLM for exactly one job: to fill gaps the typed
    relation table leaves. A clause that references another clause whose type
    pair is NOT in the table gets asked directly - 'which clauses does this
    one rely on?' - and the answer is inserted as a `depends_on` edge.

    That path is invisible in the demo (it needs an API key), so it is easy
    to break without noticing. This script makes it visible and testable.

USAGE
    py -3.11 tools/llm_probe.py                      # is the LLM configured?
    py -3.11 tools/llm_probe.py --contract 1 --clause 3.3
    py -3.11 tools/llm_probe.py --contract 1 --clause 3.3 --rebuild

    --rebuild rebuilds the contract's graph WITH the LLM enabled, so you can
    see the edges the model contributed.

WHAT TO EXPECT
    With no key configured (LLM_PROVIDER=mock) the script says so and exits
    cleanly. That is the important result: the core demo never needs a key,
    and a missing key never crashes anything.

    With a key configured, the script prints the clause, the prompt, the raw
    model answer and the parsed JSON, then says which edges the answer would
    add to the graph.

CHECKLIST BEFORE YOU SHOW THIS TO ANYBODY
    - The key lives in .env and .env is git-ignored. Never paste a key into
      a chat, a commit, a screenshot or a shared drive.
    - The repo stays private until the provisional patent is filed.
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import db          # noqa: E402
import adapter     # noqa: E402
import graph       # noqa: E402
import impact      # noqa: E402
import llm         # noqa: E402

DEMO_DB = os.path.join(ROOT, "demo.db")


def show_configuration():
    provider = llm.provider_name()
    print("LLM_PROVIDER      : %s" % provider)
    print("provider available: %s" % llm.provider_available())
    if provider == "openai":
        print("OPENAI_API_KEY set: %s" % bool(os.environ.get("OPENAI_API_KEY")))
        print("model             : %s" % os.environ.get("LLM_MODEL", "gpt-4o-mini"))
    elif provider == "gemini":
        print("GEMINI_API_KEY set: %s" % bool(os.environ.get("GEMINI_API_KEY")))
        print("model             : %s" % os.environ.get("LLM_MODEL", "gemini-2.0-flash"))
    else:
        print("\nNo provider is configured, so ask_json() returns {} and the "
              "rule table does all the work.\n"
              "That is the intended default: the demo must work with no key "
              "at all.")
    return provider, llm.provider_available()


def probe_clause(contract_id, sec_no):
    """Ask the model about one clause and show what came back."""
    index = impact._clause_index(contract_id)
    clause = index.get(sec_no)
    if not clause:
        print("Clause %s is not in contract %s." % (sec_no, contract_id))
        return

    ctype = clause.get("type")
    text = clause.get("text", "")

    # what the rule table already decided
    edges = adapter.get_edges(contract_id)
    rule_edges = sorted({(e["src"], e["dst"]) for e in edges
                         if e["rel"] == "depends_on"
                         and (e["src"] == sec_no or e["dst"] == sec_no)})
    refs = sorted({e["dst"] for e in edges
                   if e["rel"] == "references" and e["src"] == sec_no})

    print("\n" + "=" * 74)
    print("CLAUSE %s  (type: %s)" % (sec_no, ctype))
    print("=" * 74)
    print(text[:1200])
    print("\nThe rule table already decided:")
    for src, dst in rule_edges:
        print("  depends_on  %s -> %s" % (src, dst))
    for dst in refs:
        print("  references  %s -> %s" % (sec_no, dst))
    if not rule_edges and not refs:
        print("  (nothing - this clause is exactly where the LLM gets asked)")

    prompt = graph.EXTRACT_PROMPT.format(sec_no=sec_no, ctype=ctype,
                                          text=text[:4000])
    print("\n--- PROMPT SENT ---")
    print(prompt.strip())

    raw = None
    if llm.provider_available():
        provider = llm.provider_name()
        try:
            raw = (llm._call_gemini(prompt) if provider == "gemini"
                   else llm._call_openai(prompt))
        except Exception as exc:
            print("\nThe provider raised %s - ask_json() would swallow this "
                  "and return {}." % type(exc).__name__)
            raw = None

    if raw is None:
        print("\n--- NO MODEL ANSWER ---")
        print("ask_json() returns {} here, and the graph falls back to the "
              "rule table. Nothing breaks.")
        return

    print("\n--- RAW MODEL ANSWER ---")
    print(str(raw)[:1200])

    payload = llm.ask_json(prompt)
    print("\n--- PARSED JSON ---")
    print(payload)

    for ref in payload.get("depends_on", []) or []:
        ref = str(ref).strip()
        verdict = "already known" if (sec_no, ref) in rule_edges else "NEW EDGE"
        print("\n  %s -> %s   (%s)" % (sec_no, ref, verdict))
    for item in payload.get("creates", []) or []:
        print("\n  creates  %s :: %s  ->  %s"
              % (sec_no, item.get("kind"), item.get("detail")))


def rebuild_with_llm(contract_id, target=None, limit=5):
    """Rebuild the graph with the LLM enabled and report what it added.

    By default the model is asked about at most `limit` clauses, and about
    `target` only if one is named. A real key on a 59-clause contract means
    one call per clause the rule table did not settle - dozens of calls -
    which is slow and costs money. The demo only needs one.
    """
    print("\nRebuilding contract %s with use_llm=True (target=%s, limit=%s) ..."
          % (contract_id, target or "any", limit))
    before = {(e["src"], e["dst"], e["rel"]) for e in adapter.get_edges(contract_id)}
    stats = graph.build_graph(contract_id, use_llm=True, llm_limit=limit,
                              llm_target=target)
    print("clauses=%d nodes=%d edges=%d llm_calls=%d llm_fills=%d"
          % (stats["clauses"], stats["nodes"], stats["edges"],
             stats["llm_calls"], stats["llm_fills"]))
    after = {(e["src"], e["dst"], e["rel"]) for e in adapter.get_edges(contract_id)}
    added = sorted(after - before)
    if not added:
        print("no new edges (the LLM either was not configured, or agreed "
              "with the rule table)")
    for src, dst, rel in added:
        print("  + %s %s %s" % (src, rel, dst))
    print("\nRebuild to get the clean, deterministic graph back:\n"
          "    py -3.11 seed.py")


def main():
    ap = argparse.ArgumentParser(description="Exercise the LLM path.")
    ap.add_argument("--contract", type=int, default=1)
    ap.add_argument("--clause", help="clause number, e.g. 3.3")
    ap.add_argument("--rebuild", action="store_true",
                    help="rebuild the graph with the LLM enabled")
    ap.add_argument("--limit", type=int, default=5,
                    help="maximum LLM calls during --rebuild (default 5)")
    args = ap.parse_args()

    if os.path.exists(DEMO_DB):
        db.get_db_path = lambda: DEMO_DB

    provider, available = show_configuration()

    if args.clause:
        probe_clause(args.contract, args.clause)

    if args.rebuild:
        rebuild_with_llm(args.contract, target=args.clause, limit=args.limit)

    if not available:
        print("\nTo run this for real:\n"
              "  1. put the key in .env (never in a commit):\n"
              "       LLM_PROVIDER=gemini\n"
              "       GEMINI_API_KEY=...\n"
              "     or LLM_PROVIDER=openai with OPENAI_API_KEY\n"
              "  2. install the SDK for that provider (it is deliberately NOT\n"
              "     in requirements.txt - the core demo needs no key):\n"
              "       py -3.11 -m pip install google-genai\n"
              "       py -3.11 -m pip install openai\n"
              "  3. py -3.11 tools/llm_probe.py --contract 1 --clause 3.3 --rebuild\n"
              "  4. remove the key afterwards and confirm the demo still works:\n"
              "       py -3.11 seed.py\n"
              "       py -3.11 -m unittest discover -s tests")


if __name__ == "__main__":
    main()
