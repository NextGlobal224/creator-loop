"""Cloud-hosted B deployment of verified main CI bytes; no build or inference."""

from __future__ import annotations

import ast
import base64
import copy
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from contextlib import closing
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

REPO = "NextGlobal224/creator-loop"
ACCOUNT_SHA = "cb4cbcb99957ea0ea4b795edd35824fe18158d1b0e782945dc09d81d554d9499"
CHECKS = {"windows-artifact", "security-dependencies-workflow", "fast-schema-domain"}
MODEL = "@cf/openai/gpt-oss-120b"
TARGETS = {
    "production": "creator-loop-b-web",
    "qa19": "creator-loop-b-web-qa19",
    "qa-fixture": "creator-loop-b-snapshot-qa",
}
UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"


def validate_fixture_config(config: dict) -> None:
    """Reject all service or cross-worker bindings in the isolated prototype."""
    if not isinstance(config, dict) or set(config) != {
        "name",
        "main",
        "compatibility_date",
        "compatibility_flags",
        "rules",
        "workers_dev",
        "observability",
        "durable_objects",
        "migrations",
        "assets",
        "version_metadata",
        "vars",
    }:
        raise ValueError("Unexpected fixture Worker configuration")
    if (
        config["name"] != TARGETS["qa-fixture"]
        or config["main"] != "src/entry.py"
        or config["compatibility_date"] != "2026-10-13"
        or config["compatibility_flags"] != ["python_workers"]
        or config["rules"] != [{"type": "ESModule", "globs": ["**/*.mjs"]}]
        or config["workers_dev"] is not True
        or config["observability"] != {"enabled": False}
        or config["durable_objects"]
        != {"bindings": [{"name": "DATABASE", "class_name": "Database"}]}
        or config["migrations"] != [{"tag": "v1", "new_sqlite_classes": ["Database"]}]
        or config["assets"]
        != {"directory": "public", "binding": "ASSETS", "run_worker_first": True}
        or config["version_metadata"] != {"binding": "CREATOR_VERSION"}
    ):
        raise ValueError("Unsafe fixture Worker identity or binding")
    base = {
        "B_WEB_OBSERVATION_ONLY": "1",
        "B_WEB_ISOLATED_RESTORE": "1",
        "B_WEB_QA_READONLY_LOGIN": "1",
        "B_WEB_QA_SNAPSHOT_EXPORT": "1",
        "B_WEB_QA_HOSTNAME": "creator-loop-b-snapshot-qa.creator-loop-ai-beta.workers.dev",
    }
    patterns = {
        "B_WEB_QA_RESTORE_OBJECT_NAME": "qa-isolated-restore-[a-f0-9]{32}",
        "B_WEB_QA_RESTORE_SNAPSHOT_ID": "[a-f0-9]{32}",
        "B_WEB_QA_RESTORE_MANIFEST_SHA256": "[a-f0-9]{64}",
        "B_WEB_QA_EXPORT_SNAPSHOT_ID": "[a-f0-9]{32}",
        "B_WEB_QA_SOURCE_OBJECT_NAME": "qa-isolated-restore-[a-f0-9]{32}",
        "B_WEB_QA_SOURCE_SNAPSHOT_ID": "[a-f0-9]{32}",
        "B_WEB_QA_SOURCE_MANIFEST_SHA256": "[a-f0-9]{64}",
    }
    values = config["vars"]
    if not isinstance(values, dict) or any(values.get(k) != v for k, v in base.items()):
        raise ValueError("Unsafe fixture guard configuration")
    extra = set(values) - set(base)
    if extra not in (set(), set(patterns)) or any(
        not isinstance(values[key], str) or not re.fullmatch(pattern, values[key])
        for key, pattern in patterns.items()
        if key in values
    ):
        raise ValueError("Invalid fixture snapshot identity")
    if (
        extra
        and values["B_WEB_QA_SOURCE_OBJECT_NAME"]
        == values["B_WEB_QA_RESTORE_OBJECT_NAME"]
    ):
        raise ValueError("Fixture source and target must differ")


def validate_fixture_namespace(settings: dict, production_id: str, qa19_id: str) -> str:
    """Validate observed metadata; this does not prove quiescence or capacity."""
    if (
        not isinstance(production_id, str)
        or not re.fullmatch("[a-f0-9]{32}", production_id)
        or not isinstance(qa19_id, str)
        or not re.fullmatch("[a-f0-9]{32}", qa19_id)
        or production_id == qa19_id
        or not isinstance(settings, dict)
        or not isinstance(settings.get("bindings"), list)
    ):
        raise ValueError("Existing namespace identities are incomplete")
    bindings = settings["bindings"]
    permitted = {
        "DATABASE": "durable_object_namespace",
        "ASSETS": "assets",
        "CREATOR_VERSION": "version_metadata",
        "OPERATOR_KEY": "secret_text",
        "LOGIN_PASSWORD_HASH": "secret_text",
        "B_WEB_OBSERVATION_ONLY": "plain_text",
        "B_WEB_ISOLATED_RESTORE": "plain_text",
        "B_WEB_QA_READONLY_LOGIN": "plain_text",
        "B_WEB_QA_SNAPSHOT_EXPORT": "plain_text",
        "B_WEB_QA_HOSTNAME": "plain_text",
        "B_WEB_QA_RESTORE_OBJECT_NAME": "plain_text",
        "B_WEB_QA_RESTORE_SNAPSHOT_ID": "plain_text",
        "B_WEB_QA_RESTORE_MANIFEST_SHA256": "plain_text",
        "B_WEB_QA_EXPORT_SNAPSHOT_ID": "plain_text",
        "B_WEB_QA_SOURCE_OBJECT_NAME": "plain_text",
        "B_WEB_QA_SOURCE_SNAPSHOT_ID": "plain_text",
        "B_WEB_QA_SOURCE_MANIFEST_SHA256": "plain_text",
    }
    names = [b.get("name") for b in bindings if isinstance(b, dict)]
    if len(names) != len(bindings) or any(not isinstance(name, str) for name in names):
        raise ValueError("Unexpected fixture binding name")
    if len(set(names)) != len(names) or any(
        b["name"] not in permitted or permitted[b["name"]] != b.get("type")
        for b in bindings
    ):
        raise ValueError("Unexpected fixture service or duplicate binding")
    databases = [
        b for b in bindings if isinstance(b, dict) and b.get("name") == "DATABASE"
    ]
    if len(databases) != 1:
        raise ValueError("Fixture DATABASE binding missing or ambiguous")
    binding = databases[0]
    namespace = binding.get("namespace_id")
    if (
        binding.get("type") != "durable_object_namespace"
        or binding.get("class_name") != "Database"
        or binding.get("script_name") not in (None, TARGETS["qa-fixture"])
        or not isinstance(namespace, str)
        or not re.fullmatch("[a-f0-9]{32}", namespace)
        or namespace in (production_id, qa19_id)
        or any(
            isinstance(b, dict) and b.get("type") in ("ai", "browser") for b in bindings
        )
    ):
        raise ValueError("Fixture namespace or service binding unsafe")
    return namespace


def expected_binding_types(
    target: str, previous_version: dict, config: dict | None = None
) -> dict[str, str]:
    if target == "qa-fixture":
        if config is None:
            raise ValueError("Exact fixture configuration required")
        validate_fixture_config(config)
        return {
            "DATABASE": "durable_object_namespace",
            "ASSETS": "assets",
            "CREATOR_VERSION": "version_metadata",
            "OPERATOR_KEY": "secret_text",
            "LOGIN_PASSWORD_HASH": "secret_text",
            **{name: "plain_text" for name in config["vars"]},
        }
    types = {
        "AI": "ai",
        "ASSETS": "assets",
        "CREATOR_MODEL": "plain_text",
        "CREATOR_VERSION": "version_metadata",
        "DATABASE": "durable_object_namespace",
        "LOGIN_PASSWORD_HASH": "secret_text",
    }
    if config and "browser" in config:
        if config["browser"] != {"binding": "BROWSER"}:
            raise ValueError("Unexpected browser binding configuration")
        types["BROWSER"] = "browser"
    if target not in TARGETS:
        raise ValueError("Unsupported deployment target")
    operator = [
        b
        for b in previous_version["resources"]["bindings"]
        if b["name"] == "OPERATOR_KEY"
    ]
    if target == "production":
        types["OPERATOR_KEY"] = "secret_text"
    elif operator:
        # Preserve the previously provisioned *isolated QA* operator. This
        # verifier cannot introduce/copy secrets or allow arbitrary bindings.
        if len(operator) != 1 or operator[0]["type"] != "secret_text":
            raise ValueError("Unexpected existing QA operator binding")
        types["OPERATOR_KEY"] = "secret_text"
    return types


def verify_fixture_bindings(
    settings: dict, config: dict, production_id: str, qa19_id: str
) -> str:
    """Verify actual bindings before the first fixture request, not merely the namespace."""
    namespace = validate_fixture_namespace(settings, production_id, qa19_id)
    expected = expected_binding_types(
        "qa-fixture", {"resources": {"bindings": []}}, config
    )
    if "B_WEB_QA_SOURCE_OBJECT_NAME" not in config["vars"]:
        raise ValueError("Fixture snapshot identities are not configured")
    bindings = {binding["name"]: binding for binding in settings["bindings"]}
    if set(bindings) != set(expected) or any(
        bindings[name].get("type") != kind for name, kind in expected.items()
    ):
        raise ValueError("Fixture binding set incomplete or unexpected")
    if any(
        bindings[name].get("text") != value for name, value in config["vars"].items()
    ):
        raise ValueError("Fixture guard values differ from verified configuration")
    return namespace


def verify_fixture_namespace_ownership(
    settings: dict,
    config: dict,
    production_id: str,
    qa19_id: str,
    observation: dict,
) -> str:
    """Require complete account namespace metadata before any fixture request."""
    namespace = verify_fixture_bindings(settings, config, production_id, qa19_id)
    rows = observation.get("result") if isinstance(observation, dict) else None
    info = observation.get("result_info") if isinstance(observation, dict) else None
    if (
        not isinstance(observation, dict)
        or observation.get("success") is not True
        or not isinstance(rows, list)
        or not isinstance(info, dict)
        or type(info.get("total_count")) is not int
        or info["total_count"] != len(rows)
        or info.get("cursor") not in (None, "")
        or (info.get("has_more") is not None and info.get("has_more") is not False)
        or (
            info.get("total_pages") is not None
            and (type(info["total_pages"]) is not int or info["total_pages"] != 1)
        )
        or not all(isinstance(row, dict) for row in rows)
    ):
        raise ValueError("Complete namespace metadata required")
    for identity, script in (
        (production_id, TARGETS["production"]),
        (qa19_id, TARGETS["qa19"]),
        (namespace, TARGETS["qa-fixture"]),
    ):
        matches = [row for row in rows if row.get("id") == identity]
        if len(matches) != 1 or not (
            matches[0].get("script") == script
            and matches[0].get("class") == "Database"
            and matches[0].get("use_sqlite") is True
        ):
            raise ValueError("Namespace owner or SQLite backend mismatch")
    return namespace


