"""Run Phase 5 integrity tests using unittest"""
import sys
import os

sys.path.insert(0, r'D:\Desktop\revengg-demo')

import unittest
from tests.test_phase5_integrity import *

loader = unittest.TestLoader()
suite = unittest.TestSuite()

# Add test classes
suite.addTests(loader.loadTestsFromTestCase(LegacyDatabaseTests))
suite.addTests(loader.loadTestsFromTestCase(VersionAndProvenanceTests))
suite.addTests(loader.loadTestsFromTestCase(AtomicGraphTests))
suite.addTests(loader.loadTestsFromTestCase(AuditTrailTests))
suite.addTests(loader.loadTestsFromTestCase(UploadAndAuthorizationAuditTests))
suite.addTests(loader.loadTestsFromTestCase(SeedResetTests))

runner = unittest.TextTestRunner(verbosity=1)
result = runner.run(suite)

print('\n===')
print('Tests run:', result.testsRun)
print('Failures:', len(result.failures))
print('Errors:', len(result.errors))
for f in result.failures:
    print('FAILURE:', f[0], ':', f[1][:200])
for e in result.errors:
    print('ERROR:', e[0], ':', str(e[1])[:200])

sys.exit(0 if result.wasSuccessful() else 1)