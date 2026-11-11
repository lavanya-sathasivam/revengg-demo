#!/usr/bin/env python
"""Fix adapter.py getter functions for legacy compatibility."""

with open(r"D:\Desktop\revengg-demo\adapter.py", "r") as f:
    content = f.read()

# Fix 1: Update the SELECT statement in get_nodes
# Old: """SELECT node_id, clause_reference, node_type, contract_id, confidence, evidence_status,
# New: """SELECT node_id, clause_reference, node_type, contract_id, %s, %s, %s,
old_select = '"""SELECT node_id, clause_reference, node_type, contract_id, confidence, evidence_status,'
new_select = '"""SELECT node_id, clause_reference, node_type, contract_id, %s, %s, %s,'

if old_select in content:
    content = content.replace(old_select, new_select)
    print("Fixed get_nodes SELECT header")
else:
    print("get_nodes SELECT header not found")

# Fix 2: Update the %s parameters tuple
# Old: % (detail_expr, build_clause)
# New: % (detail_expr, confidence_expr, evidence_status_expr, build_clause)
old_params = "% (detail_expr, build_clause)"
new_params = "% (detail_expr, confidence_expr, evidence_status_expr, build_clause)"

if old_params in content:
    content = content.replace(old_params, new_params)
    print("Fixed get_nodes parameters tuple")
else:
    print("get_nodes parameters tuple not found")

# Fix 3: Update get_edges SELECT
# The get_edges function needs to include confidence, evidence_quality, relationship_source
# Let me find and update it

# Current get_edges SELECT (around line 1119-1125)
old_edges_select = 'e.relationship_type AS rel'
new_edges_select = 'e.relationship_type AS rel, e.confidence, e.evidence_quality, e.relationship_source'

if old_edges_select in content:
    content = content.replace(old_edges_select, new_edges_select, 1)
    print("Fixed get_edges SELECT")
else:
    print("get_edges SELECT not found")

with open(r"D:\Desktop\revengg-demo\adapter.py", "w") as f:
    f.write(content)

print("Done fixing adapter.py")