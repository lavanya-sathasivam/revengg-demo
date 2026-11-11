#!/usr/bin/env python
import sys

with open('D:\\Desktop\\revengg-demo\\adapter.py', 'r') as f:
    content = f.read()

# Fix 2: get_edges() - add NULL AS expressions for edge columns before SELECT
# And update the SELECT query

# Add NULL AS expression building right before the SELECT query
old_select_start = '''        rows = conn.execute(
            """SELECT s.clause_reference AS src_ref, t.clause_reference AS dst_ref,
                      e.relationship_type AS rel, e.confidence, e.evidence_quality,
                      e.relationship_source'''

new_select_start = '''        rows = conn.execute(
            """SELECT s.clause_reference AS src_ref, t.clause_reference AS dst_ref,
                      e.relationship_type AS rel, %s, %s, %s
                 FROM edges e
                 JOIN nodes s ON s.node_id = e.source_node
                 JOIN nodes t ON t.node_id = e.target_node
                 WHERE %s
                 ORDER BY e.edge_id"""
            % (confidence_edge, evidence_quality_edge, relationship_source_edge,
               " AND ".join(conditions)),'''

# But I also need to add the expression definitions. Let me add them right before rows = conn.execute
# Find the position just before the SELECT and add the expressions there

# Actually, let me just replace the whole SELECT + the % formatting block
old_block = '''        rows = conn.execute(
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

new_block = '''        rows = conn.execute(
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

if old_block in content:
    content = content.replace(old_block, new_block)
    print("Fixed get_edges() SELECT query", file=sys.stderr)
else:
    print("Could not find SELECT block to replace", file=sys.stderr)
    # Print what we're looking for
    idx = content.find('SELECT s.clause_reference AS src_ref')
    if idx >= 0:
        print(repr(content[idx:idx+300]), file=sys.stderr)

# Now add the NULL AS expression definitions - add them right before the rows = conn.execute line
# Find where to insert the expression definitions
expr_to_add = '''\n        # Build NULL-AS expressions for columns that may be absent in legacy databases\n        confidence_edge = "e.confidence" if "confidence" in edge_columns else "NULL AS confidence"\n        evidence_quality_edge = "e.evidence_quality" if "evidence_quality" in edge_columns else "NULL AS evidence_quality"\n        relationship_source_edge = "e.relationship_source" if "relationship_source" in edge_columns else "NULL AS relationship_source"'''

# Insert the expressions before the rows = conn.execute line
insert_pos = content.find('        rows = conn.execute(')
if insert_pos >= 0:
    # Find the line and add expressions before it
    lines = content.split('\n')
    # Find the index of the line with 'rows = conn.execute('
    for i, line in enumerate(lines):
        if 'rows = conn.execute(' in line and i > 1100:  # rough check
            # Insert before this line
            lines.insert(i, expr_to_add.rstrip())
            break
    content = '\n'.join(lines)
    # Re-apply the SELECT fix if it was already done
    # Actually, the expressions need to be there for the SELECT to reference them
    # Let me check if the SELECT replacement already happened
    print("Insert position:", insert_pos, file=sys.stderr)
else:
    print("Could not find insert position", file=sys.stderr)

with open('D:\\Desktop\\revengg-demo\\adapter.py', 'w') as f:
    f.write(content)

print("Done")