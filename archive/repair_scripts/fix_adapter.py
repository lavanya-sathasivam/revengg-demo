#!/usr/bin/env python3
"""Fix the adapter.py get_nodes() and get_edges() NULL AS issues."""

with open('D:\\Desktop\\revengg-demo\\adapter.py', 'r') as f:
    content = f.read()

# Fix 1: get_nodes() SELECT query
old_nodes = '''            """SELECT node_id, clause_reference, node_type, contract_id, confidence, evidence_status,
                      %s
                 FROM nodes WHERE contract_id = ?%s ORDER BY node_id"""
            % (detail_expr, build_clause),'''

new_nodes = '''            """SELECT node_id, clause_reference, node_type, contract_id,
                      %s, %s, %s
                 FROM nodes WHERE contract_id = ?%s ORDER BY node_id"""
            % (confidence_expr, evidence_status_expr, detail_expr, build_clause),'''

if old_nodes in content:
    content = content.replace(old_nodes, new_nodes)
    print("Fixed get_nodes() SELECT query")
else:
    print("Could not find get_nodes() SELECT query to replace")

# Fix 2: get_edges() SELECT query - use NULL AS for missing columns
old_edges = '''        rows = conn.execute(
            """SELECT s.clause_reference AS src_ref, t.clause_reference AS dst_ref,
                      e.relationship_type AS rel, e.confidence, e.evidence_quality,
                      e.relationship_source
                 FROM edges e
                 JOIN nodes s ON s.node_id = e.source_node
                 JOIN nodes t ON t.node_id = e.target_node
                 WHERE %s
                 ORDER BY e.edge_id"""
            % " AND ".join(conditions),'''
new_edges = '''        # Build NULL-AS expressions for columns that may be absent in legacy databases
        confidence_edge = "e.confidence" if "confidence" in edge_columns else "NULL AS confidence"
        evidence_quality_edge = "e.evidence_quality" if "evidence_quality" in edge_columns else "NULL AS evidence_quality"
        relationship_source_edge = "e.relationship_source" if "relationship_source" in edge_columns else "NULL AS relationship_source"

        rows = conn.execute(
            """SELECT s.clause_reference AS src_ref, t.clause_reference AS dst_ref,
                      e.relationship_type AS rel, %s, %s, %s
                 FROM edges e
                 JOIN nodes s ON s.node_id = e.source_node
                 JOIN nodes t ON t.node_id = e.target_node
                 WHERE %s
                 ORDER BY e.edge_id"""
            % (confidence_edge, evidence_quality_edge, relationship_source_edge, " AND ".join(conditions)),'''
  
if old_edges in content:
    content = content.replace(old_edges, new_edges)
    print("Fixed get_edges() SELECT query")
else:
    print("Could not find get_edges() SELECT query to replace")

with open('D:\\Desktop\\revengg-demo\\adapter.py', 'w') as f:
    f.write(content)

print("Done")