def fixture_bootstrap_sequence(
    preflight, create, observe, restore, login, record, config, cleanup=None
):
    """Order a single isolated first-create; caller must supply trusted I/O adapters.

    Preflight must independently establish identity, permission, artifact and
    the resource budget before returning the two existing namespace IDs. This
    sequencer does not itself prove those conditions and is not yet exposed by
    the live deployment workflow.
    """
    validate_fixture_config(config)
    if "B_WEB_QA_SOURCE_OBJECT_NAME" not in config["vars"]:
        raise ValueError("Fixture snapshot identities are not configured")
    observe.precreate(config)
    production_id, qa19_id = preflight(config)
    if (
        not isinstance(production_id, str)
        or not re.fullmatch("[a-f0-9]{32}", production_id)
        or not isinstance(qa19_id, str)
        or not re.fullmatch("[a-f0-9]{32}", qa19_id)
        or production_id == qa19_id
    ):
        raise ValueError("Existing namespace identities are incomplete")
    record({"state": "creation_attempting", "target": TARGETS["qa-fixture"]})
    try:
        observe.precreate(config)
    except Exception:
        record({"state": "precreate_refused", "target": TARGETS["qa-fixture"]})
        raise
    try:
        candidate = create(copy.deepcopy(config))
    except Exception:
        record({"state": "creation_unknown", "target": TARGETS["qa-fixture"]})
        raise
    record({"state": "creation_returned", "target": TARGETS["qa-fixture"]})
    try:
        if not isinstance(candidate, str) or not re.fullmatch(UUID, candidate):
            raise ValueError("Fixture creation version unconfirmed")
        settings, namespaces, deployment = observe(candidate, production_id, qa19_id)
        namespace = verify_fixture_namespace_ownership(
            settings, config, production_id, qa19_id, namespaces
        )
        if deployment != [{"version_id": candidate, "percentage": 100}]:
            raise ValueError("Fixture deployed version or traffic mismatch")
    except Exception:
        record({"state": "postcreate_unverified", "target": TARGETS["qa-fixture"]})
        raise
    record({"state": "metadata_verified", "target": TARGETS["qa-fixture"]})
    try:
        restored = restore(candidate)
        if (
            not isinstance(restored, dict)
            or restored.get("restored") is not True
            or restored.get("jobs_held") is not True
            or restored.get("alarm_restored") is not False
            or restored.get("target") != config["vars"]["B_WEB_QA_RESTORE_OBJECT_NAME"]
            or restored.get("snapshot_id")
            != config["vars"]["B_WEB_QA_RESTORE_SNAPSHOT_ID"]
            or restored.get("manifest_sha256")
            != config["vars"]["B_WEB_QA_RESTORE_MANIFEST_SHA256"]
            or restored.get("deployment") != candidate
        ):
            raise ValueError("Fixture restore or hold unconfirmed")
    except Exception:
        record({"state": "restore_unverified", "target": TARGETS["qa-fixture"]})
        raise
    record({"state": "restore_verified", "target": TARGETS["qa-fixture"]})
    try:
        receipt = login(candidate)
        if (
            not isinstance(receipt, dict)
            or set(receipt) != {"authenticated", "deployment"}
            or receipt.get("authenticated") is not True
            or receipt.get("deployment") != candidate
        ):
            raise ValueError("Fixture login or runtime identity unconfirmed")
    except Exception:
        record({"state": "login_unverified", "target": TARGETS["qa-fixture"]})
        raise
    record({"state": "login_verified", "target": TARGETS["qa-fixture"]})
    if cleanup is not None:
        try:
            result = cleanup(candidate)
            if not isinstance(result, dict) or result.get("cleaned") is not True:
                raise ValueError("Fixture cleanup unconfirmed")
        except Exception:
            record({"state": "cleanup_unverified", "target": TARGETS["qa-fixture"]})
            raise
        record({"state": "cleanup_verified", "target": TARGETS["qa-fixture"]})
    return namespace


def fixture_artifact_files(raw, commit, artifact_sha256):
    """Validate archive identity/closure; caller must authenticate CI and digest."""
    if (
        not isinstance(raw, bytes)
        or not isinstance(commit, str)
        or not re.fullmatch(r"[a-f0-9]{40}", commit)
        or not isinstance(artifact_sha256, str)
        or not re.fullmatch(r"[a-f0-9]{64}", artifact_sha256)
        or sha(raw) != artifact_sha256
    ):
        raise ValueError("Fixture artifact identity mismatch")
    files = runtime_files(raw, commit)
    required = {
        "worker/bundle-manifest.json",
        "worker/wrangler.snapshot-qa.jsonc",
        "worker/python_modules/workers/__init__.py",
        "worker/python_modules/creator_loop/backend/runtime/database.py",
        "worker/public/login.html",
        "worker/public/login.js",
        "worker/public/style.css",
        "worker/public/_headers",
        "worker/public/index.html",
        "worker/public/app.js",
        "worker/public/pending.mjs",
        "worker/public/operator-grant.mjs",
        "worker/public/intake.mjs",
        "worker/public/source-review.mjs",
    }
    if not required <= files.keys():
        raise ValueError("Fixture artifact missing required files")
    try:
        tree = ast.parse(
            files["worker/python_modules/creator_loop/backend/runtime/database.py"]
        )
        declarations = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "MIGRATIONS"
                for target in node.targets
            )
        ]
        if len(declarations) != 1:
            raise ValueError()
        migrations = ast.literal_eval(declarations[0].value)
        if (
            not isinstance(migrations, tuple)
            or not migrations
            or any(
                not isinstance(name, str)
                or not re.fullmatch(r"[0-9]{4}_[a-z0-9_]+", name)
                for name in migrations
            )
            or len(set(migrations)) != len(migrations)
        ):
            raise ValueError()
        expected_migrations = {
            "worker/src/migrations/" + name + ".sql" for name in migrations
        }
        if expected_migrations != {
            name for name in files if name.startswith("worker/src/migrations/")
        }:
            raise ValueError()
        bundle = json.loads(files["worker/bundle-manifest.json"])
        items = bundle["files"]
        if not isinstance(items, list) or not items:
            raise ValueError()
        names = set()
        for item in items:
            path = item["path"]
            if not isinstance(path, str) or path in names:
                raise ValueError()
            names.add(path)
            source = (
                "worker/src/" if path == "creator_ai.mjs" else "worker/python_modules/"
            ) + path
            if source not in files or sha(files[source]) != item["sha256"]:
                raise ValueError()
        if "creator_ai.mjs" not in names or "workers/__init__.py" not in names:
            raise ValueError()
        bundled = {
            "worker/python_modules/" + name
            for name in names
            if name != "creator_ai.mjs"
        }
        if bundled != {
            name for name in files if name.startswith("worker/python_modules/")
        }:
            raise ValueError()
    except (ValueError, TypeError, KeyError, SyntaxError):
        raise ValueError("Fixture artifact bundle closure mismatch") from None
    return files


def verify_fixture_artifact_config(files, config):
    validate_fixture_config(config)
    try:
        artifact_config = json.loads(files["worker/wrangler.snapshot-qa.jsonc"])
        validate_fixture_config(artifact_config)
        if any(
            artifact_config[key] != config[key]
            for key in artifact_config
            if key != "vars"
        ) or any(
            config["vars"].get(key) != value
            for key, value in artifact_config["vars"].items()
        ):
            raise ValueError()
        if set(artifact_config["vars"]) != {
            "B_WEB_OBSERVATION_ONLY",
            "B_WEB_ISOLATED_RESTORE",
            "B_WEB_QA_READONLY_LOGIN",
            "B_WEB_QA_SNAPSHOT_EXPORT",
            "B_WEB_QA_HOSTNAME",
        }:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise ValueError("Fixture artifact configuration mismatch") from None


def verify_fixture_ci_artifact(run, jobs, workflow, listing, wrapper, commit, config):
    """Verify protected-main CI and GitHub artifact bytes from authenticated reads."""
    verify_ci(run, jobs, workflow, commit)
    if (
        not isinstance(listing, dict)
        or type(listing.get("total_count")) is not int
        or not isinstance(listing.get("artifacts"), list)
        or listing["total_count"] != len(listing["artifacts"])
    ):
        raise ValueError("Complete CI artifact listing required")
    matches = [
        a
        for a in listing["artifacts"]
        if isinstance(a, dict)
        and a.get("name") == "b-web-runtime"
        and a.get("expired") is False
    ]
    if len(matches) != 1 or not isinstance(wrapper, bytes):
        raise ValueError("One unexpired CI runtime artifact required")
    item = matches[0]
    if (
        not isinstance(run.get("id"), int)
        or not isinstance(item.get("workflow_run"), dict)
        or item["workflow_run"].get("id") != run["id"]
        or item.get("digest") != "sha256:" + sha(wrapper)
    ):
        raise ValueError("CI artifact provenance or digest mismatch")
    try:
        with zipfile.ZipFile(io.BytesIO(wrapper)) as outer:
            if (
                outer.namelist() != ["b-web-runtime.zip"]
                or outer.getinfo("b-web-runtime.zip").file_size > 64 * 1024**2
            ):
                raise ValueError("Unexpected runtime artifact wrapper")
            raw = outer.read("b-web-runtime.zip")
    except (zipfile.BadZipFile, KeyError):
        raise ValueError("Invalid runtime artifact wrapper") from None
    digest = sha(raw)
    files = fixture_artifact_files(raw, commit, digest)
    verify_fixture_artifact_config(files, config)
    return raw, digest


