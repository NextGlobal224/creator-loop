"""Failure boundaries for durable Web wakeups, without a model or cloud service."""

import importlib.util
import json
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


class WebJobRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        module = types.ModuleType("workers")
        module.DurableObject = type("DurableObject", (), {})
        module.WorkerEntrypoint = type("WorkerEntrypoint", (), {})
        module.Response = object
        module.import_from_javascript = lambda _: None
        module.python_to_rpc = lambda value: value
        path = (
            Path(__file__).resolve().parents[2] / "cloudflare/creator-web/src/entry.py"
        )
        spec = importlib.util.spec_from_file_location("web_recovery_under_test", path)
        loaded = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"workers": module}):
            spec.loader.exec_module(loaded)
        self.loaded = loaded
        self.app = object.__new__(loaded.Database)
        self.storage = types.SimpleNamespace(
            getAlarm=AsyncMock(return_value=None), setAlarm=AsyncMock()
        )
        self.app.ctx = types.SimpleNamespace(storage=self.storage)

    async def test_queued_receipt_without_alarm_rearms_exactly_once(self):
        self.app.rows = lambda *_: [
            {
                "status": "QUEUED",
                "record": json.dumps({"id": "same-id", "status": "QUEUED"}),
            }
        ]
        await self.app.recover_wakeup()
        self.storage.setAlarm.assert_awaited_once()
        self.assertGreater(
            self.storage.setAlarm.await_args.args[0], time.time() * 1000 - 1000
        )

    async def test_existing_alarm_does_not_duplicate_or_replace_schedule(self):
        self.app.rows = lambda *_: [{"status": "QUEUED", "record": "{}"}]
        self.storage.getAlarm.return_value = 1000
        await self.app.recover_wakeup()
        self.storage.setAlarm.assert_not_awaited()

    async def test_running_owner_loss_is_failed_unknown_without_another_call(self):
        original = {
            "id": "same-id",
            "status": "RUNNING",
            "lease_until": time.time() - 1,
            "calls": [{"outcome": "UNKNOWN"}],
        }
        self.app.rows = lambda *_: [{"record": json.dumps(original)}]
        observed = []
        self.app.sync_run = observed.append
        await self.app.alarm()
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["status"], "FAILED")
        self.assertTrue(observed[0]["unknown_outcome"])
        self.assertEqual(observed[0]["calls"], original["calls"])

    async def test_live_running_lease_is_preserved_without_resubmission(self):
        lease = time.time() + 300
        self.app.rows = lambda *_: [
            {
                "record": json.dumps(
                    {"id": "same-id", "status": "RUNNING", "lease_until": lease}
                )
            }
        ]
        await self.app.alarm()
        self.storage.setAlarm.assert_awaited_once_with(int(lease * 1000) + 1)

    async def test_alarm_failure_before_enqueue_cannot_commit_a_job(self):
        # Assert the executable method order at the actual failure boundary,
        # rather than claiming that a mock tests remote storage durability.
        import ast
        import inspect
        import textwrap

        tree = ast.parse(
            textwrap.dedent(inspect.getsource(self.loaded.Database.authorized))
        )
        schedule = [
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.Await)
            and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Attribute)
            and n.value.func.attr == "setAlarm"
        ]
        commit = [
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "transactionSync"
        ]
        self.assertLess(max(schedule), min(commit))
