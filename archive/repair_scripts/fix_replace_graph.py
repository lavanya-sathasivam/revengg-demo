#!/usr/bin/env python
"""Fix the replace_graph INSERT statements in adapter.py."""
import sys

with open(r"D:\Desktop\revengg-demo\adapter.py", "r") as f:
    lines = f.readlines()

# Find and replace the nodes INSERT (lines around 520-534)
# We need to find the exact pattern and replace it

# Let's find the line numbers
for i, line in enumerate(lines):
    if 'INSERT INTO nodes' in line and 'clause_reference' in line and 'build_id' in line:
        print(f"Nodes INSERT at line {i+1}: {line.strip()[:80]}")
    if 'INSERT INTO edges' in line and 'source_node' in line and 'build_id' in line and 'relationship_type' in line:
        print(f"Edges INSERT at line {i+1}: {line.strip()[:80]}")

# Now let's do the replacement using string operations on the full content
with open(r"D:\Desktop\revengg-demo\adapter.py", "r") as f:
    content = f.read()

# Replace nodes INSERT
old_nodes = '''            inserted = cur.execute(
                """INSERT INTO nodes
                       (clause_reference, node_type, contract_id, detail, build_id,
                        confidence, evidence_status)
                  VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    node["ref"],
                    node["node_type"],
                    contract_id,
                    node["detail"],
                    build_id,
                    node.get("confidence", 1.0),
                    node.get("evidence_status", "verified"),
                ),
            )
            node_ids.append(inserted.lastrowid)'''

# Actually, let me just add the new columns to the existing INSERT
# The current INSERT has: (clause_reference, node_type, contract_id, detail, build_id)
# We need to add: confidence, evidence_status

# Let me find the exact current state
print("\nCurrent nodes INSERT in replace_graph:")
# Find the pattern
import re
match = re.search(
    r'inserted = cur\.execute\(.*?INSERT INTO nodes.*?\)',
    content,
    re.DOTALL,
)
if match:
    print(match.group()[:300])

# Find and replace edges INSERT
print("\nCurrent edges INSERT in replace_graph:")
match2 = re.search(
    r'for edge in normalized_edges:.*?INSERT INTO edges.*?\)',
    content,
    re.DOTALL,
)
if match2:
    print(match2.group()[:400])