def read_bounded_process(command, limit, timeout):
    """Read a process with bounded stdout and process-level timeout; discard stderr."""
    if (
        type(limit) is not int
        or limit <= 0
        or type(timeout) not in (int, float)
        or timeout <= 0
    ):
        raise ValueError("Invalid process limit")
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
    except OSError:
        raise RuntimeError("Authenticated GitHub CI read failed") from None
    chunks, total, errors = [], 0, []

    def read():
        nonlocal total
        try:
            while True:
                chunk = process.stdout.read(min(65536, limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    errors.append("oversized")
                    process.kill()
                    break
                chunks.append(chunk)
        except (OSError, ValueError):
            errors.append("read")

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        status = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        reader.join(timeout=5)
        raise RuntimeError("Authenticated GitHub CI read timed out") from None
    reader.join(timeout=5)
    if reader.is_alive() or errors or status != 0:
        raise RuntimeError("Authenticated GitHub CI read failed or exceeded limit")
    return b"".join(chunks)


def fetch_fixture_ci_artifact(run_id, commit, config):
    """Read the protected-main run and artifact through the authenticated gh CLI."""
    if (
        not isinstance(run_id, str)
        or not re.fullmatch(r"[0-9]{1,20}", run_id)
        or not isinstance(commit, str)
        or not re.fullmatch(r"[a-f0-9]{40}", commit)
    ):
        raise ValueError("Exact CI run and source commit required")

    def fetch(path):
        return read_bounded_process(
            ["gh", "api", "repos/" + REPO + path], 72 * 1024**2, 45
        )

    def get_json(path):
        try:
            return json.loads(fetch(path))
        except (TypeError, ValueError):
            raise ValueError("Invalid GitHub CI metadata") from None

    run = get_json("/actions/runs/" + run_id)
    jobs = get_json("/actions/runs/" + run_id + "/jobs?per_page=100")
    workflow = get_json("/actions/workflows/ci.yml")
    listing = get_json("/actions/runs/" + run_id + "/artifacts?per_page=100")
    if get_json("/commits/main").get("sha") != commit:
        raise ValueError("Protected main changed; fixture artifact refused")
    verify_ci(run, jobs, workflow, commit)
    if (
        not isinstance(listing, dict)
        or type(listing.get("total_count")) is not int
        or not isinstance(listing.get("artifacts"), list)
        or listing["total_count"] != len(listing["artifacts"])
    ):
        raise ValueError("Complete CI artifact listing required")
    matches = [
        a
        for a in listing["artifacts"]
        if isinstance(a, dict)
        and a.get("name") == "b-web-runtime"
        and a.get("expired") is False
    ]
    if (
        len(matches) != 1
        or type(matches[0].get("id")) is not int
        or matches[0]["id"] <= 0
    ):
        raise ValueError("One unexpired CI runtime artifact required")
    wrapper = fetch("/actions/artifacts/" + str(matches[0]["id"]) + "/zip")
    return verify_fixture_ci_artifact(
        run, jobs, workflow, listing, wrapper, commit, config
    )


def fixture_cloud_observer(account, headers, config, artifact, commit, artifact_sha256):
    """Bind immutable artifact inputs before any caller may create a Worker.

    This is not permission/resource proof or a deployment fence.
    """
    if not isinstance(account, str) or sha(account.encode()) != ACCOUNT_SHA:
        raise ValueError("Wrong Cloudflare account")
    files = fixture_artifact_files(artifact, commit, artifact_sha256)
    verify_fixture_artifact_config(files, config)
    config = copy.deepcopy(config)
    headers = dict(headers)

    def precreate(current_config):
        if current_config != config:
            raise ValueError("Fixture configuration changed before creation")
        verify_fixture_artifact_config(
            fixture_artifact_files(artifact, commit, artifact_sha256), current_config
        )

    def observe(candidate, production_id, qa19_id):
        return observe_fixture_cloud(
            account,
            headers,
            config,
            production_id,
            qa19_id,
            artifact,
            commit,
            artifact_sha256,
            candidate,
        )

    observe.precreate = precreate
    return observe


def fixture_login_adapter(config, operator_key, password, request_json=None):
    """Return the fixture-only login callback; caller must first establish the fence."""
    validate_fixture_config(config)
    if (
        not isinstance(operator_key, str)
        or not operator_key
        or not isinstance(password, str)
        or not password
    ):
        raise ValueError("Fixture credentials required")
    origin = "https://" + config["vars"]["B_WEB_QA_HOSTNAME"]
    request_json = request_json or (lambda *args, **kwargs: read_json(*args, **kwargs))

    def login(candidate):
        if not isinstance(candidate, str) or not re.fullmatch(UUID, candidate):
            raise ValueError("Exact fixture version required")
        headers = {
            "Origin": origin,
            "Content-Type": "application/json",
            "X-Operator-Key": operator_key,
            "X-QA-Expected-Version": candidate,
        }
        result, response = request_json(
            origin + "/api/login", headers, {"password": password}
        )
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise ValueError("Fixture login unconfirmed")
        cookie = response.get("Set-Cookie", "").split(";", 1)[0]
        if not re.fullmatch(r"__Host-creator=[a-f0-9]{64}", cookie):
            raise ValueError("Fixture session unconfirmed")
        runtime, _ = request_json(
            origin + "/api/runtime", {**headers, "Cookie": cookie}
        )
        if not isinstance(runtime, dict) or runtime.get("deployment") != candidate:
            raise ValueError("Fixture runtime version unconfirmed")
        return {"authenticated": True, "deployment": candidate}

    return login


def inspect_fixture_worker_after_ambiguous_create(account, headers):
    """Read Worker metadata only; no response authorizes a creation retry."""
    if not isinstance(account, str) or sha(account.encode()) != ACCOUNT_SHA:
        raise ValueError("Wrong Cloudflare account")
    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        + account
        + "/workers/workers/"
        + TARGETS["qa-fixture"]
    )
    try:
        body, _ = read_json(url, headers)
    except RuntimeError as error:
        if str(error) == "HTTPS operation refused: HTTP404":
            return {"state": "not_found", "retry_permitted": False}
        raise
    result = body.get("result") if isinstance(body, dict) else None
    if (
        not isinstance(body, dict)
        or body.get("success") is not True
        or not isinstance(result, dict)
        or result.get("name") != TARGETS["qa-fixture"]
        or not isinstance(result.get("id"), str)
        or not result["id"]
    ):
        raise ValueError("Fixture Worker metadata unconfirmed")
    return {"state": "present", "worker_id": result["id"], "retry_permitted": False}


def create_fixture_worker_metadata(account, headers, config, reserve_once):
    """Create the fixed QA Worker shell once; this does not upload or deploy.

    The caller must establish the resource/permission gate before calling and
    provide a durable reservation shared by every attempt. An ambiguous failure
    never authorizes another POST, even when metadata returns 404.
    """
    validate_fixture_config(config)
    if not isinstance(account, str) or sha(account.encode()) != ACCOUNT_SHA:
        raise ValueError("Wrong Cloudflare account")
    headers = {**headers, "Content-Type": "application/json"}
    if not callable(reserve_once) or reserve_once() is not True:
        raise ValueError("Fixture creation already attempted or journal unavailable")
    url = (
        "https://api.cloudflare.com/client/v4/accounts/" + account + "/workers/workers"
    )
    try:
        body, _ = read_json(url, headers, {"name": TARGETS["qa-fixture"]})
        result = body.get("result") if isinstance(body, dict) else None
        if (
            not isinstance(body, dict)
            or body.get("success") is not True
            or not isinstance(result, dict)
            or result.get("name") != TARGETS["qa-fixture"]
            or not isinstance(result.get("id"), str)
            or not result["id"]
        ):
            raise ValueError("Unconfirmed response")
    except Exception:
        # A read is diagnostic only: neither 404 nor a matching name proves
        # the outcome of the interrupted POST or permits an automatic retry.
        observation = "unavailable"
        try:
            observation = inspect_fixture_worker_after_ambiguous_create(
                account, headers
            )["state"]
        except Exception:
            pass
        raise RuntimeError(
            "Fixture creation outcome unknown; metadata="
            + observation
            + "; do not retry"
        ) from None
    return result["id"]


def activate_fixture_worker_version(
    account, headers, config, candidate, final_check, reserve_once
):
    """Submit one fixed-fixture activation after externally established safety gates."""
    validate_fixture_config(config)
    if not isinstance(account, str) or sha(account.encode()) != ACCOUNT_SHA:
        raise ValueError("Wrong Cloudflare account")
    headers = {**headers, "Content-Type": "application/json"}
    if not isinstance(candidate, str) or not re.fullmatch(UUID, candidate):
        raise ValueError("Exact fixture version required")
    if not callable(final_check) or final_check() is not True:
        raise ValueError("Fixture activation safety checks unconfirmed")
    if not callable(reserve_once) or reserve_once() is not True:
        raise ValueError("Fixture activation already attempted or journal unavailable")
    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        + account
        + "/workers/scripts/"
        + TARGETS["qa-fixture"]
        + "/deployments"
    )
    payload = {
        "strategy": "percentage",
        "versions": [{"version_id": candidate, "percentage": 100}],
        "code_update_strategy": {"mode": "immediate"},
    }
    try:
        body, _ = read_json(url, headers, payload)
        if (
            not isinstance(body, dict)
            or body.get("success") is not True
            or not isinstance(body.get("result"), dict)
            or body["result"].get("versions") != payload["versions"]
        ):
            raise ValueError("Unconfirmed activation response")
    except Exception:
        # A subsequent metadata read is diagnostic only; it is not a fence and
        # must never authorize another activation after an ambiguous outcome.
        observed = "unavailable"
        try:
            info, _ = read_json(url, headers)
            if not isinstance(info, dict) or info.get("success") is not True:
                raise ValueError("Deployment metadata unavailable")
            versions = info["result"]["deployments"][0]["versions"]
            observed = (
                "candidate" if versions == payload["versions"] else "other_or_mixed"
            )
        except Exception:
            pass
        raise RuntimeError(
            "Fixture activation outcome unknown; metadata="
            + observed
            + "; do not retry"
        ) from None
    return candidate


