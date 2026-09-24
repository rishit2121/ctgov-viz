from __future__ import annotations

from typing import Any


def pytest_terminal_summary(terminalreporter: Any) -> None:
    from tests.golden.test_golden import RESULTS

    if RESULTS:
        terminalreporter.write_line(
            f"golden planner accuracy: {sum(RESULTS)}/{len(RESULTS)}")
