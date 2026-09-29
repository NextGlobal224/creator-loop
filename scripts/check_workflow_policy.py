"""Small bootstrap policy check; not a replacement for hosted secret scanning."""

import re
from pathlib import Path

workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
assert re.search(r"^permissions:\s*\n\s*contents: read$", workflow, re.M)
assert "pull_request_target:" not in workflow
for action in re.findall(r"^\s*- uses:\s*(\S+)", workflow, re.M):
    assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", action), f"Unpinned action: {action}"
assert "fetch-depth: 0" in workflow
assert "gitleaks\" git --redact --log-opts='--all'" in workflow
print("Workflow policy OK: read default, full history secret scan, actions pinned")
