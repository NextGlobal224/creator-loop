import json
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from creator_loop.backend.runtime.owned_process import OwnedWindowsProcess
from creator_loop.backend.runtime.python_worker import python_worker
from creator_loop.backend.runtime.runtime_ownership import (
    bind_workspace_child,
    create_workspace_marker,
)


@unittest.skipUnless(
    sys.platform == "win32", "Exact Windows source interpreter ownership"
)
class SourceWorkerIdentityTests(unittest.TestCase):
    def test_source_a_request_passes_exact_child_guard_without_chrome_or_scan(self):
        with tempfile.TemporaryDirectory() as temp:
            # _request deliberately requires the canonical ownership path.
            root = Path(temp).resolve(strict=True)
            run = uuid4().hex
            workspace = root / "runtime" / ("facebook-" + run)
            workspace.mkdir(parents=True)
            component = "facebook-browser/1.63.0/existing-cdp"
            marker = create_workspace_marker(
                root, workspace, component, kind="FACEBOOK_BROWSER_WORKSPACE"
            )
            request = workspace / "request.json"
            request.write_text(
                json.dumps(
                    {
                        "format": 1,
                        "mode": "connect",
                        "keyword": "",
                        "limit": 100,
                        "run_id": run,
                        "url": None,
                        "transport": "existing",
                        "target_id": None,
                    }
                )
            )
            executable, prefix, env = python_worker(marker)
            self.assertEqual(str(executable), marker["parent"]["executable"])
            # Validate only the real request/owner guard. Never attach to Chrome.
            code = "import sys;from pathlib import Path;from contextlib import ExitStack;from creator_loop.backend.facebook.facebook_browser import _request;\nwith ExitStack() as held:\n _request(Path(sys.argv[1]),held)\nprint('EXACT_SOURCE_OWNER_VALIDATED')"
            with OwnedWindowsProcess(
                executable,
                ["-c", code, str(request)],
                workspace / "process",
                component_version=component,
                environment=env,
                memory_limit_bytes=256 * 1024**2,
                before_resume=lambda child: bind_workspace_child(
                    workspace, marker, child
                ),
            ) as process:
                outcome = process.wait(15)
            self.assertEqual(
                outcome.exit_code, 0, (workspace / "process/stderr.log").read_text()
            )
            self.assertFalse(outcome.timed_out)
            self.assertIn(
                "EXACT_SOURCE_OWNER_VALIDATED",
                (workspace / "process/stdout.log").read_text(),
            )