def upload_fixture_worker_version(
    account,
    headers,
    config,
    artifact,
    commit,
    artifact_sha256,
    expected_worker_id,
    final_check,
    reserve_once,
):
    """Upload a version once; artifact provenance and cloud safety remain caller gates."""
    if not isinstance(account, str) or sha(account.encode()) != ACCOUNT_SHA:
        raise ValueError("Wrong Cloudflare account")
    authorization = headers.get("Authorization") if isinstance(headers, dict) else None
    if not isinstance(authorization, str) or not re.fullmatch(
        r"Bearer [^\s]+", authorization
    ):
        raise ValueError("One explicit Cloudflare bearer credential required")
    headers = {"Authorization": authorization}
    files = fixture_artifact_files(artifact, commit, artifact_sha256)
    verify_fixture_artifact_config(files, config)
    observed = inspect_fixture_worker_after_ambiguous_create(account, headers)
    if (
        not isinstance(expected_worker_id, str)
        or not expected_worker_id
        or observed.get("state") != "present"
        or observed.get("worker_id") != expected_worker_id
    ):
        raise ValueError("Fixture Worker shell not confirmed")
    if not callable(final_check) or final_check() is not True:
        raise ValueError("Fixture upload safety checks unconfirmed")
    if not callable(reserve_once) or reserve_once() is not True:
        raise ValueError("Fixture upload already attempted or journal unavailable")
    root = Path(__file__).resolve().parents[1]
    wrangler = root / "cloudflare/creator-ai/node_modules/wrangler/bin/wrangler.js"
    try:
        with tempfile.TemporaryDirectory(prefix="creator-loop-fixture-") as temp:
            destination = Path(temp)
            for name, value in files.items():
                path = destination / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(value)
            config_path = destination / "worker/wrangler.snapshot-qa.jsonc"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            result = subprocess.run(
                [
                    "node",
                    str(wrangler),
                    "versions",
                    "upload",
                    "--config",
                    str(config_path),
                    "--outdir",
                    str(destination / "uploaded"),
                ],
                cwd=destination / "worker",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=180,
                env={
                    **{
                        key: value
                        for key, value in os.environ.items()
                        if not key.upper().startswith(("CLOUDFLARE_", "CF_"))
                    },
                    "CLOUDFLARE_ACCOUNT_ID": account,
                    "CLOUDFLARE_API_TOKEN": authorization[7:],
                    "WRANGLER_SEND_METRICS": "false",
                    "WRANGLER_WRITE_LOGS": "false",
                },
            )
            output = result.stdout.decode("utf-8", errors="replace")
            matches = re.findall(r"Worker Version ID:\s*(" + UUID + r")", output)
            if result.returncode or len(matches) != 1:
                raise ValueError("Unconfirmed upload")
            return matches[0]
    except Exception:
        # A list is only diagnostic: concurrent versions cannot establish which
        # version belongs to this ambiguous attempt or justify automatic retry.
        observation = "unavailable"
        try:
            url = (
                "https://api.cloudflare.com/client/v4/accounts/"
                + account
                + "/workers/scripts/"
                + TARGETS["qa-fixture"]
                + "/versions"
            )
            value, _ = read_json(url, headers)
            if isinstance(value, dict) and value.get("success") is True:
                observation = "available"
        except Exception:
            pass
        raise RuntimeError(
            "Fixture upload outcome unknown; metadata=" + observation + "; do not retry"
        ) from None


