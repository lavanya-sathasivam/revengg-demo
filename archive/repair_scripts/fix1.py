#!/usr/bin/env python
import sys

with open('D:\\Desktop\\revengg-demo\\adapter.py', 'r') as f:
    content = f.read()

# Fix 1: get_nodes() SELECT query
# The exact string from the file analysis:
old_nodes = 'SELECT node_id, clause_reference, node_type, contract_id, confidence, evidence_status,\n                      %s\n               FROM nodes WHERE contract_id = ?%s ORDER BY node_id"""\n            % (detail_expr, build_clause),'

new_nodes = 'SELECT node_id, clause_reference, node_type, contract_id,\n                      %s, %s, %s\n               FROM nodes WHERE contract_id = ?%s ORDER BY node_id"""\n            % (confidence_expr, evidence_status_expr, detail_expr, build_clause),'

if old_nodes in content:
    content = content.replace(old_nodes, new_nodes)
    print("Fixed get_nodes() SELECT query", file=sys.stderr)
else:
    print("Could not find get_nodes() SELECT query to replace", file=sys.stderr)
    # Debug: print surrounding context
    idx = content.find('SELECT node_id, clause_reference, node_type, contract_id, confidence, evidence_status')
    if idx >= 0:
        print("Found at index", idx, file=sys.stderr)
        print(repr(content[idx:idx+150]), file=sys.stderr)

with open('D:\\Desktop\\revengg-demo\\adapter.py', 'w') as f:
    f.write(content)

print("Done writing")