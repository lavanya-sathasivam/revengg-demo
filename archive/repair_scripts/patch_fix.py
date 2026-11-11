#!/usr/bin/env python
import sys

# Read the file
with open(r"D:\Desktop\revengg-demo\adapter.py", "r") as f:
    lines = f.readlines()

# Find the nodes INSERT block (lines 522-534, 0-indexed 521-533)
# And the edges INSERT block (lines 536-549, 0-indexed 535-548)

# We'll rebuild the lines list with the new content

new_lines = []
i = 0
while i < len(lines):
    line = lines[i]
    
    # Check if this is the start of the nodes INSERT block
    if i >= 520 and i <= 534 and 'inserted = cur.execute' in line and 'INSERT INTO nodes' in lines[i+1]:
        # Replace the entire block (lines 522-534)
        # New nodes INSERT
        new_lines.append('            inserted = cur.execute(\n')
        new_lines.append('                """INSERT INTO nodes\n')
        new_lines.append('                       (clause_reference, node_type, contract_id, detail, build_id,\n')
        new_lines.append('                        confidence, evidence_status)\n')
        new_lines.append('                  VALUES (?, ?, ?, ?, ?, ?, ?)\"\"\",\n')
        new_lines.append('                (\n')
        new_lines.append('                    node["ref"],\n')
        new_lines.append('                    node["node_type"],\n')
        new_lines.append('                    contract_id,\n')
        new_lines.append('                    node["detail"],\n')
        new_lines.append('                    build_id,\n')
        new_lines.append('                    node.get("confidence", 1.0),\n')
        new_lines.append('                    node.get("evidence_status", "verified"),\n')
        new_lines.append('                ),\n')
        new_lines.append('            )\n')
        new_lines.append('            node_ids.append(inserted.lastrowid)\n')
        # Skip the old block (lines 522-534 = 13 lines)
        old_block_end = 534
        i = old_block_end + 1
        continue
    
    # Check if this is the start of the edges INSERT block (lines 536-549)
    if i >= 535 and i <= 548 and 'for edge in normalized_edges' in line and 'INSERT INTO edges' in lines[i+1]:
        # Replace the entire block (lines 536-549)
        new_lines.append('        for edge in normalized_edges:\n')
        new_lines.append('            cur.execute(\n')
        new_lines.append('                """INSERT INTO edges\n')
        new_lines.append('                       (source_node, target_node, relationship_type,\n')
        new_lines.append('                        contract_id, build_id, confidence, evidence_quality,\n')
        new_lines.append('                        relationship_source)\n')
        new_lines.append('                  VALUES (?, ?, ?, ?, ?, ?, ?, ?)\"\"\",\n')
        new_lines.append('                (\n')
        new_lines.append('                    node_ids[edge["source"]],\n')
        new_lines.append('                    node_ids[edge["target"]],\n')
        new_lines.append('                    edge["relationship_type"],\n')
        new_lines.append('                    contract_id,\n')
        new_lines.append('                    build_id,\n')
        new_lines.append('                    edge.get("confidence", 1.0),\n')
        new_lines.append('                    edge.get("evidence_quality", "explicit"),\n')
        new_lines.append('                    edge.get("relationship_source", "rule_table"),\n')
        new_lines.append('                ),\n')
        new_lines.append('            )\n')
        # Skip the old block (lines 536-549 = 14 lines)
        old_block_end = 548
        i = old_block_end + 1
        continue
    
    new_lines.append(line)
    i += 1

# Write back
with open(r"D:\Desktop\revengg-demo\adapter.py", "w") as f:
    f.writelines(new_lines)

print("Done patching adapter.py")