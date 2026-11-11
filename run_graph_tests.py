"""Run graph tests using unittest"""
import sys
import os

# Add the root to path
sys.path.insert(0, r'D:\Desktop\revengg-demo')

import unittest
from tests.test_graph import *

# Run specific test classes
loader = unittest.TestLoader()
suite = unittest.TestSuite()

# Add test classes
suite.addTests(loader.loadTestsFromTestCase(TestSegmentation))
suite.addTests(loader.loadTestsFromTestCase(TestClassification))
suite.addTests(loader.loadTestsFromTestCase(TestRelationshipModel))
suite.addTests(loader.loadTestsFromTestCase(TestImpact))
suite.addTests(loader.loadTestsFromTestCase(TestConflictAndCrossContract))
suite.addTests(loader.loadTestsFromTestCase(TestApiSurface))
suite.addTests(loader.loadTestsFromTestCase(TestRobustness))

runner = unittest.TextTestRunner(verbosity=2)
result = runner.run(suite)

# Exit with appropriate code
sys.exit(0 if result.wasSuccessful() else 1)