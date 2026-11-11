import json
import os
import sys

# Set up demo environment
os.environ["LLM_PROVIDER"] = "mock"

sys.path.insert(0, '.')

# Test get_graph with proper setup
from unittest.mock import patch, MagicMock
from fastapi import Request

# Import after env is set
from api_b import get_graph
from auth import DEMO_PRINCIPAL

# Create a mock request with proper principal
req = MagicMock(spec=Request)
req.state.principal = DEMO_PRINCIPAL

try:
    p = get_graph(1, request=req)
    print('Keys:', list(p.keys()))
    print('Counts:', p['counts'])
    print('Number of nodes:', len(p['nodes']))
    print('Number of edges:', len(p['edges']))
    print('First 3 nodes:', json.dumps(p['nodes'][:3], indent=2))
    print('First 3 edges:', json.dumps(p['edges'][:3], indent=2))
except Exception as e:
    print('Error:', type(e).__name__, e)
    import traceback
    traceback.print_exc()