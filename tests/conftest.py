"""Shared fixtures for the test suite.

`manager` is session-scoped deliberately: creating/disposing multiple
StaAutomationThreadManager instances across the test session (even across
different test files, each with their own module-scoped instance) raced with
the `uiautomation` package's process-wide cached COM state and produced a
`RPC_E_DISCONNECTED`-class crash — reproduced live while building this port
(see PORT_NOTES.md). One long-lived manager per process is also how the real
app uses it (a singleton service), not one per test module.
"""

import sys

import pytest


@pytest.fixture(scope="session")
def manager():
    if sys.platform != "win32":
        yield None
        return

    from winspark.automation.sta_thread_manager import StaAutomationThreadManager

    mgr = StaAutomationThreadManager()
    try:
        yield mgr
    finally:
        mgr.dispose()
