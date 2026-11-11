#!/usr/bin/env python
"""Verification script for Phase 5B implementation."""
import sys
import os
import importlib.util
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

import impact
import adapter

TEST_MODULE_PATH = REPOSITORY_ROOT / "tests" / "test_phase7_hardening.py"
spec = importlib.util.spec_from_file_location(
    "phase7_hardening_for_verification", TEST_MODULE_PATH
)
if spec is None or spec.loader is None:
    raise ImportError(f"Unable to load {TEST_MODULE_PATH}")
phase7_tests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(phase7_tests)
CounterfactualEvidenceTests = phase7_tests.CounterfactualEvidenceTests
make_clause = phase7_tests.clause

def main():
    results = []
    
    # Create a test contract with various clause types
    # Test 1: Verify all 8 change types can be analyzed
    test = CounterfactualEvidenceTests()
    test.setUp()

    try:
        # Create a contract with payment, SLA, notice clauses.
        contract_id = test._make_contract([
            make_clause("1", "1. Payment Period. The Customer shall pay within 30 days.", "Payment Period", "payment"),
            make_clause("2", "2. Service Level. Monthly availability shall be at least 99%.", "Service Level", "sla"),
            make_clause("3", "3. Notices. Any notice is deemed served 7 days after despatch.", "Notices", "notice"),
        ])

        change_types = ['payment', 'sla', 'termination', 'notice', 'removal', 'replacement', 'fee', 'interest']

        results.append("=== CHANGE TYPE VERIFICATION ===")
        results.append(f"Contract created: {contract_id}")

        for ct in change_types:
            result = impact.analyze_change(contract_id, "1" if ct != "sla" else "2",
                                           30 if ct != "sla" else 99,
                                           15 if ct != "sla" else 98,
                                           change_type=ct)
            cf = result['counterfactual']
            ar = result['analysis_status']
            results.append(f"  {ct:12s}: analysis_status={ar:15s} counterfactual_status={cf['status']:15s} rows={len(cf['rows']):2d} notes={len(cf['notes']):2d}")

        # Test 2: Verify database immutability.
        results.append("\n=== DATABASE IMMUTABILITY ===")
        before_clauses = adapter.list_clauses(contract_id)
        before_count = len(before_clauses)

        for ct in ['payment', 'sla', 'notice']:
            for _ in range(3):
                impact.analyze_change(contract_id, "1" if ct != "sla" else "2",
                                      30 if ct != "sla" else 99,
                                      15 if ct != "sla" else 98,
                                      change_type=ct)

        after_clauses = adapter.list_clauses(contract_id)
        after_count = len(after_clauses)

        if before_count != after_count:
            raise AssertionError(
                f"Clause count changed during analysis: {before_count} -> {after_count}"
            )
        results.append(f"  Clause count preserved: {before_count} -> {after_count} [OK]")
    
        # Test 3: Verify notice test behavior.
        results.append("\n=== NOTICE TEST VERIFICATION ===")
        result = impact.analyze_change(contract_id, "3", 7, 5, change_type="notice")
        cf = result['counterfactual']
        ar = result['analysis_status']
        results.append(f"  Notice change_type analysis:")
        results.append(f"    analysis_status: {ar}")
        results.append(f"    counterfactual_status: {cf['status']}")
        results.append(f"    rows count: {len(cf['rows'])}")
        results.append(f"    notes count: {len(cf['notes'])}")
        if (
            ar != impact.ANALYSIS_STATUS_SUPPORTED
            or cf["status"] != impact.ANALYSIS_STATUS_PARTIAL
            or not cf["rows"]
        ):
            raise AssertionError("Notice analysis no longer produces supported partial rows")
        results.append("  [OK] Notice is modelled (SUPPORTED with partial rows+notes)")
    
        # Test 4: Verify fee and interest are descriptive.
        results.append("\n=== FEE AND INTEREST VERIFICATION ===")
        for ct in ['fee', 'interest']:
            result = impact.analyze_change(contract_id, "1", 30, 15, change_type=ct)
            cf = result['counterfactual']
            ar = result['analysis_status']
            results.append(f"  {ct:8s}: analysis_status={ar:15s} rows={len(cf['rows']):2d} notes={len(cf['notes']):2d}")
            if not cf["notes"]:
                raise AssertionError(f"{ct} analysis lost its explanatory notes")
            if cf["status"] not in {
                impact.ANALYSIS_STATUS_UNSUPPORTED,
                impact.ANALYSIS_STATUS_PARTIAL,
            }:
                raise AssertionError(
                    f"{ct} analysis has an unexpected status: {cf['status']}"
                )
            results.append(f"    Notes: {cf['notes'][0][:80]}...")
    
        # Test 5: Verify termination identifies evidence-backed consequences.
        results.append("\n=== TERMINATION VERIFICATION ===")
        result = impact.analyze_change(contract_id, "3", 7, 5, change_type="termination")
        cf = result['counterfactual']
        results.append(f"  termination: status={cf['status']} rows={len(cf['rows'])} notes={len(cf['notes'])}")
        if not cf['notes']:
            raise AssertionError("Termination analysis produced no evidence-backed notes")
        results.append(f"    First note: {cf['notes'][0][:80]}...")
    
        # Test 6: Verify removal/replacement identify dependent clauses.
        results.append("\n=== REMOVAL AND REPLACEMENT VERIFICATION ===")
        for ct in ['removal', 'replacement']:
            result = impact.analyze_change(contract_id, "1", 30, 15, change_type=ct)
            cf = result['counterfactual']
            results.append(f"  {ct:10s}: status={cf['status']} rows={len(cf['rows']):2d} notes={len(cf['notes']):2d}")
            if not cf['rows']:
                raise AssertionError(f"{ct} analysis produced no dependent-clause rows")
            for row in cf['rows'][:2]:
                results.append(f"    row: {row.get('metric', 'N/A')[:50]}")
    
        # Test 7: Run the existing Phase 7 and full test suites.
        results.append("\n=== TEST SUITE ===")
        import subprocess
        result = subprocess.run(
            [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', '*test_phase7*'],
            capture_output=True, text=True, cwd=str(REPOSITORY_ROOT)
        )
        if result.returncode:
            raise AssertionError(result.stderr or result.stdout)
        results.append("  Phase 7 tests: 0")
        result2 = subprocess.run(
            [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'],
            capture_output=True, text=True, cwd=str(REPOSITORY_ROOT)
        )
        if result2.returncode:
            raise AssertionError(result2.stderr or result2.stdout)
        results.append("  Full test suite: 0")
    finally:
        test.doCleanups()
    
    # Print results
    print("\n".join(results))
    
if __name__ == "__main__":
    main()