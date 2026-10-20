"""Console demo of the impact analysis. Owner: Person B (branch b-graph).

This is deliberately NOT a web page. Person C owns the UI. Branch B has to be
demonstrable on its own, straight from the terminal, so that the project still
shows a working impact engine if the UI slips.

Usage:
    py -3.11 cli.py --list
    py -3.11 cli.py --contract 1 --change 7.2:30:15
    py -3.11 cli.py --contract 1 --graph
    py -3.11 cli.py --compare 1 2
"""

import argparse
import os

import db

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO_DB = os.path.join(HERE, "demo.db")

# Branch B's demo data lives in demo.db so it never overwrites the contract
# Person A uploaded into app.db (their test suite reads contract 1 from it).
if os.path.exists(DEMO_DB):
    db.get_db_path = lambda: DEMO_DB

import adapter     # noqa: E402
import graph       # noqa: E402
import impact      # noqa: E402


def print_graph(contract_id):
    nodes = adapter.get_nodes(contract_id)
    edges = adapter.get_edges(contract_id)
    index = {c["section_number"]: c for c in adapter.list_clauses(contract_id)}

    print("\n=== CONTRACT RELATIONSHIP MODEL (contract %d) ===" % contract_id)
    print("\nNODES")
    for n in nodes:
        print("  [%-14s] %-14s %s" % (n["node_type"], n["clause_reference"],
                                      n.get("detail", "")[:52]))

    print("\nEDGES")
    for e in edges:
        if e["rel"] == "creates":
            continue
        print("  %-12s %-12s %-12s   (%s)" %
              (e["src"], e["rel"], e["dst"],
               index.get(e["dst"], {}).get("section_number", "-")))
    print()


def print_impact(report):
    c = report["changed_clause"]
    print("\n" + "=" * 74)
    print("CHANGED CLAUSE: %s (%s)" % (c["sec_no"], c["type"]))
    print("  %s  ->  %s" % (c["old_value"], c["new_value"]))
    print("=" * 74)

    print("\nDIRECT IMPACT")
    if report["direct"]:
        for h in report["direct"]:
            print("  - Clause %s" % h["sec_no"])
    else:
        print("  (none)")

    print("\nINDIRECT IMPACT")
    if report["indirect"]:
        for h in report["indirect"]:
            print("  - Clause %s  (hop %d)" % (h["sec_no"], h["hops"]))
    else:
        print("  (none)")

    print("\nCAUSAL CHAIN")
    print("  " + "  ->  ".join(report["chain"]))

    print("\nOLD vs NEW")
    for row in report["counterfactual"]["rows"]:
        flag = "CHANGED" if row["changed"] else "unchanged"
        print("  %-52s %-12s -> %-12s [%s]" %
              (row["metric"], row["old"], row["new"], flag))
        if row.get("detail"):
            print("      %s" % row["detail"])
    for note in report["counterfactual"]["notes"]:
        print("  ! %s" % note)

    print("\nUNCERTAINTY (what we are NOT sure about)")
    for d in report["doubts"]:
        print("  [%s / %s] %s" % (d["code"], d["severity"], d["question"]))
        for ev in d["evidence"]:
            print("        evidence: Clause %s (%s)" % (ev["id"], ev["why"]))

    if report["conflicts"]:
        print("\nINTERNAL CONFLICTS")
        for k in report["conflicts"]:
            print("  - %s" % k["message"])

    print("\nLAWYER REVIEW POINTS")
    for rp in report["review_points"]:
        print("  - %s" % rp)

    print("\nEVIDENCE")
    for ev in report["evidence"]:
        print("  Clause %-10s (%-12s) %s" % (ev["id"], ev["type"], ev["why"]))

    print("\nSUMMARY")
    print("  " + report["summary"])
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="list contracts")
    ap.add_argument("--contract", type=int, help="contract id")
    ap.add_argument("--change", help="SEC_NO:OLD:NEW  e.g. 7.2:30:15")
    ap.add_argument("--graph", action="store_true", help="print the relationship model")
    ap.add_argument("--compare", nargs=2, type=int, metavar=("A", "B"),
                    help="compare two contracts")
    args = ap.parse_args()

    if args.compare:
        result = impact.compare_contracts(args.compare[0], args.compare[1])
        print("\nCROSS-CONTRACT COMPARISON  (contract %d vs contract %d)"
              % (args.compare[0], args.compare[1]))
        print("  A: %s" % result["contract_a"])
        print("  B: %s" % result["contract_b"])
        if result["mismatches"]:
            print("\n  MISMATCHES")
            for m in result["mismatches"]:
                print("   - %s" % m["message"])
        else:
            print("\n  no mismatches found")

        anomaly = impact.portfolio_anomaly([args.compare[0], args.compare[1]])
        print("\nPORTFOLIO ANOMALY")
        print("  payment periods: %s" % [r["payment_days"] for r in anomaly["rows"]])
        for o in anomaly.get("outliers", []):
            print("   - contract %s is the odd one out (%s days)"
                  % (o["contract_id"], o["payment_days"]))
        return

    if args.list or args.contract is None:
        print("\nCONTRACTS")
        for c in adapter.list_contracts():
            print("  %d  %s  (%s)" % (c["contract_id"], c["filename"],
                                      c["processing_status"]))
        print("\nuse: --contract <id> --change 7.2:30:15")
        return

    if args.graph:
        print_graph(args.contract)
        return

    if args.change:
        try:
            sec, old, new = args.change.split(":")
            report = impact.analyze_change(args.contract, sec, int(old), int(new))
        except ValueError:
            print("bad --change format. use SEC_NO:OLD:NEW e.g. 7.2:30:15")
            return
        if "error" in report:
            print(report["error"])
            return
        print_impact(report)


if __name__ == "__main__":
    main()
