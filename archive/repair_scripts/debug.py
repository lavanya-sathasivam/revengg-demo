#!/usr/bin/env python
with open('D:\\Desktop\\revengg-demo\\adapter.py', 'r') as f:
    content = f.read()

# Find the SELECT and the % formatting block
idx = content.find('ORDER BY e.edge_id' + chr(34)*4 + chr(10) + '            % \" AND \"')
# Let me just search for a unique string
idx = content.find('e.relationship_source\n               FROM edges e')
if idx >= 0:
    print('Found at', idx)
    print(repr(content[idx:idx+200]))
else:
    print('Not found')