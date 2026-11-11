#!/usr/bin/env python
import sys

with open('D:\\Desktop\\revengg-demo\\adapter.py', 'r') as f:
    content = f.read()

# Fix 2: get_edges() - add NULL AS expressions for edge columns
# After 'edge_columns = _columns(conn, "edges")' line, add NULL AS building code
# And update the SELECT query

# Step 1: Add NULL AS expression building after edge_columns
old_edge_cols = 'edge_columns = _columns(conn, "edges") if edges_exist else set()\n    has_build_columns'
new_edge_cols = 'edge_columns = _columns(conn, "edges") if edges_exist else set()\n\n        # Build NULL-AS expressions for columns that may be absent in legacy databases\n        confidence_edge = "e.confidence" if "confidence" in edge_columns else "NULL AS confidence"\n        evidence_quality_edge = "e.evidence_quality" if "evidence_quality" in edge_columns else "NULL AS evidence_quality"\n        relationship_source_edge = "e.relationship_source" if "relationship_source" in edge_columns else "NULL AS relationship_source"\n        has_build_columns'

if old_edge_cols not in content:
    print("Could not find edge_cols pattern 1", file=sys.stderr)
else:
    content = content.replace(old_edge_cols, new_edge_cols)
    print("Added NULL AS expression building after edge_columns", file=sys.stderr)

# Step 2: Update the SELECT query
old_select = '''        rows = conn.execute(
            """SELECT s.clause_reference AS src_ref, t.clause_reference AS dst_ref,
                      e.relationship_type AS rel, e.confidence, e.evidence_quality,
                      e.relationship_source
                 FROM edges e
                 JOIN nodes s ON s.node_id = e.source_node
                 JOIN nodes t ON t.node_id = e.target_node
                 WHERE %s
                 ORDER BY e.edge_id"""
            % " AND ".join(conditions),
            params,
        ).fetchall()'''

new_select = '''        rows = conn.execute(
            """SELECT s.clause_reference AS src_ref, t.clause_reference AS dst_ref,
                      e.relationship_type AS rel, %s, %s, %s
                 FROM edges e
                 JOIN nodes s ON s.node_id = e.source_node
                 JOIN nodes t ON t.node_id = e.target_node
                 WHERE %s
                 ORDER BY e.edge_id"""
            % (confidence_edge, evidence_quality_edge, relationship_source_edge,
               " AND ".join(conditions)),
            params,
        ).fetchall()'''

if old_select not in content:
    print("Could not find SELECT query pattern", file=sys.stderr)
else:
    content = content.replace(old_select, new_select)
    print("Fixed get_edges() SELECT query", file=sys.stderr)

with open('D:\\Desktop\\revengg-demo\\adapter.py', 'w') as f:
    f.write(content)

print("Done")