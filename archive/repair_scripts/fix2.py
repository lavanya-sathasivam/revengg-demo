#!/usr/bin/env python
import sys

with open('D:\\Desktop\\revengg-demo\\adapter.py', 'r') as f:
    content = f.read()

# Fix 2: get_edges() SELECT query
old_edges = 'SELECT s.clause_reference AS src_ref, t.clause_reference AS dst_ref,\n                      e.relationship_type AS rel, e.confidence, e.evidence_quality, e.relationship_source\n               FROM edges e\n               JOIN nodes s ON s.node_id = e.source_node\n               JOIN nodes t ON t.node_id'

new_edges = 'SELECT s.clause_reference AS src_ref, t.clause_reference AS dst_ref,\n                      e.relationship_type AS rel, %s, %s, %s\n               FROM edges e\n               JOIN nodes s ON s.node_id = e.source_node\n               JOIN nodes t ON t.node_id'

# Build the NULL AS expressions for edge columns
# We need to add code before the SELECT that creates these expressions
# based on edge_columns checking

# First, let me find where edge_columns is obtained
idx = content.find('edge_columns = _columns(conn, \"edges\")')
if idx >= 0:
    print("Found edge_columns at index", idx, file=sys.stderr)
    # Check what comes after
    print(repr(content[idx:idx+100]), file=sys.stderr)
else:
    print("Could not find edge_columns", file=sys.stderr)