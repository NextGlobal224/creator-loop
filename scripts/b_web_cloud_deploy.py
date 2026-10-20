"""Cloud-hosted B deployment of verified main CI bytes; no build or inference."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from email import policy
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

REPO = "NextGlobal224/creator-loop"
ACCOUNT_SHA = "cb4cbcb99957ea0ea4b795edd35824fe18158d1b0e782945dc09d81d554d9499"
CHECKS = {"windows-artifact", "security-dependencies-workflow", "fast-schema-domain"}
MODEL = "@cf/openai/gpt-oss-120b"
TARGETS = {"production": "creator-loop-b-web", "qa19": "creator-loop-b-web-qa19"}
UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Redirect refused")


def read_bytes(url: str, headers: dict, data=None):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "CreatorLoop-B-Web-CloudDeploy/1", **headers},
        data=None if data is None else json.dumps(data).encode(),
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=45) as r:
            raw = r.read(64 * 1024**2 + 1)
            if len(raw) > 64 * 1024**2:
                raise ValueError("Oversized HTTPS response")
            return raw, r.headers
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"HTTPS operation refused: HTTP{error.code}") from None


def read_json(url: str, headers: dict, data=None):
    raw, response = read_bytes(url, headers, data)
    return json.loads(raw), response


def storage_compatible(current: dict[str, bytes], target: dict[str, bytes]):
    """Conservative rollback: all Python/domain dependencies and schema unchanged."""

    def storage(files):
        return {
            n: b
            for n, b in files.items()
            if n.startswith("worker/python_modules/") or n.endswith((".py", ".sql"))
        }

    if not storage(current) or storage(current) != storage(target):
        raise ValueError("Rollback Python/domain/storage dependency closure changed")
    a, b = (json.loads(f["worker/wrangler.jsonc"]) for f in (current, target))
    for key in (
        "migrations",
        "compatibility_date",
        "compatibility_flags",
        "durable_objects",
    ):
        if a.get(key) != b.get(key):
            raise ValueError("Rollback schema/runtime compatibility changed")


def verify_remote_modules(raw: bytes, content_type: str, files: dict[str, bytes]):
    message = BytesParser(policy=policy.default).parsebytes(
        ("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode()
        + raw
    )
    if not message.is_multipart():
        raise ValueError("Official multipart module response required")
    expected = {}
    for name, body in files.items():
        if name.startswith("worker/python_modules/"):
            expected[name[len("worker/") :]] = body
        elif name.startswith("worker/src/"):
            expected[name[len("worker/src/") :]] = body
    seen = {}
    for part in message.iter_parts():
        name = part.get_filename() or part.get_param(
            "name", header="content-disposition"
        )
        if name == "metadata":
            continue
        body = part.get_payload(decode=True)
        if name in seen or name not in expected or body != expected[name]:
            raise ValueError("Actual deployed module differs from trusted CI bytes")
        seen[name] = sha(body)
    if not expected or set(seen) != set(expected):
        raise ValueError("Missing deployed module")
    return seen


def static_header_policy(files):
    lines = files["worker/public/_headers"].decode("utf-8").splitlines()
    if not lines or lines[0] != "/*":
        raise ValueError("Only reviewed global static header policy supported")
    headers = {}
    for line in lines[1:]:
        if not line.startswith("  ") or ":" not in line:
            raise ValueError("Unsupported static header rule")
        name, value = line.strip().split(":", 1)
        if name.lower() in headers:
            raise ValueError("Duplicate static header rule")
        headers[name.lower()] = value.strip()
    if not headers:
        raise ValueError("Missing static security headers")
    return headers


def activate_once(final_check, create_deployment, current_id, version, record_attempt):
    state = final_check()
    record_attempt(version)
    create_deployment(
        {
            "strategy": "percentage",
            "versions": [{"version_id": version, "percentage": 100}],
        }
    )
    if current_id() != version:
        raise ValueError("Activated version ambiguous; no retry/automatic rollback")
    return state


def verify_ci(run: dict, jobs: dict, workflow: dict, commit: str):
    if not (
        run["head_sha"] == commit
        and run["head_branch"] == "main"
        and run["event"] == "push"
        and run["workflow_id"] == workflow["id"]
        and run["status"] == "completed"
        and run["conclusion"] == "success"
        and run["repository"]["full_name"] == REPO
        and jobs["total_count"] == len(jobs["jobs"])
        and all(
            {j["name"]: j["conclusion"] for j in jobs["jobs"]}.get(name) == "success"
            for name in CHECKS
        )
    ):
        raise ValueError("Trusted current main CI and all required jobs needed")


def runtime_files(raw: bytes, commit: str) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or len(names) > 2000:
            raise ValueError("Duplicate or excessive archive entries")
        result, total = {}, 0
        for item in archive.infolist():
            name, parts = item.filename, PurePosixPath(item.filename).parts
            total += item.file_size
            if (
                not parts
                or name != PurePosixPath(name).as_posix()
                or name != item.orig_filename
                or name.startswith("/")
                or "\\" in name
                or ":" in name
                or item.is_dir()
                or any(p.startswith(".") for p in parts)
                or (item.external_attr >> 16) & 0o170000 == 0o120000
                or item.file_size > 32 * 1024**2
                or total > 64 * 1024**2
            ):
                raise ValueError("Unsafe runtime archive")
            result[name] = archive.read(item)
    manifest = json.loads(result.pop("runtime.json"))
    if (
        manifest.get("format") != "B_WEB_CI_RUNTIME_V1"
        or manifest.get("commit") != commit
        or set(manifest["files"]) != set(result)
        or any(
            not name.startswith("worker/") or sha(value) != manifest["files"][name]
            for name, value in result.items()
        )
        or not {
            "worker/src/entry.py",
            "worker/src/creator_ai.mjs",
            "worker/wrangler.jsonc",
        }
        <= result.keys()
    ):
        raise ValueError("Runtime identity/digest mismatch")
    return result


def compatible_bindings(current: dict, target: dict):
    def binding(value, name):
        return next(b for b in value["resources"]["bindings"] if b["name"] == name)

    a, b = binding(current, "DATABASE"), binding(target, "DATABASE")
    if (
        not a.get("namespace_id")
        or a["namespace_id"] != b.get("namespace_id")
        or a.get("class_name") != "Database"
        or b.get("class_name") != "Database"
        or binding(target, "CREATOR_MODEL").get("text") != MODEL
    ):
        raise ValueError("Rollback database/model bindings incompatible")


def main():
    if (
        os.environ["GITHUB_REPOSITORY"] != REPO
        or os.environ["GITHUB_REF"] != "refs/heads/main"
    ):
        raise ValueError("Protected main workflow only")
    commit, run = os.environ["GITHUB_SHA"], os.environ["B_CI_RUN"]
    if not re.fullmatch(r"[a-f0-9]{40}", commit) or not re.fullmatch(
        r"[0-9]{1,20}", run
    ):
        raise ValueError("Exact commit/run needed")
    target, operation = os.environ["B_TARGET"], os.environ["B_OPERATION"]
    worker = TARGETS[target]
    expected = os.environ["B_EXPECTED_DEPLOYMENT"]
    if operation not in ("deploy", "rollback") or not re.fullmatch(UUID, expected):
        raise ValueError("Explicit operation and current deployment needed")

    def gh(*args):
        return subprocess.check_output(["gh", *args], timeout=180)

    def github(path):
        return json.loads(gh("api", "repos/" + REPO + path))

    def artifact_bytes(run_id, name):
        metadata = github("/actions/runs/" + str(run_id) + "/artifacts?per_page=100")
        matches = [
            a for a in metadata["artifacts"] if a["name"] == name and not a["expired"]
        ]
        if metadata["total_count"] != len(metadata["artifacts"]) or len(matches) != 1:
            raise ValueError("Unambiguous authoritative artifact needed")
        artifact = matches[0]
        raw = gh("api", f"repos/{REPO}/actions/artifacts/{artifact['id']}/zip")
        if artifact.get("digest") != "sha256:" + sha(raw):
            raise ValueError("Authoritative artifact digest mismatch")
        return raw, artifact["digest"]

    def trusted_runtime(ci_run, source_commit):
        verify_ci(
            github("/actions/runs/" + str(ci_run)),
            github("/actions/runs/" + str(ci_run) + "/jobs?per_page=100"),
            github("/actions/workflows/ci.yml"),
            source_commit,
        )
        raw, digest = artifact_bytes(ci_run, "b-web-runtime")
        with zipfile.ZipFile(io.BytesIO(raw)) as outer:
            if (
                outer.namelist() != ["b-web-runtime.zip"]
                or outer.getinfo("b-web-runtime.zip").file_size > 64 * 1024**2
            ):
                raise ValueError("Unexpected runtime wrapper")
            archive = outer.read("b-web-runtime.zip")
        return runtime_files(archive, source_commit), sha(archive), digest

    def qualified_receipt(run_id, requested_version):
        if not re.fullmatch(r"[0-9]{1,20}", run_id):
            raise ValueError("Exact prior successful deployment workflow needed")
        meta = github("/actions/runs/" + run_id)
        workflow = github("/actions/workflows/b_web_deploy.yml")
        if not (
            meta["workflow_id"] == workflow["id"]
            and meta["event"] == "workflow_dispatch"
            and meta["head_branch"] == "main"
            and meta["status"] == "completed"
            and meta["conclusion"] == "success"
            and meta["repository"]["full_name"] == REPO
        ):
            raise ValueError(
                "Rollback receipt needs successful protected-main workflow"
            )
        raw, _ = artifact_bytes(run_id, "b-web-cloud-deployment")
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if (
                archive.namelist().count("deploy-result.json") != 1
                or archive.getinfo("deploy-result.json").file_size > 1024**2
            ):
                raise ValueError("Invalid workflow receipt")
            receipt = json.loads(archive.read("deploy-result.json"))
        if not (
            receipt.get("operation") == "deploy"
            and receipt.get("live_runtime_confirmed") is True
            and receipt.get("deployment") == requested_version
            and receipt.get("target") == target
            and receipt.get("account_sha256") == ACCOUNT_SHA
            and receipt.get("workflow_run") == run_id
            and receipt.get("remote_bytes_verified") is True
            and receipt.get("source_commit") == meta["head_sha"]
        ):
            raise ValueError("Unqualified rollback version/target/account/bytes")
        files, archive_hash, digest = trusted_runtime(
            receipt["ci_run"], receipt["source_commit"]
        )
        if (
            receipt.get("runtime_sha256") != archive_hash
            or receipt.get("artifact_digest") != digest
        ):
            raise ValueError("Receipt runtime differs from authoritative CI")
        return files

    verify_ci(
        github("/actions/runs/" + run),
        github("/actions/runs/" + run + "/jobs?per_page=100"),
        github("/actions/workflows/ci.yml"),
        commit,
    )
    if operation == "deploy" and github("/commits/main")["sha"] != commit:
        raise ValueError("Main changed; no deployment")
    account, token = (
        os.environ["CLOUDFLARE_ACCOUNT_ID"],
        os.environ["CLOUDFLARE_API_TOKEN"],
    )
    if sha(account.encode()) != ACCOUNT_SHA:
        raise ValueError("Wrong Cloudflare account")
    root = Path(__file__).resolve().parents[1]
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    api = "https://api.cloudflare.com/client/v4/accounts/" + account

    def cloud(path, data=None):
        result, _ = read_json(api + path, headers, data)
        if not result.get("success"):
            raise RuntimeError("Cloudflare operation refused")
        return result["result"]

    script = "/workers/scripts/" + worker

    def current_id():
        versions = cloud(script + "/deployments")["deployments"][0]["versions"]
        if len(versions) != 1 or versions[0]["percentage"] != 100:
            raise ValueError("Refuse mixed deployment")
        return versions[0]["version_id"]

    if current_id() != expected:
        raise ValueError("Deployment changed; no upload/rollback performed")
    previous_version = cloud(script + "/versions/" + expected)
    compatible_bindings(previous_version, previous_version)
    if cloud("/workers/account-settings")["default_usage_model"] != "standard":
        raise ValueError("Purchased standard plan configuration changed")
    origin = "https://" + worker + ".creator-loop-ai-beta.workers.dev"
    password = os.environ[
        "B_WEB_QA19_PASSWORD" if target == "qa19" else "B_WEB_QA_PASSWORD"
    ]
    if not password:
        raise ValueError(
            "Target-specific login credential missing; never fall back to another target"
        )
    _, response_headers = read_json(
        origin + "/api/login",
        {"Origin": origin, "Content-Type": "application/json"},
        {"password": password},
    )
    cookie = response_headers["Set-Cookie"].split(";", 1)[0]
    if not cookie.startswith("__Host-creator="):
        raise ValueError("Expected private session")

    def app(path):
        return read_json(origin + path, {"Cookie": cookie})[0]

    before = app("/api/state")
    if any(j["status"] in ("QUEUED", "RUNNING") for j in before["jobs"]):
        raise ValueError("Active job; no deployment")

    def final_preconditions():
        # Upload/build can take minutes. No traffic mutation occurs until these
        # fresh reads. CF activation has no CAS; this is not atomic quiescence.
        if github("/commits/main")["sha"] != commit or current_id() != expected:
            raise ValueError(
                "Main/deployment changed; inactive candidate not activated"
            )
        compatible_bindings(previous_version, cloud(script + "/versions/" + expected))
        state = app("/api/state")
        if any(j["status"] in ("QUEUED", "RUNNING") for j in state["jobs"]):
            raise ValueError("Active job before activation; no traffic mutation")
        return state

    # The operator supplies only an already-observed included-resource proof.
    # Re-read relevant account usage here; never rely on its age/value alone.
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    query = """query($a:string,$s:string,$e:string){viewer{accounts(filter:{accountTag:$a}){
      workersInvocationsAdaptive(limit:1,filter:{datetime_geq:$s,datetime_leq:$e}){sum{requests cpuTimeUs}}
      durableObjectsInvocationsAdaptiveGroups(limit:1,filter:{datetime_geq:$s,datetime_leq:$e}){sum{requests}}
      durableObjectsPeriodicGroups(limit:1,filter:{datetime_geq:$s,datetime_leq:$e}){sum{duration rowsRead rowsWritten}}
      durableObjectsSqlStorageGroups(limit:1000,filter:{datetime_geq:$s,datetime_leq:$e}){dimensions{namespaceId} max{storedBytes}}
    }}}"""
    observed, _ = read_json(
        "https://api.cloudflare.com/client/v4/graphql",
        headers,
        {
            "query": query,
            "variables": {
                "a": account,
                "s": (now - timedelta(days=31)).isoformat(),
                "e": now.isoformat(),
            },
        },
    )
    if observed.get("errors"):
        raise ValueError("Cannot verify included resources")
    usage = observed["data"]["viewer"]["accounts"][0]
    w = usage["workersInvocationsAdaptive"][0]["sum"]
    d = usage["durableObjectsPeriodicGroups"][0]["sum"]
    requests = usage["durableObjectsInvocationsAdaptiveGroups"][0]["sum"]["requests"]
    storage = usage["durableObjectsSqlStorageGroups"]
    if not (
        w["requests"] + 400 < 10000000
        and w["cpuTimeUs"] / 1000 + 12000000 < 30000000
        and requests + 400 < 1000000
        and d["duration"] + 60000 < 400000
        and d["rowsRead"] + 10000000 < 25000000000
        and d["rowsWritten"] + 1000000 < 50000000
        and 0 < len(storage) < 1000
        and sum(s["max"]["storedBytes"] for s in storage) + 512 * 1024**2 < 5 * 1024**3
    ):
        raise ValueError("Included-resource guard closed")
    record = {
        "execution": "live",
        "provider": "cloudflare",
        "component": "B",
        "operation": operation,
        "source_commit": commit,
        "ci_run": int(run),
        "workflow_run": os.environ["GITHUB_RUN_ID"],
        "target": target,
        "account_sha256": ACCOUNT_SHA,
        "previous_deployment": expected,
        "started_at": time.time(),
        "model_calls": 0,
        "included_resource_guard": True,
        "outside_plan_charge_authorized_usd": 0,
    }
    Path("deploy-result.json").write_text(json.dumps(record, indent=2))
    if operation == "rollback":
        version = os.environ["B_ROLLBACK_VERSION"]
        if not re.fullmatch(UUID, version) or version == expected:
            raise ValueError("Explicit distinct compatible rollback version needed")
        compatible_bindings(
            cloud(script + "/versions/" + expected),
            cloud(script + "/versions/" + version),
        )
        current_files = qualified_receipt(
            os.environ["B_CURRENT_WORKFLOW_RUN"], expected
        )
        files = qualified_receipt(os.environ["B_ROLLBACK_WORKFLOW_RUN"], version)
        storage_compatible(current_files, files)
        # No DB migration/restore; complete Python/domain tree must be identical.
    else:
        files, archive_hash, artifact_digest = trusted_runtime(run, commit)
        destination = Path(os.environ["RUNNER_TEMP"]) / (
            "b-web-runtime-" + os.environ["GITHUB_RUN_ID"]
        )
        destination.mkdir(exist_ok=False)
        for name, value in files.items():
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        config_path = destination / "worker/wrangler.jsonc"
        config = json.loads(config_path.read_text())
        if config["name"] != TARGETS["production"] or config["migrations"] != [
            {"tag": "v1", "new_sqlite_classes": ["Database"]}
        ]:
            raise ValueError("Unexpected worker/migrations")
        config["name"] = worker
        config_path.write_text(json.dumps(config))
        record.update(
            runtime_sha256=archive_hash,
            artifact_digest=artifact_digest,
            runtime_files={name: sha(value) for name, value in files.items()},
        )
        Path("deploy-result.json").write_text(json.dumps(record, indent=2))
        with Path("deploy.log").open("wb") as log:
            result = subprocess.run(
                [
                    "node",
                    str(
                        root
                        / "cloudflare/creator-ai/node_modules/wrangler/bin/wrangler.js"
                    ),
                    "versions",
                    "upload",
                    "--config",
                    str(config_path),
                    "--outdir",
                    str(destination / "uploaded"),
                ],
                cwd=destination / "worker",
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=180,
                env={
                    **os.environ,
                    "WRANGLER_SEND_METRICS": "false",
                    "WRANGLER_WRITE_LOGS": "false",
                },
            )
        record["deploy_exit_code"] = result.returncode
        Path("deploy-result.json").write_text(json.dumps(record, indent=2))
        if result.returncode:
            raise RuntimeError(
                "Upload not confirmed; inspect existing deployment, never auto retry"
            )
        matches = re.findall(
            r"Worker Version ID:\s*(" + UUID + r")", Path("deploy.log").read_text()
        )
        if len(matches) != 1 or matches[0] == expected:
            raise ValueError("Inactive uploaded version unconfirmed; do not retry")
        version = matches[0]
        record["uploaded_inactive_version"] = version
        Path("deploy-result.json").write_text(json.dumps(record, indent=2))
        compatible_bindings(previous_version, cloud(script + "/versions/" + version))

    def record_attempt(candidate):
        record["activation_attempted"] = True
        record["candidate_version"] = candidate
        Path("deploy-result.json").write_text(json.dumps(record, indent=2))

    before = activate_once(
        final_preconditions,
        lambda data: cloud(script + "/deployments", data),
        current_id,
        version,
        record_attempt,
    )
    record["deployment"] = version
    compatible_bindings(
        previous_version, cloud(script + "/versions/" + record["deployment"])
    )
    record["finished_at"] = time.time()
    Path("deploy-result.json").write_text(json.dumps(record, indent=2))
    time.sleep(8)  # One bounded propagation wait, no inference/status poll loop.
    runtime = app("/api/runtime")
    if runtime["deployment"] != record["deployment"] or runtime["model"] != MODEL:
        raise ValueError(
            "Deployment occurred but live observation not confirmed; read before any retry"
        )
    if app("/api/state")["revision"] < before["revision"]:
        raise ValueError("Domain revision regressed")
    config = json.loads(files["worker/wrangler.jsonc"])
    settings = cloud(script + "/settings")
    binding_types = {
        "AI": "ai",
        "ASSETS": "assets",
        "CREATOR_MODEL": "plain_text",
        "CREATOR_VERSION": "version_metadata",
        "DATABASE": "durable_object_namespace",
        "LOGIN_PASSWORD_HASH": "secret_text",
    }
    if target == "production":
        binding_types["OPERATOR_KEY"] = "secret_text"
    bindings = {b["name"]: b for b in settings["bindings"]}
    expected_namespace = next(
        b["namespace_id"]
        for b in previous_version["resources"]["bindings"]
        if b["name"] == "DATABASE"
    )
    if not (
        len(bindings) == len(settings["bindings"])
        and {n: b["type"] for n, b in bindings.items()} == binding_types
        and settings["compatibility_date"] == config["compatibility_date"]
        and sorted(settings["compatibility_flags"])
        == sorted(config["compatibility_flags"])
        and settings.get("logpush") in (None, False)
        and bindings["DATABASE"]["namespace_id"] == expected_namespace
        and bindings["DATABASE"]["class_name"] == "Database"
        and bindings["DATABASE"].get("script_name") in (None, worker)
        and bindings["CREATOR_MODEL"]["text"] == MODEL
    ):
        raise ValueError("Actual settings differ from trusted runtime")
    raw, response = read_bytes(api + script + "/content/v2", headers)
    record["modules"] = verify_remote_modules(raw, response["Content-Type"], files)
    assets = {}
    header_policy = static_header_policy(files)
    for name, expected_bytes in files.items():
        if not name.startswith("worker/public/"):
            continue
        suffix = name[len("worker/public/") :]
        if suffix == "_headers":
            continue  # Assets control metadata, verified by actual response policy.
        if suffix.startswith("_"):
            raise ValueError("Unsupported Assets control metadata")
        path = (
            "/"
            if suffix == "index.html"
            else ("/login" if suffix == "login.html" else "/" + suffix)
        )
        body, asset_headers = read_bytes(origin + path, {"Cookie": cookie})
        if body != expected_bytes or any(
            asset_headers.get(k) != v for k, v in header_policy.items()
        ):
            raise ValueError("Actual static asset differs from trusted CI")
        assets[suffix] = sha(body)
    if current_id() != version or app("/api/runtime")["deployment"] != version:
        raise ValueError("Deployment changed during byte verification")
    record["static_assets"] = assets
    record["static_header_policy_sha256"] = sha(files["worker/public/_headers"])
    record["namespace_sha256"] = sha(expected_namespace.encode())
    record["settings_verified"] = True
    record["remote_bytes_verified"] = True
    record["live_runtime_confirmed"] = True
    Path("deploy-result.json").write_text(json.dumps(record, indent=2))
    print(
        json.dumps(
            {
                "deployment": record["deployment"],
                "target": target,
                "operation": operation,
                "model_calls": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
