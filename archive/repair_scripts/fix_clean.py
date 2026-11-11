#!/usr/bin/env python
"""Cleanup and fix adapter.py for legacy SQLite compatibility."""

with open('D:\\Desktop\\revengg-demo\\adapter.py', 'r') as f:
    content = f.read()

# ---- Remove duplicate NULL AS expressions (lines 1132-1135) ----
# Find and remove the duplicate block
dup_start = "\n        # Build NULL-AS expressions for columns that may be absent in legacy databases\n        confidence_edge = \"e.confidence\" if \"confidence\" in edge_columns else \"NULL AS confidence\"\n        evidence_quality_edge = \"e.evidence_quality\" if \"evidence_quality\" in edge_columns else \"NULL AS evidence_quality\"\n        relationship_source_edge = \"e.relationship_source\" if \"relationship_source\" in edge_columns else \"NULL AS relationship_source\""
# There are two occurrences - keep the first, remove the second
# Let me just remove the second occurrence
lines = content.split('\n')
# Find lines with the duplicate
new_lines = []
skip_dup = False
skip_dup_count = 0
for line in lines:
    if 'confidence_edge = "e.confidence"' in line and 'evidence_quality_edge' in line and 'relationship_source_edge' in line:
        if skip_dup:
            # This is the second occurrence, skip it
            skip_dup_count += 1
            continue
        else:
            skip_dup = True
    new_lines.append(line)

content = '\n'.join(new_lines)
print(f"Removed {skip_dup_count} duplicate NULL AS expression block(s)")

# ---- Now fix the get_edges() SELECT query ----
# The current SELECT uses raw column names. I need to change it to use the
# NULL AS expressions and update the % formatting.

# Current SELECT (approximately, need to find exact):
# """SELECT s.clause_reference AS src_ref, t.clause_reference AS dst_ref,
#       e.relationship_type AS rel, e.confidence, e.evidence_quality, relationship_source
#    FROM edges e
#    JOIN nodes s ON s.node_id = e.source_node
#    JOIN nodes t ON t.node_id = e.target_node
#    WHERE %s
#    ORDER BY e.edge_id"""
# % " AND ".join(conditions),

# New SELECT should use the expressions and have proper %s placeholders:
# """SELECT s.clause_reference AS src_ref, t.clause_reference AS dst_ref,
#       e.relationship_type AS rel, %s, %s, %s
#    FROM edges e
#    JOIN nodes s ON s.node_id = e.source_node
#    JOIN nodes t ON t.node_id = e.target_node
#    WHERE %s
#    ORDER BY e.edge_id"""
# % (confidence_edge, evidence_quality_edge, relationship_source_edge,
#    " AND ".join(conditions)),

# Find the old SELECT block and replace it
old_select_block = '''        """SELECT s.clause_reference AS src_ref, t.clause_reference AS dst_ref,
                      e.relationship_type AS rel, e.confidence, e.evidence_quality, relationship_source
                 FROM edges e
                 JOIN nodes s ON s.node_id = e.source_node
                 JOIN nodes t ON t.node_id = e.target_node
                 WHERE %s
                 ORDER BY e.edge_id"""
            % " AND ".join(conditions),'''

new_select_block = '''        """SELECT s.clause_reference AS src_ref, t.clause_reference AS dst_ref,
                      e.relationship_type AS rel, %s, %s, %s
                 FROM edges e
                 JOIN nodes s ON s.node_id = e.source_node
                 JOIN nodes t ON t.node_id = e.target_node
                 WHERE %s
                 ORDER BY e.edge_id"""
            % (confidence_edge, evidence_quality_edge, relationship_source_edge,
               " AND ".join(conditions)),'''

if old_select_block in content:
    content = content.replace(old_select_block, new_select_block)
    print("Fixed get_edges() SELECT query")
else:
    print("Could not find get_edges() SELECT block to replace")
    # Debug: find what's there
    idx = content.find('SELECT s.clause_reference AS src_ref')
    if idx >= 0:
        print("Found SELECT at", idx)
        print(repr(content[idx:idx+250]))

# ---- Fix the get_nodes() SELECT params ----
# The get_nodes() was already fixed by fix1.py, but let me verify and fix the params
old_nodes_params = '% (confidence_expr, evidence_status_expr, detail_expr, build_clause),'
# Check if it's already correct
idx = content.find('confidence_expr, evidence_status_expr')
if idx >= 0:
    print("get_nodes() params already fixed")
else:
    print("get_nodes() params need fixing")
    # Try to find the old params
    old = '% (detail_expr, build_clause),'
    new = '% (confidence_expr, evidence_status_expr, detail_expr, build_clause),'
    if old in content:
        content = content.replace(old, new)
        print("Fixed get_nodes() params")

with open('D:\\Desktop\\revengg-demo\\adapter.py', 'w') as f:
    f.write(content)

print("Done writing adapter.py")