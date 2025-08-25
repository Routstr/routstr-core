#!/usr/bin/env python3
"""
Performance Testing Runner

This script runs performance tests and generates a detailed report.
Usage: python tests/integration/run_performance_tests.py
"""

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


async def run_performance_suite() -> bool:
    """Run the complete performance test suite"""
    print("=" * 80)
    print("ROUTSTR PROXY - PERFORMANCE TEST SUITE")
    print("=" * 80)
    print(f"Started at: {datetime.now().isoformat()}")
    print()

    # Performance test commands
    test_suites = [
        {
            "name": "Baseline Performance Metrics",
            "cmd": "pytest tests/integration/test_performance_load.py::TestPerformanceBaseline -v -s",
        },
        {
            "name": "Load Testing - 100 Concurrent Users",
            "cmd": "pytest tests/integration/test_performance_load.py::TestLoadScenarios::test_concurrent_users_100 -v -s",
        },
        {
            "name": "Sustained Load - 1000 RPM",
            "cmd": "pytest tests/integration/test_performance_load.py::TestLoadScenarios::test_sustained_load_1000_rpm -v -s",
        },
        {
            "name": "Memory Leak Detection",
            "cmd": "pytest tests/integration/test_performance_load.py::TestMemoryLeaks -v -s",
        },
        {
            "name": "Performance Regression Tests",
            "cmd": "pytest tests/integration/test_performance_load.py::TestPerformanceRegression -v -s",
        },
    ]

    results: List[Dict[str, Any]] = []

    for suite in test_suites:
        print(f"\n{'=' * 60}")
        print(f"Running: {suite['name']}")
        print(f"{'=' * 60}")

        start_time = datetime.now()

        # Run the test
        proc = await asyncio.create_subprocess_shell(
            suite["cmd"], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await proc.communicate()

        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        result = {
            "name": suite["name"],
            "success": proc.returncode == 0,
            "duration": duration,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
        }

        if proc.returncode == 0:
            print(f"PASSED: {suite['name']} ({duration:.2f}s)")
        else:
            print(f"FAILED: {suite['name']} ({duration:.2f}s)")
            if stderr:
                print(f"Error: {stderr.decode()}")

        results.append(result)

    # Generate report
    print("\n" + "=" * 80)
    print("PERFORMANCE TEST SUMMARY")
    print("=" * 80)

    total_tests = len(results)
    passed_tests = sum(1 for r in results if r["success"])
    failed_tests = total_tests - passed_tests

    print(f"Total Tests: {total_tests}")
    print(f"Passed: {passed_tests}")
    print(f"Failed: {failed_tests}")
    print(f"Success Rate: {(passed_tests / total_tests) * 100:.1f}%")

    # Save report
    report_dir = Path("tests/integration/performance_reports")
    report_dir.mkdir(exist_ok=True)

    report_file = (
        report_dir
        / f"performance_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )

    report_data = {
        "timestamp": datetime.now().isoformat(),
        "summary": {
            "total": total_tests,
            "passed": passed_tests,
            "failed": failed_tests,
            "success_rate": passed_tests / total_tests,
        },
        "results": results,
    }

    with open(report_file, "w") as f:
        json.dump(report_data, f, indent=2)

    print(f"\nDetailed report saved to: {report_file}")

    return passed_tests == total_tests


async def main() -> None:
    """Main entry point"""
    # Check if proxy server is running
    import httpx

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get("http://localhost:8000/")
            if response.status_code != 200:
                print("WARNING: Proxy server may not be running properly")
    except Exception:
        print("ERROR: Proxy server is not running!")
        print("Please start the server with: uvicorn routstr.main:app")
        sys.exit(1)

    # Run performance tests
    success = await run_performance_suite()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
