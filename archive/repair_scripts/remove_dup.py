#!/usr/bin/env python
with open('D:\\Desktop\\revengg-demo\\adapter.py', 'r') as f:
    content = f.read()

lines = content.split('\n')

# Remove the duplicate NULL AS block (the second occurrence at lines 1132-1135)
# Keep the first occurrence (lines 1127-1130)
new_lines = []
dup_count = 0
for line in lines:
    # Check if this line starts the NULL AS expression block
    if line.strip().startswith('confidence_edge') and 'e.confidence' in line:
        dup_count += 1
        if dup_count <= 1:
            new_lines.append(line)
        else:
            # Skip this and subsequent lines until the block ends
            # The block is 4 lines: confidence, evidence_quality, relationship_source, blank
            continue
    else:
        new_lines.append(line)

# However, the above might not work perfectly. Let me check what we have.
print("dup_count was:", dup_count)

# Let me just specifically remove lines that contain the duplicate block
# The duplicate block starts with 'confidence_edge = "e.confidence"' on its own
new_lines2 = []
in_dup = False
for line in lines:
    if 'confidence_edge = "e.confidence"' in line and 'evidence_quality_edge' in line and 'relationship_source_edge' in line:
        if not in_dup:
            # First occurrence - keep
            in_dup = True
            new_lines2.append(line)
        else:
            # Second occurrence - skip
            continue
    else:
        new_lines2.append(line)

content = '\n'.join(new_lines2)
print("Kept %d duplicate blocks (should be 1)" % (1 if new_lines2 else 0))

# Now verify the content looks right
with open('D:\\Desktop\\revengg-demo\\adapter.py', 'r') as f:
    check = f.read()
 dup_check = check.count('confidence_edge = "e.confidence"')
print("Now has %d occurrences of confidence_edge expression" % dup_check)

with open('D:\\Desktop\\revengg-demo\\adapter.py', 'w') as f:
    f.write(content)
print("Done")