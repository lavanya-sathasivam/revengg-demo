#!/usr/bin/env python
import re

with open(r"D:\Desktop\revengg-demo\adapter.py", "r") as f:
    content = f.read()

# Find and replace the nodes INSERT
# Pattern matches the specific block we need to change
old_nodes = """        node_ids = []
        for node in normalized_nodes:
            inserted = cur.execute(
                """INSERT INTO nodes
                       (clause_reference, node_type, contract_id, detail, build_id)
                  VALUES (?, ?, ?, ?, ?)""",
                (
                    node["ref"],
                    node["node_type"],
                    contract_id,
                    node["detail"],
                    build_id,
                ),
            )
            node_ids.append(inserted.lastrowid)"""

new_nodes = """        node_ids = []
        for node in normalized_nodes:
            inserted = cur.execute(
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
            node_ids.append(inserted.lastrowid)"""

if old_nodes in content:
    content = content.replace(old_nodes, new_nodes)
    print("Replaced nodes INSERT")
else:
    print("Could not find nodes INSERT block")
    # Let's print what we have around line 520
    lines = content.split("\n")
    for i in range(518, 540):
        print(f"{i}: {lines[i]!r}")

# Find and replace the edges INSERT
old_edges = """        for edge in normalized_edges:
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

new_edges = """        for edge in normalized_edges:
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

if old_edges in content:
    content = content.replace(old_edges, new_edges)
    print("Replaced edges INSERT")
else:
    print("Could not find edges INSERT block")

with open(r"D:\Desktop\revengg-demo\adapter.py", "w") as f:
    f.write(content)

print("File written successfully")