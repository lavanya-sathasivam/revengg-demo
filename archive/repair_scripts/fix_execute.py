#!/usr/bin/env python
with open('D:\\Desktop\\revengg-demo\\adapter.py', 'r') as f:
    content = f.read()

# Find the broken section and fix it
# Look for the SELECT with 12-space indent and missing conn.execute(
old_select_start = '            """SELECT s.clause_reference AS src_ref, t.clause_reference AS dst_ref,'
if old_select_start in content:
    # Replace the SELECT line and add the conn.execute( wrapper
    # The SELECT is currently at 12 spaces; we need to add rows = conn.execute(\n            before it
    # and adjust the closing
    
    # First, let me find the full block to replace
    # The block goes from the SELECT string through the % formatting to .fetchall()
    
    # Find where .fetchall() is for this block
    idx1 = content.find('            """SELECT s.clause_reference AS src_ref, t.clause_reference AS dst_ref,')
    idx2 = content.find('.fetchall()', idx1)
    idx3 = content.find('            params,', idx1)
    
    if idx1 >= 0 and idx2 >= 0 and idx3 >= 0:
        # Extract the block
        block = content[idx1:idx2 + len('.fetchall()')]
        print("Found block at", idx1, "to", idx2)
        print("Block:", repr(block[:200]))
    else:
        print("Could not find block boundaries")
        # Just try a simpler approach
else:
    print("Could not find SELECT start")