def fixture_upload_reservation(path):
    """Reserve one fixture upload; no retries across a shared journal."""
    path = Path(path)
    if not path.is_absolute() or not path.parent.is_dir():
        raise ValueError("An absolute durable reservation path is required")

    def reserve():
        with closing(sqlite3.connect(str(path), timeout=5)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS fixture_upload_attempts (target TEXT PRIMARY KEY NOT NULL)"
            )
            try:
                db.execute(
                    "INSERT INTO fixture_upload_attempts VALUES (?)",
                    (TARGETS["qa-fixture"],),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    return reserve


def fixture_activation_reservation(path):
    """Reserve one fixture activation, independent of create and restore."""
    path = Path(path)
    if not path.is_absolute() or not path.parent.is_dir():
        raise ValueError("An absolute durable reservation path is required")

    def reserve():
        with closing(sqlite3.connect(str(path), timeout=5)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS fixture_activation_attempts (target TEXT PRIMARY KEY NOT NULL)"
            )
            try:
                db.execute(
                    "INSERT INTO fixture_activation_attempts VALUES (?)",
                    (TARGETS["qa-fixture"],),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    return reserve


def fixture_create_reservation(path):
    """Reserve the one fixture Worker creation in a shared local journal."""
    path = Path(path)
    if not path.is_absolute() or not path.parent.is_dir():
        raise ValueError("An absolute durable reservation path is required")

    def reserve():
        with closing(sqlite3.connect(str(path), timeout=5)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS fixture_create_attempts (target TEXT PRIMARY KEY NOT NULL)"
            )
            try:
                db.execute(
                    "INSERT INTO fixture_create_attempts VALUES (?)",
                    (TARGETS["qa-fixture"],),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    return reserve


def fixture_restore_reservation(path):
    """Reserve fixture restore attempts across processes sharing this SQLite file.

    The caller must keep this file on durable storage and use the same path for
    every run. This is not a Cloudflare resource reservation or distributed lock.
    """
    path = Path(path)
    if not path.is_absolute() or not path.parent.is_dir():
        raise ValueError("An absolute durable reservation path is required")

    def reserve(candidate, target, snapshot, manifest_sha256):
        if not all(
            isinstance(value, str) and re.fullmatch(pattern, value)
            for value, pattern in (
                (candidate, UUID),
                (target, r"qa-isolated-restore-[a-f0-9]{32}"),
                (snapshot, r"[a-f0-9]{32}"),
                (manifest_sha256, r"[a-f0-9]{64}"),
            )
        ):
            raise ValueError("Invalid fixture reservation identity")
        # Never store credentials, source materials or user data in this journal.
        key = sha(json.dumps([target, snapshot], separators=(",", ":")).encode())
        with closing(sqlite3.connect(str(path), timeout=5)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS fixture_restore_attempts "
                "(identity TEXT PRIMARY KEY NOT NULL, metadata TEXT NOT NULL)"
            )
            try:
                db.execute(
                    "INSERT INTO fixture_restore_attempts VALUES (?, ?)",
                    (key, sha((candidate + manifest_sha256).encode())),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    return reserve


def fixture_cleanup_adapter(config, operator_key, manifest, request_json):
    """Clean only a verified QA staging snapshot using the caller's request budget."""
    validate_fixture_config(config)
    if (
        not isinstance(operator_key, str)
        or not operator_key
        or not callable(request_json)
    ):
        raise ValueError("Fixture operator and budgeted request adapter required")
    identity = copy.deepcopy(config["vars"])
    manifest = copy.deepcopy(manifest)
    encoded = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    if not isinstance(manifest, dict) or sha(encoded) != identity.get(
        "B_WEB_QA_RESTORE_MANIFEST_SHA256"
    ):
        raise ValueError("Fixture cleanup manifest mismatch")
    origin = "https://" + identity["B_WEB_QA_HOSTNAME"]
    target = identity["B_WEB_QA_RESTORE_OBJECT_NAME"]
    snapshot = identity["B_WEB_QA_RESTORE_SNAPSHOT_ID"]

    def cleanup(candidate):
        if not isinstance(candidate, str) or not re.fullmatch(UUID, candidate):
            raise ValueError("Exact fixture version required")
        headers = {
            "Origin": origin,
            "Content-Type": "application/json",
            "X-Operator-Key": operator_key,
            "X-QA-Expected-Version": candidate,
            "X-QA-Object": "target",
        }
        result, _ = request_json(
            origin + "/api/qa-snapshot-staging",
            headers,
            {"operation": "cleanup", "snapshot_id": snapshot, "manifest": manifest},
        )
        if (
            not isinstance(result, dict)
            or result.get("qa_object") != target
            or result.get("qa_deployment") != candidate
            or result.get("cleaned") is not True
        ):
            raise ValueError("Fixture cleanup or object identity unconfirmed")
        return result

    return cleanup


def fixture_restore_adapter(
    config, operator_key, manifest, reserve_once, request_json=None
):
    """Return a single-attempt fixture restore callback, not a fence or budget proof.

    reserve_once must durably reject prior or ambiguous attempts across runs.
    The caller must verify deployment/namespace/resource gates before invocation.
    """
    validate_fixture_config(config)
    if (
        not isinstance(operator_key, str)
        or not operator_key
        or not callable(reserve_once)
    ):
        raise ValueError("Fixture operator and durable reservation required")
    config = copy.deepcopy(config)
    manifest = copy.deepcopy(manifest)
    identity = config["vars"]
    encoded = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    if not isinstance(manifest, dict) or sha(encoded) != identity.get(
        "B_WEB_QA_RESTORE_MANIFEST_SHA256"
    ):
        raise ValueError("Fixture restore manifest mismatch")
    parts = manifest.get("parts")
    if not isinstance(parts, list) or not 0 < len(parts) <= 384:
        raise ValueError("Fixture restore parts missing")
    origin = "https://" + identity["B_WEB_QA_HOSTNAME"]
    request_json = request_json or (lambda *args, **kwargs: read_json(*args, **kwargs))
    target = identity["B_WEB_QA_RESTORE_OBJECT_NAME"]
    snapshot = identity["B_WEB_QA_RESTORE_SNAPSHOT_ID"]
    restore_payload = {
        "operation": "restore",
        "snapshot_id": snapshot,
        "manifest": manifest,
    }
    if len(json.dumps(restore_payload).encode()) > 512 * 1024:
        raise ValueError("Fixture restore request exceeds body limit")
    attempted = False

    def restore(candidate):
        nonlocal attempted
        if not isinstance(candidate, str) or not re.fullmatch(UUID, candidate):
            raise ValueError("Exact fixture version required")
        headers = {
            "Origin": origin,
            "Content-Type": "application/json",
            "X-Operator-Key": operator_key,
            "X-QA-Expected-Version": candidate,
            "X-QA-Object": "target",
        }

        def request(payload):
            result, _ = request_json(
                origin + "/api/qa-snapshot-staging", headers, payload
            )
            if (
                not isinstance(result, dict)
                or result.get("qa_object") != target
                or result.get("qa_deployment") != candidate
            ):
                raise ValueError("Fixture object identity unconfirmed")
            return result

        result = request({"operation": "status", "snapshot_id": snapshot})
        if result.get("restored") is not True:
            if (
                result.get("restored") is not False
                or type(result.get("staged_parts")) is not int
                or result["staged_parts"] != len(parts)
            ):
                raise ValueError("Fixture staged parts incomplete or status unknown")
            if attempted:
                raise ValueError("Prior restore outcome unknown; no retry")
            attempted = True
            if (
                reserve_once(
                    candidate,
                    target,
                    snapshot,
                    identity["B_WEB_QA_RESTORE_MANIFEST_SHA256"],
                )
                is not True
            ):
                raise ValueError("Fixture restore reservation unconfirmed")
            result = request(restore_payload)
        if (
            result.get("restored") is not True
            or result.get("jobs_held") is not True
            or result.get("alarm_restored") is not False
        ):
            raise ValueError("Fixture held restore unconfirmed")
        return {
            "restored": True,
            "jobs_held": True,
            "alarm_restored": False,
            "target": target,
            "snapshot_id": snapshot,
            "manifest_sha256": identity["B_WEB_QA_RESTORE_MANIFEST_SHA256"],
            "deployment": candidate,
        }

    return restore


def fixture_capture_reservation(path, snapshot):
    path = Path(path)
    if (
        not path.is_absolute()
        or not path.parent.is_dir()
        or not isinstance(snapshot, str)
        or not re.fullmatch(r"[a-f0-9]{32}", snapshot)
    ):
        raise ValueError("Invalid fixture capture journal identity")

    def reserve():
        with closing(sqlite3.connect(str(path), timeout=5)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS fixture_capture_attempts (snapshot TEXT PRIMARY KEY NOT NULL)"
            )
            try:
                db.execute(
                    "INSERT INTO fixture_capture_attempts VALUES (?)", (snapshot,)
                )
            except sqlite3.IntegrityError:
                return False
        return True

    return reserve


def fixture_seed_capture_adapter(
    config,
    operator_key,
    seed_manifest,
    seed_parts,
    revision,
    checkpoint_sha256,
    reserve_restore,
    reserve_capture,
    request_json=None,
):
    """Seed a fixed fixture source and capture once; caller must establish QA safety."""
    validate_fixture_config(config)
    if (
        not isinstance(operator_key, str)
        or not operator_key
        or not isinstance(seed_manifest, dict)
        or not isinstance(seed_parts, (list, tuple))
        or type(revision) is not int
        or revision < 0
        or not isinstance(checkpoint_sha256, str)
        or not re.fullmatch(r"[a-f0-9]{64}", checkpoint_sha256)
        or not callable(reserve_restore)
        or not callable(reserve_capture)
    ):
        raise ValueError("Invalid fixture seed inputs")
    config, seed_manifest, seed_parts = (
        copy.deepcopy(config),
        copy.deepcopy(seed_manifest),
        tuple(seed_parts),
    )
    values = config["vars"]
    digest = sha(
        json.dumps(
            seed_manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    )
    parts = seed_manifest.get("parts")
    if (
        digest != values.get("B_WEB_QA_SOURCE_MANIFEST_SHA256")
        or not isinstance(parts, list)
        or not 0 < len(parts) == len(seed_parts) <= 384
    ):
        raise ValueError("Fixture seed manifest mismatch")
    for metadata, raw in zip(parts, seed_parts):
        if (
            not isinstance(metadata, dict)
            or not isinstance(raw, bytes)
            or not 0 < len(raw) <= 262144
            or type(metadata.get("bytes")) is not int
            or metadata["bytes"] != len(raw)
            or metadata.get("sha256") != sha(raw)
        ):
            raise ValueError("Fixture seed part mismatch")
    origin = "https://" + values["B_WEB_QA_HOSTNAME"]
    request_json = request_json or (lambda *args, **kwargs: read_json(*args, **kwargs))
    source = values["B_WEB_QA_SOURCE_OBJECT_NAME"]
    snapshot = values["B_WEB_QA_SOURCE_SNAPSHOT_ID"]
    export = values["B_WEB_QA_EXPORT_SNAPSHOT_ID"]

    def seed(candidate):
        if not isinstance(candidate, str) or not re.fullmatch(UUID, candidate):
            raise ValueError("Exact fixture version required")
        headers = {
            "Origin": origin,
            "Content-Type": "application/json",
            "X-Operator-Key": operator_key,
            "X-QA-Expected-Version": candidate,
            "X-QA-Object": "source",
        }

        def request(route, payload):
            result, _ = request_json(origin + route, headers, payload)
            if (
                not isinstance(result, dict)
                or result.get("qa_object") != source
                or result.get("qa_deployment") != candidate
            ):
                raise ValueError("Fixture source identity unconfirmed")
            return result

        def pinned_manifest():
            result = request(
                "/api/qa-snapshot-export",
                {"operation": "manifest", "snapshot_id": export},
            )
            manifest = result.get("manifest")
            if (
                not isinstance(manifest, dict)
                or sha(
                    json.dumps(
                        manifest,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode()
                )
                != values["B_WEB_QA_RESTORE_MANIFEST_SHA256"]
            ):
                raise ValueError("Captured fixture manifest mismatch")
            return manifest

        state = request(
            "/api/qa-snapshot-staging", {"operation": "status", "snapshot_id": snapshot}
        )
        if state.get("restored") is False:
            for position, raw in enumerate(seed_parts):
                response = request(
                    "/api/qa-snapshot-staging",
                    {
                        "operation": "put",
                        "snapshot_id": snapshot,
                        "position": position,
                        "sha256": sha(raw),
                        "content_base64": base64.b64encode(raw).decode(),
                    },
                )
                if (
                    response.get("stored") is not True
                    or type(response.get("duplicate")) is not bool
                ):
                    raise ValueError("Fixture seed part unconfirmed")
            if reserve_restore(candidate, source, snapshot, digest) is not True:
                raise ValueError(
                    "Fixture seed restore already attempted; inspect status"
                )
            state = request(
                "/api/qa-snapshot-staging",
                {
                    "operation": "restore",
                    "snapshot_id": snapshot,
                    "manifest": seed_manifest,
                },
            )
        if (
            state.get("restored") is not True
            or state.get("jobs_held") is not True
            or state.get("alarm_restored") is not False
        ):
            raise ValueError("Fixture source restore unconfirmed")
        if reserve_capture() is not True:
            return pinned_manifest()
        try:
            result = request(
                "/api/qa-snapshot-export",
                {
                    "operation": "capture",
                    "snapshot_id": export,
                    "revision": revision,
                    "checkpoint_sha256": checkpoint_sha256,
                },
            )
        except Exception:
            try:
                return pinned_manifest()
            except Exception:
                raise RuntimeError(
                    "Fixture capture outcome unknown; do not retry"
                ) from None
        captured = result.get("manifest")
        if (
            not isinstance(captured, dict)
            or sha(
                json.dumps(
                    captured, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode()
            )
            != values["B_WEB_QA_RESTORE_MANIFEST_SHA256"]
        ):
            raise ValueError("Captured fixture manifest mismatch")
        return captured

    return seed


def fixture_transfer_adapter(config, operator_key, manifest, request_json=None):
    """Transfer verified pinned fixture parts; caller must establish the cloud fence."""
    validate_fixture_config(config)
    if (
        not isinstance(operator_key, str)
        or not operator_key
        or not isinstance(manifest, dict)
    ):
        raise ValueError("Fixture transfer inputs missing")
    config, manifest = copy.deepcopy(config), copy.deepcopy(manifest)
    values = config["vars"]
    encoded = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    if sha(encoded) != values.get("B_WEB_QA_RESTORE_MANIFEST_SHA256"):
        raise ValueError("Fixture transfer manifest mismatch")
    parts = manifest.get("parts")
    if not isinstance(parts, list) or not 0 < len(parts) <= 384:
        raise ValueError("Fixture transfer parts missing")
    # Validate the complete pinned manifest before the first network request.
    # Otherwise an invalid later part can leave earlier parts staged and spend
    # requests/SQL writes despite the transfer being certain to fail.
    for item in parts:
        if (
            not isinstance(item, dict)
            or type(item.get("bytes")) is not int
            or not 0 < item["bytes"] <= 262144
            or not isinstance(item.get("sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
        ):
            raise ValueError("Invalid fixture part metadata")
    origin = "https://" + values["B_WEB_QA_HOSTNAME"]
    request_json = request_json or (lambda *args, **kwargs: read_json(*args, **kwargs))

    def transfer(candidate):
        if not isinstance(candidate, str) or not re.fullmatch(UUID, candidate):
            raise ValueError("Exact fixture version required")

        def request(role, route, payload):
            headers = {
                "Origin": origin,
                "Content-Type": "application/json",
                "X-Operator-Key": operator_key,
                "X-QA-Expected-Version": candidate,
                "X-QA-Object": role,
            }
            result, _ = request_json(origin + route, headers, payload)
            name = values[
                "B_WEB_QA_SOURCE_OBJECT_NAME"
                if role == "source"
                else "B_WEB_QA_RESTORE_OBJECT_NAME"
            ]
            if (
                not isinstance(result, dict)
                or result.get("qa_object") != name
                or result.get("qa_deployment") != candidate
            ):
                raise ValueError("Fixture transfer object identity unconfirmed")
            return result

        state = request(
            "target",
            "/api/qa-snapshot-staging",
            {
                "operation": "status",
                "snapshot_id": values["B_WEB_QA_RESTORE_SNAPSHOT_ID"],
            },
        )
        if state.get("restored") is True:
            return 0  # restore adapter independently checks the receipt and hold
        if state.get("restored") is not False:
            raise ValueError("Fixture target restore status unknown")
        source_id = values["B_WEB_QA_EXPORT_SNAPSHOT_ID"]
        remote = request(
            "source",
            "/api/qa-snapshot-export",
            {"operation": "manifest", "snapshot_id": source_id},
        )
        if remote.get("manifest") != manifest:
            raise ValueError("Fixture export manifest differs")
        for position, item in enumerate(parts):
            part = request(
                "source",
                "/api/qa-snapshot-export",
                {"operation": "part", "snapshot_id": source_id, "position": position},
            )
            try:
                content = part["content_base64"]
                if not isinstance(content, str) or len(content) > 349528:
                    raise ValueError()
                raw = base64.b64decode(content, validate=True)
                if (
                    part.get("position") != position
                    or part.get("sha256") != item["sha256"]
                    or len(raw) != item["bytes"]
                    or sha(raw) != item["sha256"]
                ):
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise ValueError("Fixture export part mismatch") from None
            stored = request(
                "target",
                "/api/qa-snapshot-staging",
                {
                    "operation": "put",
                    "snapshot_id": values["B_WEB_QA_RESTORE_SNAPSHOT_ID"],
                    "position": position,
                    "sha256": item["sha256"],
                    "content_base64": content,
                },
            )
            if (
                stored.get("stored") is not True
                or type(stored.get("duplicate")) is not bool
            ):
                raise ValueError("Fixture staging receipt unconfirmed")
        return len(parts)

    return transfer


def fixture_bootstrap_with_adapters(
    preflight,
    create,
    observe,
    record,
    config,
    operator_key,
    password,
    manifest,
    journal,
):
    """Wire the fixture restore/login adapters; callers still establish all gates.

    This does not supply a real create adapter or make the bootstrap cloud-safe.
    The journal must be the same protected durable file across all attempts.
    """
    reserve_create = fixture_create_reservation(journal)
    restore = fixture_restore_adapter(
        config, operator_key, manifest, fixture_restore_reservation(journal)
    )
    transfer = fixture_transfer_adapter(config, operator_key, manifest)
    login = fixture_login_adapter(config, operator_key, password)

    def create_once(current_config):
        if reserve_create() is not True:
            raise ValueError(
                "Prior fixture creation attempt; inspect metadata, do not retry"
            )
        return create(current_config)

    def transfer_restore(candidate):
        transfer(candidate)
        return restore(candidate)

    return fixture_bootstrap_sequence(
        preflight, create_once, observe, transfer_restore, login, record, config
    )


def fixture_phase_journal(path, binding):
    """Persist confirmed fixture identifiers; ambiguous mutations remain reserved."""
    path = Path(path)
    if (
        not path.is_absolute()
        or not path.parent.is_dir()
        or not isinstance(binding, str)
        or not re.fullmatch(r"[a-f0-9]{64}", binding)
    ):
        raise ValueError("A bound durable fixture journal is required")

    def access(phase, value=None):
        if phase not in ("worker", "candidate", "activation"):
            raise ValueError("Unknown fixture phase")
        if value is not None and (
            not isinstance(value, str)
            or not value
            or len(value) > 128
            or (phase != "worker" and not re.fullmatch(UUID, value))
        ):
            raise ValueError("Invalid confirmed fixture identifier")
        with closing(sqlite3.connect(str(path), timeout=5)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS fixture_phase_binding (id INTEGER PRIMARY KEY CHECK(id=1), digest TEXT NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO fixture_phase_binding VALUES(1,?)", (binding,)
            )
            if db.execute(
                "SELECT digest FROM fixture_phase_binding WHERE id=1"
            ).fetchone() != (binding,):
                raise ValueError("Fixture journal belongs to another configuration")
            db.execute(
                "CREATE TABLE IF NOT EXISTS fixture_phases (phase TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)"
            )
            if value is not None:
                db.execute(
                    "INSERT OR IGNORE INTO fixture_phases VALUES(?,?)", (phase, value)
                )
            row = db.execute(
                "SELECT value FROM fixture_phases WHERE phase=?", (phase,)
            ).fetchone()
            if value is not None and row != (value,):
                raise ValueError("Fixture phase result changed")
            return row[0] if row else None

    return access


def fixture_core_request_plan(target_parts, seed_parts=None):
    """Maximum app requests for two core attempts, including capture reconciliation.

    Does not cover later browser QA, manual probes, CPU, SQL rows or other traffic.
    """
    if type(target_parts) is not int or not 1 <= target_parts <= 384:
        raise ValueError("Invalid target part count")
    if seed_parts is not None and (
        type(seed_parts) is not int or not 1 <= seed_parts <= 384
    ):
        raise ValueError("Invalid seed part count")
    # Transfer: status + manifest + two requests/part; restore: two; login: two.
    core = 2 * target_parts + 6
    # Seed: status + parts + restore + capture + one unknown-capture lookup.
    if seed_parts is not None:
        core += seed_parts + 4
    return {"per_attempt": core, "max_attempts": 2, "total": 2 * core}


def fixture_full_request_forecast(
    target_parts, seed_parts, *, body_limit, cleanup, browser
):
    """Calculate a declared whole-QA upper bound, without claiming enforcement.

    Each supplemental value is the total allowed requests, including retries.
    Browser traffic must include assets and all automatic browser requests.
    Missing or unbounded values are rejected, including a boolean masquerading as 0.
    """
    extras = {"body_limit": body_limit, "cleanup": cleanup, "browser": browser}
    if any(type(value) is not int or value < 0 for value in extras.values()):
        raise ValueError("Every supplemental request bound must be a finite integer")
    core = fixture_core_request_plan(target_parts, seed_parts)
    return {
        "core": core["total"],
        **extras,
        "total": core["total"] + sum(extras.values()),
    }


def fixture_request_usage_screen(
    forecast,
    observed_workers,
    observed_do,
    pending_workers,
    pending_do,
    reserve_workers,
    reserve_do,
):
    """Screen request counts only; freshness, spend and enforcement need separate proof."""
    if not isinstance(forecast, dict) or set(forecast) != {
        "core",
        "body_limit",
        "cleanup",
        "browser",
        "total",
    }:
        raise ValueError("Complete fixture forecast required")
    values = tuple(forecast.values()) + (
        observed_workers,
        observed_do,
        pending_workers,
        pending_do,
        reserve_workers,
        reserve_do,
    )
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("Unknown or invalid request usage")
    if forecast["total"] != sum(
        forecast[k] for k in ("core", "body_limit", "cleanup", "browser")
    ):
        raise ValueError("Inconsistent fixture forecast")
    return (
        observed_workers + pending_workers + reserve_workers + forecast["total"]
        < 10_000_000
        and observed_do + pending_do + reserve_do + forecast["total"] < 1_000_000
    )


def fixture_request_budget(path, binding, origin, plan):
    """Reserve app requests before I/O across processes using the same journal.

    This local cap is not a Cloudflare spend limit or distributed deployment lock.
    """
    path = Path(path)
    full_plan = isinstance(plan, dict) and set(plan) == {
        "core",
        "body_limit",
        "cleanup",
        "browser",
        "total",
    }
    if full_plan:
        values = tuple(plan.values())
        if any(type(value) is not int or value < 0 for value in values) or (
            not 1 <= plan["core"] <= 2340
            or plan["total"]
            != sum(plan[key] for key in ("core", "body_limit", "cleanup", "browser"))
        ):
            raise ValueError("Invalid fixture request budget")
        core = plan["core"]
        plan = {"per_attempt": core // 2, "max_attempts": 2, "total": plan["total"]}
        if plan["per_attempt"] * 2 != core:
            raise ValueError("Invalid fixture request budget")
    if (
        not path.is_absolute()
        or not path.parent.is_dir()
        or not isinstance(binding, str)
        or not re.fullmatch(r"[a-f0-9]{64}", binding)
        or not isinstance(origin, str)
        or not origin.startswith("https://")
        or not isinstance(plan, dict)
        or set(plan) != {"per_attempt", "max_attempts", "total"}
        or type(plan["total"]) is not int
        or type(plan["max_attempts"]) is not int
        or type(plan["per_attempt"]) is not int
        or plan["max_attempts"] != 2
        or (not full_plan and plan["total"] != plan["per_attempt"] * 2)
        or not 1 <= plan["per_attempt"] * 2 <= 2340
    ):
        raise ValueError("Invalid fixture request budget")

    def reserve(kind):
        with closing(sqlite3.connect(str(path), timeout=5)) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS fixture_request_budget "
                "(id INTEGER PRIMARY KEY CHECK(id=1), binding TEXT NOT NULL, "
                "maximum INTEGER NOT NULL, attempts INTEGER NOT NULL, used INTEGER NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO fixture_request_budget VALUES (1,?,?,0,0)",
                (binding, plan["total"]),
            )
            row = db.execute(
                "SELECT binding,maximum,attempts,used FROM fixture_request_budget WHERE id=1"
            ).fetchone()
            if row[0] != binding or row[1] != plan["total"]:
                raise ValueError("Fixture request budget changed")
            if kind == "attempt":
                if row[2] >= plan["max_attempts"]:
                    raise ValueError("Fixture attempt budget exhausted")
                db.execute(
                    "UPDATE fixture_request_budget SET attempts=attempts+1 WHERE id=1"
                )
            else:
                if row[2] < 1 or row[3] >= row[1]:
                    raise ValueError("Fixture request budget exhausted")
                db.execute("UPDATE fixture_request_budget SET used=used+1 WHERE id=1")
            db.commit()

    def request(url, headers, data=None):
        if url not in {
            origin + "/api/login",
            origin + "/api/runtime",
            origin + "/api/qa-snapshot-export",
            origin + "/api/qa-snapshot-staging",
        }:
            raise ValueError("Unexpected fixture budget target")
        reserve("request")
        return read_json(url, headers, data)

    return lambda: reserve("attempt"), request


def fixture_bootstrap_cloud(
    account,
    headers,
    config,
    artifact,
    commit,
    artifact_sha256,
    journal,
    preflight,
    upload_check,
    activation_check,
    record,
    operator_key,
    password,
    manifest,
    seed_spec=None,
    request_usage=None,
):
    """Compose real adapters; callers must supply independent, current safety gates.

    This helper is not exposed by main. Observer still fails closed until a
    deployment fence and relevant asset proof exist. The same protected journal
    is mandatory for every process; retries after ambiguous mutations are refused.
    """
    authorization = headers.get("Authorization") if isinstance(headers, dict) else None
    if not isinstance(authorization, str) or not re.fullmatch(
        r"Bearer [^\s]+", authorization
    ):
        raise ValueError("One explicit Cloudflare bearer credential required")
    headers = {"Authorization": authorization}
    journal_binding = sha(
        json.dumps(
            {
                "account": ACCOUNT_SHA,
                "config": config,
                "commit": commit,
                "artifact": artifact_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    )
    phase = fixture_phase_journal(journal, journal_binding)
    observer = fixture_cloud_observer(
        account, headers, config, artifact, commit, artifact_sha256
    )
    frozen = copy.deepcopy(config)
    target_parts = manifest.get("parts") if isinstance(manifest, dict) else None
    if not isinstance(target_parts, list):
        raise ValueError("Fixture target part list required")
    seed_parts = None
    if seed_spec is not None:
        if not isinstance(seed_spec, dict) or not isinstance(
            seed_spec.get("parts"), (list, tuple)
        ):
            raise ValueError("Fixture seed part list required")
        seed_parts = len(seed_spec["parts"])
    # API-only sequence: cleanup once per attempt. Browser and oversized-body
    # probes remain disabled until their complete request paths are bounded.
    plan = fixture_full_request_forecast(
        len(target_parts), seed_parts, body_limit=0, cleanup=2, browser=0
    )
    start_attempt, request_json = fixture_request_budget(
        journal, journal_binding, "https://" + frozen["vars"]["B_WEB_QA_HOSTNAME"], plan
    )
    restore = fixture_restore_adapter(
        frozen,
        operator_key,
        manifest,
        fixture_restore_reservation(journal),
        request_json,
    )
    transfer = fixture_transfer_adapter(frozen, operator_key, manifest, request_json)
    login = fixture_login_adapter(frozen, operator_key, password, request_json)
    cleanup = fixture_cleanup_adapter(frozen, operator_key, manifest, request_json)
    seed = None
    if seed_spec is not None:
        if not isinstance(seed_spec, dict) or set(seed_spec) != {
            "manifest",
            "parts",
            "revision",
            "checkpoint_sha256",
        }:
            raise ValueError("Invalid fixture seed specification")
        seed = fixture_seed_capture_adapter(
            frozen,
            operator_key,
            seed_spec["manifest"],
            seed_spec["parts"],
            seed_spec["revision"],
            seed_spec["checkpoint_sha256"],
            fixture_restore_reservation(journal),
            fixture_capture_reservation(
                journal, frozen["vars"]["B_WEB_QA_EXPORT_SNAPSHOT_ID"]
            ),
            request_json,
        )

    def create(current_config):
        if current_config != frozen:
            raise ValueError("Fixture configuration changed")
        worker_id = phase("worker")
        if worker_id is None:
            worker_id = create_fixture_worker_metadata(
                account, headers, frozen, fixture_create_reservation(journal)
            )
            phase("worker", worker_id)
        else:
            observed = inspect_fixture_worker_after_ambiguous_create(account, headers)
            if (
                observed.get("state") != "present"
                or observed.get("worker_id") != worker_id
            ):
                raise ValueError("Saved fixture Worker identity unconfirmed")
        candidate = phase("candidate")
        if candidate is None:
            candidate = upload_fixture_worker_version(
                account,
                headers,
                frozen,
                artifact,
                commit,
                artifact_sha256,
                worker_id,
                upload_check,
                fixture_upload_reservation(journal),
            )
            phase("candidate", candidate)
        if not callable(activation_check):
            raise ValueError("Fixture candidate verification missing")
        active = phase("activation")
        if active is not None:
            if active != candidate or activation_check(candidate) is not True:
                raise ValueError("Saved fixture activation unconfirmed")
            url = (
                "https://api.cloudflare.com/client/v4/accounts/"
                + account
                + "/workers/scripts/"
                + TARGETS["qa-fixture"]
                + "/deployments"
            )
            info, _ = read_json(url, headers)
            if (
                not isinstance(info, dict)
                or info.get("success") is not True
                or info.get("result", {}).get("deployments", [{}])[0].get("versions")
                != [{"version_id": candidate, "percentage": 100}]
            ):
                raise ValueError("Saved fixture deployment changed")
            return candidate
        active = activate_fixture_worker_version(
            account,
            headers,
            frozen,
            candidate,
            lambda: activation_check(candidate),
            fixture_activation_reservation(journal),
        )
        phase("activation", active)
        return active

    def transfer_restore(candidate):
        if seed is not None and seed(candidate) != manifest:
            raise ValueError("Captured fixture differs from pinned target")
        transfer(candidate)
        return restore(candidate)

    def budgeted_preflight(current_config):
        result = preflight(current_config)
        if not callable(request_usage):
            raise ValueError("Fixture request usage evidence missing")
        usage = request_usage()
        required = {
            "observed_workers",
            "observed_do",
            "pending_workers",
            "pending_do",
            "reserve_workers",
            "reserve_do",
        }
        if not isinstance(usage, dict) or set(usage) != required:
            raise ValueError("Fixture request usage evidence incomplete")
        if not fixture_request_usage_screen(plan, **usage):
            raise ValueError("Fixture request resource screen closed")
        start_attempt()
        return result

    return fixture_bootstrap_sequence(
        budgeted_preflight,
        create,
        observer,
        transfer_restore,
        login,
        record,
        frozen,
        cleanup,
    )


def fixture_bootstrap_from_ci(
    account,
    headers,
    config,
    ci_run,
    commit,
    journal,
    preflight,
    upload_check,
    activation_check,
    record,
    operator_key,
    password,
    manifest,
    seed_spec=None,
    request_usage=None,
):
    """Bind the bootstrap to authenticated protected-main CI before any mutation.

    Other gates are independently required and main remains fail-closed.
    """
    artifact, digest = fetch_fixture_ci_artifact(ci_run, commit, config)
    return fixture_bootstrap_cloud(
        account,
        headers,
        config,
        artifact,
        commit,
        digest,
        journal,
        preflight,
        upload_check,
        activation_check,
        record,
        operator_key,
        password,
        manifest,
        seed_spec,
        request_usage,
    )


def observe_fixture_cloud(
    account,
    headers,
    config,
    production_id,
    qa19_id,
    artifact,
    commit,
    artifact_sha256,
    candidate,
):
    """Read management metadata and module bytes; never call the Worker or DO."""
    if not isinstance(account, str) or sha(account.encode()) != ACCOUNT_SHA:
        raise ValueError("Wrong Cloudflare account")
    if not isinstance(candidate, str) or not re.fullmatch(UUID, candidate):
        raise ValueError("Exact fixture version required")
    files = fixture_artifact_files(artifact, commit, artifact_sha256)
    verify_fixture_artifact_config(files, config)
    api = "https://api.cloudflare.com/client/v4/accounts/" + account
    script = "/workers/scripts/" + TARGETS["qa-fixture"]
    deployment_path = api + script + "/deployments"
    first, _ = read_json(deployment_path, headers)
    expected = [{"version_id": candidate, "percentage": 100}]
    if (
        not isinstance(first, dict)
        or first.get("success") is not True
        or not isinstance(first.get("result"), dict)
        or not first["result"].get("deployments")
        or first["result"]["deployments"][0].get("versions") != expected
    ):
        raise ValueError("Fixture candidate is not receiving all traffic")
    version, _ = read_json(
        api
        + "/workers/workers/"
        + TARGETS["qa-fixture"]
        + "/versions/"
        + candidate
        + "?include=modules",
        headers,
    )
    namespaces, _ = read_json(
        api + "/workers/durable_objects/namespaces?per_page=100&page=1", headers
    )
    if (
        not isinstance(version, dict)
        or version.get("success") is not True
        or not isinstance(version.get("result"), dict)
        or version["result"].get("id") != candidate
    ):
        raise ValueError("Fixture version metadata unavailable")
    verified = version["result"]
    verify_fixture_namespace_ownership(
        verified, config, production_id, qa19_id, namespaces
    )
    if verified.get("compatibility_date") != config["compatibility_date"] or sorted(
        verified.get("compatibility_flags", [])
    ) != sorted(config["compatibility_flags"]):
        raise ValueError("Fixture runtime compatibility mismatch")
    expected_modules = {}
    for name, body in files.items():
        if name.startswith("worker/python_modules/"):
            module_name = name[len("worker/") :]
        elif name.startswith("worker/src/"):
            module_name = name[len("worker/src/") :]
        else:
            continue
        if module_name in expected_modules or not isinstance(body, bytes):
            raise ValueError("Ambiguous or invalid artifact module")
        # Wrangler 4.150.0 findAdditionalModules: vendored files are Data,
        # except JS in workers/; the Python entry and configured ESModule /
        # default SQL Text rules have distinct content types.
        if module_name.startswith("python_modules/"):
            vendor = module_name[len("python_modules/") :]
            kind = (
                "application/javascript+module"
                if vendor.startswith("workers/") and vendor.endswith((".js", ".mjs"))
                else "application/octet-stream"
            )
        elif module_name == "entry.py":
            kind = "text/x-python"
        elif module_name.endswith(".mjs"):
            kind = "application/javascript+module"
        elif module_name.endswith(".sql"):
            kind = "text/plain"
        else:
            raise ValueError("Unsupported fixture module type")
        expected_modules[module_name] = (body, kind)
    modules = verified.get("modules")
    if not expected_modules or not isinstance(modules, list):
        raise ValueError("Version-bound modules missing")
    actual = {}
    for module in modules:
        if not isinstance(module, dict) or module.get("name") in actual:
            raise ValueError("Invalid version-bound module")
        name = module.get("name")
        if name not in expected_modules:
            raise ValueError("Unexpected version-bound module")
        if module.get("content_type") != expected_modules[name][1]:
            raise ValueError("Version-bound module content type mismatch")
        try:
            actual[name] = base64.b64decode(module["content_base64"], validate=True)
        except (ValueError, TypeError, KeyError):
            raise ValueError("Invalid version-bound module encoding") from None
    if actual != {name: value[0] for name, value in expected_modules.items()}:
        raise ValueError("Version-bound modules differ from artifact")
    final, _ = read_json(deployment_path, headers)
    if final != first:
        raise ValueError("Fixture deployment changed during observation")
    # Management reads cannot provide an atomic fence against later updates,
    # and static asset bytes are not verified by the modules response.
    raise ValueError("Fixture fence and static asset proof unavailable")


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def included_sql_storage_screen(storage):
    """Conservative size screen only; not proof of GB-month or pending usage."""
    return (
        0 < len(storage) < 100
        and sum(s["max"]["storedBytes"] for s in storage) + 512 * 1024**2
        < 5_000_000_000
    )


def require_legacy_qa_fence() -> None:
    """No external fence for the old QA19 constructor/alarm is proven yet."""
    raise ValueError("QA19 preflight unavailable: old runtime quiescence unproven")


def read_included_usage(account: str, headers: dict, start: str, end: str) -> dict:
    """Read full-window sums and namespace maxima without redundant partitions.

    Native settings allow the complete31day window; one bounded max query
    preserves every namespace peak without five repeated storage reads. Use the
    documented account-based policy consistently; never switch modes on error.
    """
    first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
    if (
        first.utcoffset() is None
        or last.utcoffset() is None
        or not timedelta(0) < last - first <= timedelta(days=31)
    ):
        raise ValueError("Invalid full accounting window")
    selections = {
        "workersInvocationsAdaptive": "(limit:1){sum{requests cpuTimeUs}}",
        "durableObjectsInvocationsAdaptiveGroups": "(limit:1){sum{requests}}",
        "durableObjectsPeriodicGroups": "(limit:1){sum{duration rowsRead rowsWritten}}",
        "durableObjectsSqlStorageGroups": "(limit:10){dimensions{namespaceId} max{storedBytes}}",
    }
    sum_fields = {
        "workersInvocationsAdaptive": {"requests", "cpuTimeUs"},
        "durableObjectsInvocationsAdaptiveGroups": {"requests"},
        "durableObjectsPeriodicGroups": {"duration", "rowsRead", "rowsWritten"},
    }
    started = time.monotonic()
    previous = None
    completed_reads = 0
    # Keep only already-validated account-wide aggregates for diagnostics.
    # They never make an incomplete observation eligible for the guard.
    usage = {}
    # Apply this official read policy only to GraphQL, not caller REST headers.
    headers = {**headers, "X-Rate-Limit-Type": "account-based"}

    def observe(node, query, variables):
        nonlocal previous, completed_reads
        current = time.monotonic()
        delay = 0 if previous is None else max(0, 2.0 - (current - previous))
        if current - started + delay > 120:
            raise ValueError("Included-resource observation deadline exceeded")
        if delay:
            time.sleep(delay)
        previous = time.monotonic()
        value, _ = read_json(
            "https://api.cloudflare.com/client/v4/graphql",
            headers,
            {"query": query, "variables": variables},
        )
        if value.get("errors"):
            codes = []
            kinds = []
            for error in value["errors"][:8]:
                code = error.get("extensions", {}).get("code")
                codes.append(
                    code
                    if type(code) is str
                    and code in {"budget", "rate_limit", "limits", "bad_query"}
                    else "unknown"
                )
                message = error.get("message")
                message = message.casefold() if isinstance(message, str) else ""
                # Public artifacts retain only categories, never provider text
                # that can reflect account IDs, auth headers or private data.
                kinds.append(
                    "rate_limit"
                    if "rate limit" in message or "rate limiter" in message
                    else "query_resources"
                    if "excessive resources" in message
                    else "scope_combination"
                    if "too many nodes, zones and accounts" in message
                    else "unknown"
                )
            Path("included-resource-error.json").write_text(
                json.dumps(
                    {
                        "node": node,
                        "codes": codes,
                        "kinds": kinds,
                        "attempt": completed_reads + 1,
                        "mode": "account-based",
                        "window": {
                            k: variables[k] for k in ("s", "e") if k in variables
                        },
                        "model_calls": 0,
                        "guard_pass": False,
                        "completed_aggregates": {
                            name: dict(rows[0]["sum"])
                            for name, rows in usage.items()
                            if name in sum_fields
                        },
                    }
                )
            )
            raise ValueError("Cannot verify included resources: " + node)
        accounts = value["data"]["viewer"]["accounts"]
        if not isinstance(accounts, list) or len(accounts) != 1:
            raise ValueError("Included-resource account observation missing")
        completed_reads += 1
        return accounts[0]

    settings = observe(
        "settings",
        "query($a:string){viewer{accounts(filter:{accountTag:$a}){settings{"
        + "".join(node + "{enabled maxDuration notOlderThan}" for node in selections)
        + "}}}}",
        {"a": account},
    ).get("settings")
    if not isinstance(settings, dict):
        raise ValueError("Included-resource dataset settings missing")
    for node in selections:
        limits = settings.get(node)
        if (
            not isinstance(limits, dict)
            or limits.get("enabled") is not True
            or any(
                type(limits.get(k)) is not int or limits[k] <= 0
                for k in ("maxDuration", "notOlderThan")
            )
            or (last - first).total_seconds() > limits["maxDuration"]
            or datetime.now(first.tzinfo).timestamp() - first.timestamp()
            > limits["notOlderThan"]
        ):
            raise ValueError("Included-resource dataset window unavailable: " + node)
    for node, selection in selections.items():
        selection = selection.replace(
            "){", ",filter:{datetime_geq:$s,datetime_leq:$e}){", 1
        )
        query = (
            "query($a:string,$s:string,$e:string){viewer{accounts(filter:{accountTag:$a}){"
            + node
            + selection
            + "}}}"
        )
        if node.endswith("SqlStorageGroups"):
            rows = observe(node, query, {"a": account, "s": start, "e": end}).get(node)
            if not isinstance(rows, list) or not 0 < len(rows) < 10:
                raise ValueError("Included-resource observation absent or truncated")
            seen = set()
            for row in rows:
                namespace = row["dimensions"]["namespaceId"]
                value = row["max"]["storedBytes"]
                if not isinstance(namespace, str) or not namespace or namespace in seen:
                    raise ValueError("Invalid storage namespace observation")
                if (
                    type(value) not in (int, float)
                    or not math.isfinite(value)
                    or value < 0
                ):
                    raise ValueError("Invalid included-resource usage")
                seen.add(namespace)
        else:
            rows = observe(node, query, {"a": account, "s": start, "e": end}).get(node)
            if not isinstance(rows, list) or len(rows) != 1:
                raise ValueError("Included-resource observation absent or truncated")
            for row in rows:
                if set(row["sum"]) != sum_fields[node]:
                    raise ValueError("Included-resource metric missing")
                if any(
                    type(v) not in (int, float) or not math.isfinite(v) or v < 0
                    for v in row["sum"].values()
                ):
                    raise ValueError("Invalid included-resource usage")
        usage[node] = rows
    if time.monotonic() - started > 120:
        raise ValueError("Included-resource observation deadline exceeded")
    return usage


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


def require_idle_jobs(state):
    # The UI list is capped and excludes diagnostic jobs. Never infer global
    # quiescence from it; older runtimes lack this exhaustive count and fail closed.
    count = state.get("active_job_count")
    if type(count) is not int or count != 0:
        raise ValueError(
            "Complete active job count unavailable or nonzero; no deployment"
        )


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


def observe_activated_runtime(app, current_id, version, *, sleep=time.sleep):
    """Bound propagation reads only; never repeat activation or mask a new version."""
    for attempt in range(8):
        if current_id() != version:
            raise ValueError("Deployment changed during propagation observation")
        runtime = app("/api/runtime")
        if runtime["model"] != MODEL:
            raise ValueError("Live model differs from trusted runtime")
        if runtime["deployment"] == version:
            return runtime
        if attempt < 7:
            sleep(8)
    raise ValueError(
        "Deployment occurred but live observation not confirmed; read before any retry"
    )


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


def fixture_first_create_preflight(run, commit):
    """Read authenticated main CI artifact; never create or contact an object.

    Presence of credentials is not proof of their scope or of resource budget.
    Callers must still establish those facts and a deployment fence before use.
    """
    root = Path(__file__).resolve().parents[1]
    config = json.loads(
        (root / "cloudflare/creator-web/wrangler.snapshot-qa.jsonc").read_text(
            encoding="utf-8"
        )
    )
    validate_fixture_config(config)
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    if sha(account.encode()) != ACCOUNT_SHA:
        raise ValueError("Wrong or missing Cloudflare account")
    if not os.environ.get("CLOUDFLARE_API_TOKEN", "").strip():
        raise ValueError("Cloudflare credential missing")
    fetch_fixture_ci_artifact(run, commit, config)


def main():
    if (
        os.environ["GITHUB_REPOSITORY"] != REPO
        or os.environ["GITHUB_REF"] != "refs/heads/main"
    ):
        raise ValueError("Protected main workflow only")
    workflow_commit = os.environ["GITHUB_SHA"]
    operation = os.environ["B_OPERATION"]
    commit = (
        os.environ["B_VERIFY_SOURCE_COMMIT"]
        if operation == "verify"
        else workflow_commit
    )
    run = os.environ["B_CI_RUN"]
    if not re.fullmatch(r"[a-f0-9]{40}", commit) or not re.fullmatch(
        r"[0-9]{1,20}", run
    ):
        raise ValueError("Exact commit/run needed")
    target = os.environ["B_TARGET"]
    worker = TARGETS[target]
    if operation not in ("deploy", "rollback", "verify"):
        raise ValueError("Explicit operation needed")
    if target == "qa-fixture":
        if operation != "deploy":
            raise ValueError("Fixture first-create requires deploy operation")
        fixture_first_create_preflight(run, commit)
        raise ValueError(
            "Fixture bootstrap unavailable: resource, permission and deployment fence proofs required"
        )
    expected = os.environ.get("B_EXPECTED_DEPLOYMENT", "")
    if not re.fullmatch(UUID, expected):
        raise ValueError("Explicit operation and current deployment needed")
    if target == "production":
        # The installed production DO can rearm work upon any cold request.
        # Its capped state response also cannot prove a queue fence. Refuse
        # before login/state until a reviewed non-activating observation and
        # quiescence mechanism are available; do not bypass via a flag.
        raise ValueError(
            "Production preflight unavailable: non-activating job observation and quiescence required"
        )
    if target == "qa19":
        # The currently deployed QA19 runtime can rearm old work on login.
        # Resource totals, an empty recent-jobs page, or a config flag cannot
        # establish that its old alarms and in-flight invocations are fenced.
        require_legacy_qa_fence()

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
            receipt.get("operation") in ("deploy", "verify")
            and receipt.get("live_runtime_confirmed") is True
            and receipt.get("deployment") == requested_version
            and receipt.get("target") == target
            and receipt.get("account_sha256") == ACCOUNT_SHA
            and receipt.get("workflow_run") == run_id
            and receipt.get("remote_bytes_verified") is True
            and receipt.get("workflow_commit", receipt.get("source_commit"))
            == meta["head_sha"]
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
        if operation == "verify" and data is not None:
            raise ValueError("Verification cannot mutate Cloudflare resources")
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
    # The operator supplies only an already-observed included-resource proof.
    # Re-read relevant account usage here; never rely on its age/value alone.
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    usage = read_included_usage(
        account,
        headers,
        (now - timedelta(days=31)).isoformat(),
        now.isoformat(),
    )
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
        and included_sql_storage_screen(storage)
    ):
        raise ValueError("Included-resource guard closed")
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
    require_idle_jobs(before)

    def final_preconditions():
        # Upload/build can take minutes. No traffic mutation occurs until these
        # fresh reads. CF activation has no CAS; this is not atomic quiescence.
        if github("/commits/main")["sha"] != commit or current_id() != expected:
            raise ValueError(
                "Main/deployment changed; inactive candidate not activated"
            )
        compatible_bindings(previous_version, cloud(script + "/versions/" + expected))
        state = app("/api/state")
        require_idle_jobs(state)
        return state

    record = {
        "execution": "live",
        "provider": "cloudflare",
        "component": "B",
        "operation": operation,
        "source_commit": commit,
        "workflow_commit": workflow_commit,
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
    elif operation == "verify":
        # Qualify the already-active version from trusted push-main CI bytes.
        # A separate successful receipt never rewrites an earlier failed run.
        files, archive_hash, artifact_digest = trusted_runtime(run, commit)
        version = expected
        record.update(
            runtime_sha256=archive_hash,
            artifact_digest=artifact_digest,
            runtime_files={name: sha(value) for name, value in files.items()},
            activation_attempted=False,
        )
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
        expected_binding_types(target, previous_version, config)
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

    if operation != "verify":
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
    observe_activated_runtime(app, current_id, record["deployment"])
    if app("/api/state")["revision"] < before["revision"]:
        raise ValueError("Domain revision regressed")
    config = json.loads(files["worker/wrangler.jsonc"])
    settings = cloud(script + "/settings")
    binding_types = expected_binding_types(target, previous_version, config)
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
