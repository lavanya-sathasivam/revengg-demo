"""Script to make Phase 5A changes to adapter.py"""
import sys

with open(r"D:\Desktop\revengg-demo\adapter.py", "r") as f:
    content = f.read()

# --- Change 1: Update _GRAPH_MIGRATIONS (already done, verify) ---
# The _GRAPH_MIGRATIONS already has the new columns from earlier

# --- Change 2: Update nodes INSERT in replace_graph ---
# Find the old block and replace it
old_nodes_insert = """        node_ids = []
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

new_nodes_insert = """        node_ids = []
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

if old_nodes_insert in content:
    content = content.replace(old_nodes_insert, new_nodes_insert)
    print("OK: Replaced nodes INSERT in replace_graph")
else:
    print("WARN: Could not find nodes INSERT block for replacement")
    # Debug: print surrounding lines
    idx = content.find("node_ids = []")
    if idx != -1:
        print(f"Found at index {idx}")
        print(repr(content[idx:idx+500]))

# --- Change 3: Update edges INSERT in replace_graph ---
old_edges_insert = """        for edge in normalized_edges:
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

new_edges_insert = """        for edge in normalized_edges:
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

if old_edges_insert in content:
    content = content.replace(old_edges_insert, new_edges_insert)
    print("OK: Replaced edges INSERT in replace_graph")
else:
    print("WARN: Could not find edges INSERT block for replacement")
    # Debug
    idx = content.find("for edge in normalized_edges:")
    if idx != -1:
        print(f"Found at index {idx}")
        print(repr(content[idx:idx+500]))

# --- Change 4: Update compute_graph_hash to include new fields ---
# The hash should include confidence and evidence_quality if present,
# but for backward compatibility, we include them only when they have
# non-default values. Actually, let me just always include them since
# the hash is computed on the in-memory data structure.

# Actually, looking at the existing hash, it hashes node ref, node_type, detail
# and edge source key, target key, relationship_type. The new fields are additional
# content info, so they should be included for correctness.

# Let me check the current compute_graph_hash function
old_hash_start = """    node_entries = sorted(
        (
            str(node.get("ref") or ""),
            str(node.get("node_type") or ""),
            str(node.get("detail") or ""),
        )
        for node in nodes
    )"""
new_hash_start = """    node_entries = sorted(
        (
            str(node.get("ref") or ""),
            str(node.get("node_type") or ""),
            str(node.get("detail") or ""),
            str(node.get("confidence", 1.0) or ""),
            str(node.get("evidence_status") or ""),
        )
        for node in nodes
    )"""

if old_hash_start in content:
    content = content.replace(old_hash_start, new_hash_start)
    print("OK: Updated node hash start")
else:
    print("WARN: Could not find node hash start block")

# Hmm, actually let me not change the hash for now. The new fields on nodes
# are secondary. Let me focus on edges.

# --- Change 5: Update _validate_graph_input to handle new fields ---
# The validation function checks that edges have valid relationship_type
# and source/target indices. I need to also allow the new fields.

# Actually, _validate_graph_input normalizes the input. Let me check if it
# needs updating. It currently checks:
# - node ref and node_type exist
# - source/target are valid indices
# - relationship_type is in VALID_RELATIONSHIPS
# 
# The new fields (confidence, evidence_quality, relationship_source) are
# optional and don't need validation changes.

# --- Change 6: Update get_nodes and get_edges ---
# These functions already return dicts from SQLite rows. Since the new
# columns are added via ALTER TABLE, the dicts will include the new keys.
# The existing tests should still work because they access specific keys.

# But I need to make sure the new columns have default values so they don't
# break anything. Since the migration uses ALTER TABLE ADD COLUMN without
# NOT NULL, the columns will be NULL by default, which is fine.

# --- Change 7: Update build_graph in graph.py to set confidence/evidence ---
# This is the key change. When building the graph, edges created from
# the rule table should be marked as "explicit"/"rule_table", while LLM-filled
# edges should be marked as "inferred"/"llm".

# Let me check the build_graph function for where edges are created.

print("Script completed its checks")