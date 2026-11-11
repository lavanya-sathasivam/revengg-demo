#!/usr/bin/env python
"""Comprehensive fix for adapter.py legacy SQLite compatibility."""

with open('D:\\Desktop\\revengg-demo\\adapter.py', 'r') as f:
    content = f.read()

# ===== FIX 1: get_nodes() SELECT query =====
# The get_nodes() SELECT needs to use NULL AS expressions for missing columns.
# Current problem: SELECT uses plain column names (confidence, evidence_status)
# Fix: Replace the SELECT and its parameter substitution

old_nodes_select = 'SELECT node_id, clause_reference, node_type, contract_id, confidence, evidence_status,\n                      %s\n                 FROM nodes WHERE contract_id = ?%s ORDER BY node_id'
new_nodes_select = 'SELECT node_id, clause_reference, node_type, contract_id,\n                      %s, %s, %s\n                 FROM nodes WHERE contract_id = ?%s ORDER BY node_id'

if old_nodes_select in content:
    content = content.replace(old_nodes_select, new_nodes_select)
    print("Fixed get_nodes() SELECT query")
else:
    print("Could not find get_nodes() SELECT query (may already be fixed)")

old_nodes_params = '% (detail_expr, build_clause),'
new_nodes_params = '% (confidence_expr, evidence_status_expr, detail_expr, build_clause),'

if old_nodes_params in content:
    content = content.replace(old_nodes_params, new_nodes_params)
    print("Fixed get_nodes() params")
else:
    print("get_nodes() params may already be OK")

# ===== FIX 2: get_edges() - remove duplicate/incorrect NULL AS blocks =====
# Remove any duplicate NULL AS expression blocks from get_edges area
# Keep only one block at the right location

# Remove all NULL AS expression blocks that are in the wrong location
# (specifically, remove blocks that have the wrong indentation or context)

# Strategy: find all occurrences of the NULL AS pattern and keep only the one
# in the get_edges function (after edge_columns check)

# First, let's identify the positions of NULL AS expression blocks
import re
# Pattern: a comment followed by 3 confidence_edge lines
pattern = re.compile(r'# Build NULL-AS expressions.*?confidence_edge.*?relationship_source_edge', re.DOTALL)

# Find all matches
matches = list(pattern.finditer(content))
print("Found %d NULL AS expression blocks" % len(matches))

# Keep the last one (which should be in get_edges), remove others
# Actually, let me just remove all such blocks and add the correct one back
# in the right location.

# Remove all comment blocks that start with "# Build NULL-AS expressions"
# and contain confidence_edge definitions
lines = content.split('\n')
new_lines = []
skip_until = -1
for i, line in enumerate(lines):
    if '# Build NULL-AS expressions' in line:
        if skip_until == -1:
            # First occurrence - keep it
            skip_until = i + 1
            new_lines.append(line)
        else:
            # Subsequent occurrences - skip
            skip_until = i + 4  # Skip the next 3 lines too
            continue
    else:
        if skip_until > 0 and i <= skip_until:
            continue
        if skip_until > 0 and i == skip_until:
            skip_until = -1
            new_lines.append(line)
        else:
            new_lines.append(line)

content = '\n'.join(new_lines)
print("Cleaned up duplicate NULL AS blocks")

# ===== FIX 3: get_edges() - add proper NULL AS expressions and fix SELECT =====
# The get_edges function needs:
# 1. NULL AS expressions for confidence, evidence_quality, relationship_source
# 2. SELECT using those expressions
# 3. Proper % formatting

# Add NULL AS expressions right after edge_columns check
# Find the edge_columns line and add the expressions after it
edge_cols_marker = 'edge_columns = _columns(conn, "edges") if edges_exist else set()'
# Find this and add expressions after it

# Actually, the expressions should be added before the SELECT query,
# right before the rows = conn.execute( call.

# Let me find a good insertion point - after the build_id check for CURRENT state
# and before the rows = conn.execute( line.

# The expressions need to be defined before they're used in the SELECT.

# Let me just replace the entire get_edges SELECT + formatting block
# with the correct version.

# First, find the current SELECT block in get_edges
old_select_block = 'e.relationship_type AS rel, confidence_edge, evidence_quality_edge, relationship_source_edge\n               FROM edges e'
new_select_block = 'e.relationship_type AS rel, confidence_edge, evidence_quality_edge, relationship_source_edge\n               FROM edges e'

# Actually, the SELECT is already using the expressions. The issue is the % formatting.
# The current % formatting is: % " AND ".join(conditions),
# But we need to also pass the expression variables... wait, no.
# The expressions are embedded in the SELECT string as literal text.
# The %s in the WHERE clause is replaced by " AND ".join(conditions).
# The confidence_edge etc. are Python variables whose string values
# get substituted into the SQL string via the initial string replacement.

# Let me verify: the SELECT string has confidence_edge as literal text.
# When the SQL is executed, confidence_edge's value (e.g., "NULL AS confidence")
# is already in the SQL string. So the % formatting only needs to handle the WHERE clause.

# So the current structure should work if:
# 1. The expressions are defined (they are, at lines 1119-1122)
# 2. The SELECT references them (it does, at line 1125)
# 3. The % formatting handles the WHERE clause (it does, at line 1131)

# The issue might be the indentation. Let me check the current indentation.

# Actually, let me just check if the tests pass now and fix specific issues.
print("Checking current state...")

# Verify the expressions are defined in get_edges
if 'confidence_edge = "e.confidence"' in content:
    print("confidence_edge expression found in content")
else:
    print("WARNING: confidence_edge expression MISSING from content")

if 'evidence_quality_edge = "e.evidence_quality"' in content:
    print("evidence_quality_edge expression found in content")
else:
    print("WARNING: evidence_quality_edge expression MISSING from content")

if 'relationship_source_edge = "e.relationship_source"' in content:
    print("relationship_source_edge expression found in content")
else:
    print("WARNING: relationship_source_edge expression MISSING from content")

# Check the SELECT references the expressions
if 'confidence_edge, evidence_quality_edge, relationship_source_edge' in content:
    print("SELECT references expression variables")
else:
    print("WARNING: SELECT does not reference expression variables")

# Check for the % formatting
if '% " AND ".join(conditions),' in content:
    print("% formatting for WHERE clause found")
else:
    print("WARNING: % formatting for WHERE clause MISSING")

with open('D:\\Desktop\\revengg-demo\\adapter.py', 'w') as f:
    f.write(content)

print("Done writing adapter.py")