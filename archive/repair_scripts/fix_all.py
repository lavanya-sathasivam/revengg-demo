#!/usr/bin/env python
"""Fix adapter.py for legacy SQLite compatibility - get_nodes and get_edges NULL AS."""

with open('D:\\Desktop\\revengg-demo\\adapter.py', 'r') as f:
    content = f.read()

# ---- Fix 1: get_nodes() ----
# Replace the SELECT query to use NULL AS expressions for missing columns
old_nodes_select = 'SELECT node_id, clause_reference, node_type, contract_id, confidence, evidence_status,\n                      %s\n                 FROM nodes WHERE contract_id = ?%s ORDER BY node_id'
new_nodes_select = 'SELECT node_id, clause_reference, node_type, contract_id,\n                      %s, %s, %s\n                 FROM nodes WHERE contract_id = ?%s ORDER BY node_id'

# The %s substitutions change from (detail_expr, build_clause) to 
# (confidence_expr, evidence_status_expr, detail_expr, build_clause)
old_nodes_params = '% (detail_expr, build_clause),'
new_nodes_params = '% (confidence_expr, evidence_status_expr, detail_expr, build_clause),'

if old_nodes_select in content:
    content = content.replace(old_nodes_select, new_nodes_select)
    print("Fixed get_nodes() SELECT query")
else:
    print("Could not find get_nodes() SELECT query")

if old_nodes_params in content:
    content = content.replace(old_nodes_params, new_nodes_params)
    print("Fixed get_nodes() params")
else:
    print("Could not find get_nodes() params")

# ---- Fix 2: get_edges() ----
# Add NULL AS expression definitions for edge columns
# Insert before the rows = conn.execute(...) line

expr_to_add = '''\n        # Build NULL-AS expressions for columns that may be absent in legacy databases\n        confidence_edge = "e.confidence" if "confidence" in edge_columns else "NULL AS confidence"\n        evidence_quality_edge = "e.evidence_quality" if "evidence_quality" in edge_columns else "NULL AS evidence_quality"\n        relationship_source_edge = "e.relationship_source" if "relationship_source" in edge_columns else "NULL AS relationship_source"'''

# Find the position to insert the expressions - right before "rows = conn.execute"
insert_pos = content.find('        rows = conn.execute(')
if insert_pos >= 0:
    # Insert the expressions before this line
    lines = content.split('\n')
    # Find the exact line number
    actual_idx = None
    for i, line in enumerate(lines):
        if '        rows = conn.execute(' in line and i > 1100:
            actual_idx = i
            break
    if actual_idx is not None:
        lines.insert(actual_idx, expr_to_add.rstrip())
        content = '\n'.join(lines)
        print("Added NULL AS expressions for edge columns")
    else:
        print("Could not find insert position for expressions")
else:
    print("Could not find rows = conn.execute position")

# Now replace the SELECT query in get_edges()
# Build the old and new SELECT blocks
# First, let me find the exact SELECT string
old_select = 'SELECT s.clause_reference AS src_ref, t.clause_reference AS dst_ref,\n                      e.relationship_type AS rel, e.confidence, e.evidence_quality,\n                      e.relationship_source\n               FROM edges e\n               JOIN nodes s ON s.node_id = e.source_node\n               JOIN nodes t ON t.node_id = e.target_node\n               WHERE %s\n            '

# Actually, let me find the exact block to replace
old_block_start = 'e.relationship_source\n               FROM edges e\n               JOIN nodes s ON s.node_id = e.source_node'
new_block_start = 'relationship_source_edge\n               FROM edges e\n               JOIN nodes s ON s.node_id = e.source_node'

if old_block_start in content:
    content = content.replace(old_block_start, new_block_start)
    print("Fixed get_edges() SELECT column reference")
else:
    print("Could not find get_edges() SELECT block start")

# Now replace the % formatting line
old_param_line = '            % " AND ".join(conditions),'
new_param_line = '            % (confidence_edge, evidence_quality_edge, relationship_source_edge,\n               " AND ".join(conditions)),'

# Hmm, the formatting is different. Let me check the actual content
idx = content.find('            % " AND ".join(conditions),')
if idx >= 0:
    print("Found param line at", idx)
    print(repr(content[idx:idx+100]))
else:
    print("Could not find param line")

# Let me just replace the whole execute block
old_execute = '''        rows = conn.execute(
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

new_execute = '''        rows = conn.execute(
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

if old_execute in content:
    content = content.replace(old_execute, new_execute)
    print("Fixed get_edges() SELECT query")
else:
    print("Could not find execute block to replace")
    # Debug: find the execute block
    idx = content.find('rows = conn.execute(')
    if idx >= 0:
        print("Found execute at", idx)
        print(repr(content[idx:idx+300]))

with open('D:\\Desktop\\revengg-demo\\adapter.py', 'w') as f:
    f.write(content)

print("Done writing adapter.py")