#!/usr/bin/env python
import re

with open(r"D:\Desktop\revengg-demo\adapter.py", "r") as f:
    content = f.read()

# The exact old edges INSERT block (from lines 538-551)
old_edges_block = """        for edge in normalized_edges:
            cur.execute(
                """INSERT INTO edges
                       (source_node, target_node, relationship_type,
                        contract_id, build_id)
                  VALUES (?, ?, ?, ?, ?)""",
                (
                    node_ids[edge["source"]],
                    node_ids[edge["target"]],
                    edge["relationship_type"],
                    contract_id,
                    build_id,
                ),
            )"""

# The new edges INSERT block
new_edges_block = """        for edge in normalized_edges:
            cur.execute(
                """INSERT INTO edges
                       (source_node, target_node, relationship_type,
                        contract_id, build_id, confidence, evidence_quality,
                        relationship_source)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    node_ids[edge["source"]],
                    node_ids[edge["target"]],
                    edge["relationship_type"],
                    contract_id,
                    build_id,
                    edge.get("confidence", 1.0),
                    edge.get("evidence_quality", "explicit"),
                    edge.get("relationship_source", "rule_table"),
                ),
            )"""

if old_edges_block in content:
    content = content.replace(old_edges_block, new_edges_block)
    print("Replaced edges INSERT block successfully")
else:
    print("Could not find edges INSERT block")
    # Debug: find for edge in normalized_edges
    idx = content.find("for edge in normalized_edges")
    if idx != -1:
        print(f"Found at index {idx}")
        print(repr(content[idx:idx+300]))

with open(r"D:\Desktop\revengg-demo\adapter.py", "w") as f:
    f.write(content)

print("Done")