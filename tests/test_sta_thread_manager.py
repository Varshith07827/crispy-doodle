"""Windows-only tests for StaAutomationThreadManager (port of
StaAutomationThreadManager.cs). Verifies work items marshal onto a single
dedicated thread (not the caller's) and that a failing work item surfaces its
exception to the caller without killing the queue for subsequent items —
the two properties the STA design exists to guarantee.
"""

import sys
import threading

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="requires pythoncom / a real Windows session")


@pytest.mark.asyncio
async def test_work_runs_on_a_single_dedicated_thread_not_the_caller():
    from winspark.automation.sta_thread_manager import StaAutomationThreadManager

    manager = StaAutomationThreadManager()
    try:
        caller_thread_id = threading.get_ident()

        thread_id_1 = await manager.invoke_async(threading.get_ident)
        thread_id_2 = await manager.invoke_async(threading.get_ident)

        assert thread_id_1 == thread_id_2
        assert thread_id_1 != caller_thread_id
        assert manager.is_healthy
    finally:
        manager.dispose()


@pytest.mark.asyncio
async def test_failed_work_item_raises_to_caller_without_breaking_the_queue():
    from winspark.automation.sta_thread_manager import StaAutomationThreadManager

    manager = StaAutomationThreadManager()
    try:

        def _boom():
            raise ValueError("simulated automation failure")

        with pytest.raises(ValueError, match="simulated automation failure"):
            await manager.invoke_async(_boom)

        # queue must still be usable after a failed item
        result = await manager.invoke_async(lambda: 1 + 1)
        assert result == 2

        health = manager.get_health()
        assert health.failed_requests >= 1
        assert health.total_requests_processed >= 1
    finally:
        manager.dispose()


@pytest.mark.asyncio
async def test_dispose_prevents_further_work():
    from winspark.automation.sta_thread_manager import StaAutomationThreadManager

    manager = StaAutomationThreadManager()
    manager.dispose()

    with pytest.raises(RuntimeError):
        await manager.invoke_async(lambda: None)
