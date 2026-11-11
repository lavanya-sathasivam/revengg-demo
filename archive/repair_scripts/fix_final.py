#!/usr/bin/env python
"""Clean up adapter.py - remove duplicates and fix SELECT queries."""

with open('D:\\Desktop\\revengg-demo\\adapter.py', 'r') as f:
    content = f.read()

# ---- Remove duplicate NULL AS expressions (the second occurrence) ----
lines = content.split('\n')
new_lines = []
in_dup = False
dup_count = 0
for line in lines:
    if 'confidence_edge = "e.confidence"' in line and 'evidence_quality_edge' in line and 'relationship_source_edge' in line:
        dup_count += 1
        if dup_count == 1:
            new_lines.append(line)
        else:
            continue
    else:
        new_lines.append(line)

content = '\n'.join(new_lines)
print("Removed %d duplicate NULL AS block(s), keeping 1" % (dup_count - 1))

# ---- Fix the get_edges() SELECT column references ----
# Replace e.confidence, e.evidence_quality, relationship_source_edge
# with confidence_edge, evidence_quality_edge, relationship_source_edge
old_cols = 'e.confidence, e.evidence_quality, relationship_source_edge'
new_cols = 'confidence_edge, evidence_quality_edge, relationship_source_edge'

if old_cols in content:
    content = content.replace(old_cols, new_cols)
    print("Fixed get_edges() SELECT column references")
else:
    print("Could not find old column references")

# ---- Verify the SELECT and % formatting work together ----
# The SELECT string has the expression variables as literal text.
# The %s in the WHERE clause is replaced by " AND ".join(conditions).
# The confidence_edge etc. variables are already embedded in the SELECT
# string via the replace above, so the % formatting doesn't need changing.

# Verify the fix by checking the content
idx = content.find('rows = conn.execute')
if idx >= 0:
    # Print the execute block
    lines_subset = content[idx:idx+400]
    print("Execute block:")
    print(lines_subset)

with open('D:\\Desktop\\revengg-demo\\adapter.py', 'w') as f:
    f.write(content)

print("Done writing adapter.py")