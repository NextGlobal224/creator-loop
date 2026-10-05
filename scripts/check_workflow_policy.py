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
release = workflow.split("  release:\n", 1)[1]
assert "needs: [fast, security, windows-artifact]" in release
assert release.index("scripts/check_release_gate.py") < release.index(
    "gh release create"
)
for binding in ("--expected-commit", "--expected-run-url", "--tag"):
    assert binding in release, f"Missing publication binding: {binding}"
print(
    "Workflow policy OK: read default, full history secret scan, actions pinned, V1 publish guard"
)
