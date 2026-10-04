"""Minimal Windows desktop launcher; --smoke validates packaged execution."""

import argparse
import json
import sqlite3
import sys
import zipfile
from contextlib import closing
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from . import __version__
from .app_lock import AppDataLock, DataRootBusy
from .database import initialize, open_readonly, validate
from .paths import data_root, ensure_data_root

MAINTENANCE_REQUESTED = 20
COMPONENTS_REQUESTED = 21


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--play-media":
        from .playback_child import run_playback_worker

        return run_playback_worker(Path(sys.argv[2]))
    # Private owned decoder entry point precedes all DB/root initialization.
    if len(sys.argv) == 3 and sys.argv[1] == "--decode-media":
        from .isolated_decode import run_decode_worker

        return run_decode_worker(Path(sys.argv[2]))
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply-completed-copy-restore",
        type=Path,
        help="Distinct confirmed fresh copy; retains current bundle and keeps launch guarded",
    )
    parser.add_argument(
        "--reviewed-fresh-restore",
        help="Fresh selected backup/current bundle review identity",
    )
    parser.add_argument(
        "--confirm-fresh-restore",
        action="store_true",
        help="Explicit consent to a new copy of the selected bound backup",
    )
    parser.add_argument(
        "--review-completed-copy-restore",
        type=Path,
        help="Read a distinct explicit fresh restore choice for a changed completed guarded copy; no apply",
    )
    parser.add_argument(
        "--fresh-restore-backup",
        help="Explicit bound backup ID for fresh completed-copy restore",
    )
    parser.add_argument(
        "--review-damaged-restore",
        help="Read current damaged source, explicit backup/candidate and optional raw/preparation proofs; no apply",
    )
    parser.add_argument("--review-raw-source", type=Path)
    parser.add_argument("--review-damaged-preparation", type=Path)
    parser.add_argument(
        "--review-corrupt-copy",
        type=Path,
        help="Read actual guarded copy, bound backup time/candidate and current media; no apply",
    )
    parser.add_argument(
        "--confirm-preserve-unknown",
        action="store_true",
        help="Explicitly retain all inspected unknown partial DB/sidecars before a new guarded copy",
    )
    parser.add_argument(
        "--resume-corrupt-copy",
        type=Path,
        help="Explicit continuation of an inspected interrupted copy; keeps launch guarded",
    )
    parser.add_argument(
        "--confirm-keep-partial",
        action="store_true",
        help="Explicit consent to retain an existing empty partial target separately",
    )
    parser.add_argument(
        "--recover-corrupt-copy",
        type=Path,
        help="Recover an explicitly reviewed actually validated guarded copy with fresh health",
    )
    parser.add_argument(
        "--reviewed-inspection", help="Current inspection state proof SHA256"
    )
    parser.add_argument(
        "--confirm-recovery",
        action="store_true",
        help="Explicit consent to compatible activation and fresh recovery health",
    )
    parser.add_argument(
        "--inspect-corrupt-copy",
        type=Path,
        help="Inspect guarded copy actual bytes/positions without changing data or guard",
    )
    parser.add_argument(
        "--copy-corrupt-restore",
        type=Path,
        help="Guarded confirmed DB replacement; requires explicit health recovery before launch",
    )
    parser.add_argument("--verify-corrupt-preparation", type=Path)
    parser.add_argument(
        "--reviewed-preparation", help="SHA256 of the selected preparation manifest"
    )
    parser.add_argument(
        "--prepare-corrupt-restore",
        help="Stage a reviewed backup separately after damaged-source retention; no apply",
    )
    parser.add_argument("--raw-source-manifest", type=Path)
    parser.add_argument(
        "--verify-preserved-source",
        type=Path,
        help="Revalidate an explicit raw-source archive; no live DB assessment or restore",
    )
    parser.add_argument(
        "--reviewed-damage",
        help="Explicit damaged-source identity for the selected archive",
    )
    parser.add_argument(
        "--preserve-corrupt-source",
        help="Retain reviewed damaged DB/sidecar bytes separately; no restore",
    )
    parser.add_argument(
        "--inspect-corrupt-restore",
        help="Review damaged SQLite bytes and an explicit backup; no restore",
    )
    parser.add_argument(
        "--inspect-components",
        action="store_true",
        help="Read saved component declarations; not a fresh file check",
    )
    parser.add_argument(
        "--check-components",
        type=Path,
        help="Verify selected local component artifacts without opening the user DB",
    )
    parser.add_argument(
        "--select-components",
        type=Path,
        help="Verify and save an external selection with the app closed; no installation",
    )
    parser.add_argument(
        "--reviewed-components",
        help="Bind saved selections to the reviewed declarations",
    )
    parser.add_argument(
        "--components",
        action="store_true",
        help="Open the local component selection window",
    )
    parser.add_argument(
        "--smoke", action="store_true", help="Create/open a DB and exit"
    )
    parser.add_argument(
        "--ui-smoke", action="store_true", help="Open the Library UI briefly and exit"
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Validate a DB-only backup with the app closed",
    )
    parser.add_argument(
        "--maintenance",
        action="store_true",
        help="Open backup/update/restore UI without opening the user DB",
    )
    parser.add_argument(
        "--stage-update", type=Path, help="Verify and stage a supplied ZIP"
    )
    parser.add_argument(
        "--prepare-update",
        type=Path,
        help="Backup, stage and migrate; activation pending",
    )
    parser.add_argument("--release-manifest", type=Path)
    parser.add_argument("--installation-root", type=Path)
    parser.add_argument(
        "--inspect-restore",
        help="Assess an explicit DB-only backup ID without restoring",
    )
    parser.add_argument(
        "--restore-candidate",
        type=Path,
        help="Explicit staged candidate for restore compatibility assessment",
    )
    parser.add_argument(
        "--apply-restore", help="Restore an explicitly reviewed backup ID"
    )
    parser.add_argument(
        "--reviewed-restore", help="Assessment identity shown during review"
    )
    parser.add_argument(
        "--confirm-lost-changes",
        action="store_true",
        help="Explicitly accept losing all DB changes since this backup",
    )
    parser.add_argument(
        "--confirm-media-issues",
        action="store_true",
        help="Explicitly acknowledge the assessed missing/changed media",
    )
    parser.add_argument(
        "--recover-restore",
        type=Path,
        help="Resolve actual outcome of a confirmed interrupted restore",
    )
    parser.add_argument(
        "--inspect-update",
        type=Path,
        help="Inspect interrupted update state without mutation",
    )
    parser.add_argument(
        "--resume-update",
        type=Path,
        help="Back up current state and explicitly resume an interrupted update",
    )
    parser.add_argument(
        "--repair-update-metadata",
        type=Path,
        help="Recheck a completed update and repair its coordination metadata",
    )
    parser.add_argument(
        "--launch-managed",
        action="store_true",
        help="Launch the health-validated active installation",
    )
    parser.add_argument(
        "--compatible-only",
        action="store_true",
        help="Require existing compatible schema without migration",
    )
    parser.add_argument(
        "--activate-update",
        type=Path,
        help="Activate a prepared journal and health-check",
    )
    parser.add_argument(
        "--health-check",
        action="store_true",
        help="Read-only schema/storage health; no migration or UI",
    )
    args = parser.parse_args()
    if args.apply_completed_copy_restore is not None:
        if (
            args.fresh_restore_backup is None
            or args.installation_root is None
            or args.restore_candidate is None
            or args.reviewed_fresh_restore is None
            or not args.confirm_fresh_restore
            or not args.confirm_lost_changes
            or any(
                value
                for name, value in vars(args).items()
                if name
                not in (
                    "apply_completed_copy_restore",
                    "fresh_restore_backup",
                    "installation_root",
                    "restore_candidate",
                    "reviewed_fresh_restore",
                    "confirm_fresh_restore",
                    "confirm_lost_changes",
                    "confirm_media_issues",
                )
            )
        ):
            parser.error(
                "Fresh copy requires only explicit journal/backup/candidate/installation/fresh review and fresh/loss consent"
            )
        from .corrupt_copy_recovery import _record
        from .fresh_restore_apply import copy_fresh_restore

        try:
            journal = copy_fresh_restore(
                data_root(),
                args.apply_completed_copy_restore,
                args.fresh_restore_backup,
                args.installation_root,
                args.restore_candidate,
                reviewed_fresh_restore=args.reviewed_fresh_restore,
                confirm_fresh_restore=args.confirm_fresh_restore,
                confirm_lost_changes=args.confirm_lost_changes,
                confirm_media_issues=args.confirm_media_issues,
            )
            receipt = _record(journal)
            print(
                json.dumps(
                    {
                        "journal_name": journal.name,
                        "phase": receipt["phase"],
                        "activated": False,
                        "restored": False,
                        "guard_retained": True,
                    }
                )
            )
            return 0
        except DataRootBusy as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(
                f"Fresh copy refused: {type(exc).__name__}; keep guard and all current/original evidence",
                file=sys.stderr,
            )
            return 4
    if args.reviewed_fresh_restore is not None or args.confirm_fresh_restore:
        parser.error("Fresh review/consent requires --apply-completed-copy-restore")
    if args.review_completed_copy_restore is not None:
        if (
            args.fresh_restore_backup is None
            or args.installation_root is None
            or args.restore_candidate is None
            or any(
                value
                for name, value in vars(args).items()
                if name
                not in (
                    "review_completed_copy_restore",
                    "fresh_restore_backup",
                    "installation_root",
                    "restore_candidate",
                )
            )
        ):
            parser.error(
                "Fresh review requires only explicit copy journal/backup/candidate/installation; no consent or apply"
            )
        from .fresh_restore_review import review_fresh_restore

        try:
            print(
                json.dumps(
                    review_fresh_restore(
                        data_root(),
                        args.review_completed_copy_restore,
                        args.fresh_restore_backup,
                        args.installation_root,
                        args.restore_candidate,
                    )
                )
            )
            return 0
        except DataRootBusy as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(
                f"Fresh restore review refused: {type(exc).__name__}; keep current/original bytes and guard",
                file=sys.stderr,
            )
            return 4
    if args.fresh_restore_backup is not None:
        parser.error("Fresh backup choice requires --review-completed-copy-restore")
    if args.review_damaged_restore is not None:
        if (
            args.installation_root is None
            or args.restore_candidate is None
            or any(
                value
                for name, value in vars(args).items()
                if name
                not in (
                    "review_damaged_restore",
                    "installation_root",
                    "restore_candidate",
                    "review_raw_source",
                    "review_damaged_preparation",
                )
            )
        ):
            parser.error(
                "Damaged review requires only explicit backup/candidate/installation and optional raw/preparation"
            )
        from .damaged_restore_review import review_damaged_restore

        try:
            print(
                json.dumps(
                    review_damaged_restore(
                        data_root(),
                        args.review_damaged_restore,
                        args.installation_root,
                        args.restore_candidate,
                        raw_manifest=args.review_raw_source,
                        preparation_manifest=args.review_damaged_preparation,
                    )
                )
            )
            return 0
        except DataRootBusy as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(
                f"Damaged restore review refused: {type(exc).__name__}; keep all current/raw/stage/backup bytes",
                file=sys.stderr,
            )
            return 4
    if (
        args.review_raw_source is not None
        or args.review_damaged_preparation is not None
    ):
        parser.error("Raw/preparation review flags require --review-damaged-restore")
    if args.review_corrupt_copy is not None:
        if args.installation_root is None or any(
            value
            for name, value in vars(args).items()
            if name not in ("review_corrupt_copy", "installation_root")
        ):
            parser.error("Copy review requires only explicit journal and installation")
        from .corrupt_copy_review import review_corrupt_copy

        try:
            print(
                json.dumps(
                    review_corrupt_copy(
                        data_root(), args.review_corrupt_copy, args.installation_root
                    )
                )
            )
            return 0
        except DataRootBusy as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(
                f"Copy review refused: {type(exc).__name__}; keep guard and all evidence",
                file=sys.stderr,
            )
            return 4
    if args.reviewed_inspection is not None and (
        args.recover_corrupt_copy is None and args.resume_corrupt_copy is None
    ):
        parser.error(
            "Inspection review requires --recover-corrupt-copy or --resume-corrupt-copy"
        )
    if args.confirm_recovery and args.recover_corrupt_copy is None:
        parser.error("Recovery consent requires --recover-corrupt-copy")
    if args.confirm_keep_partial and args.resume_corrupt_copy is None:
        parser.error("Partial retention consent requires --resume-corrupt-copy")
    if args.confirm_preserve_unknown and args.resume_corrupt_copy is None:
        parser.error("Unknown partial consent requires --resume-corrupt-copy")
    if args.resume_corrupt_copy is not None:
        if (
            args.reviewed_inspection is None
            or args.installation_root is None
            or not args.confirm_lost_changes
            or any(
                value
                for name, value in vars(args).items()
                if name
                not in (
                    "resume_corrupt_copy",
                    "reviewed_inspection",
                    "installation_root",
                    "confirm_lost_changes",
                    "confirm_keep_partial",
                    "confirm_preserve_unknown",
                    "confirm_media_issues",
                )
            )
        ):
            parser.error(
                "Copy continuation requires only journal/inspection/installation/loss and optional partial/media consent"
            )
        from .corrupt_copy_resume import resume_corrupt_copy

        try:
            journal = resume_corrupt_copy(
                data_root(),
                args.resume_corrupt_copy,
                args.installation_root,
                reviewed_inspection=args.reviewed_inspection,
                confirm_lost_changes=args.confirm_lost_changes,
                confirm_keep_partial=args.confirm_keep_partial,
                confirm_preserve_unknown=args.confirm_preserve_unknown,
                confirm_media_issues=args.confirm_media_issues,
            )
            print(
                json.dumps(
                    {
                        "journal_name": journal.name,
                        "activated": False,
                        "restored": False,
                        "guard_retained": True,
                    }
                )
            )
            return 0
        except DataRootBusy as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(
                f"Copy continuation refused: {type(exc).__name__}; keep guard and all original/partial evidence",
                file=sys.stderr,
            )
            return 4
    if args.recover_corrupt_copy is not None:
        if (
            args.reviewed_inspection is None
            or args.installation_root is None
            or not args.confirm_recovery
            or any(
                value
                for name, value in vars(args).items()
                if name
                not in (
                    "recover_corrupt_copy",
                    "reviewed_inspection",
                    "installation_root",
                    "confirm_recovery",
                )
            )
        ):
            parser.error(
                "Corrupt recovery requires only explicit journal/inspection/installation/consent"
            )
        from .corrupt_copy_recovery import recover_corrupt_copy

        try:
            candidate = recover_corrupt_copy(
                data_root(),
                args.recover_corrupt_copy,
                args.installation_root,
                reviewed_inspection=args.reviewed_inspection,
                confirm_recovery=args.confirm_recovery,
            )
            print(
                json.dumps(
                    {
                        "candidate_name": candidate.name,
                        "activated": True,
                        "restored": True,
                        "guard_retained": False,
                    }
                )
            )
            return 0
        except DataRootBusy as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(
                f"Corrupt recovery refused: {type(exc).__name__}; keep evidence and inspect actual guard/state",
                file=sys.stderr,
            )
            return 4
    if args.inspect_corrupt_copy is not None:
        if any(
            value
            for name, value in vars(args).items()
            if name != "inspect_corrupt_copy"
        ):
            parser.error("Corrupt copy inspection requires only an explicit journal")
        from .corrupt_restore_inspection import inspect_corrupt_copy

        try:
            print(
                json.dumps(inspect_corrupt_copy(data_root(), args.inspect_corrupt_copy))
            )
            return 0
        except DataRootBusy as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(
                f"Corrupt copy inspection refused: {type(exc).__name__}; keep guard and all evidence",
                file=sys.stderr,
            )
            return 4
    if args.copy_corrupt_restore is not None:
        if (
            args.reviewed_preparation is None
            or args.reviewed_restore is None
            or args.installation_root is None
            or args.restore_candidate is None
            or not args.confirm_lost_changes
            or any(
                value
                for name, value in vars(args).items()
                if name
                not in (
                    "copy_corrupt_restore",
                    "reviewed_preparation",
                    "reviewed_restore",
                    "installation_root",
                    "restore_candidate",
                    "confirm_lost_changes",
                    "confirm_media_issues",
                )
            )
        ):
            parser.error(
                "Guarded corrupt copy requires only explicit preparation/review/candidate/installation/loss consent"
            )
        from .corrupt_restore_copy import copy_corrupt_restore

        try:
            root = data_root()
            journal = copy_corrupt_restore(
                root,
                args.copy_corrupt_restore,
                args.installation_root,
                args.restore_candidate,
                reviewed_preparation=args.reviewed_preparation,
                reviewed_identity=args.reviewed_restore,
                confirm_lost_changes=args.confirm_lost_changes,
                confirm_media_issues=args.confirm_media_issues,
            )
            print(
                json.dumps(
                    {
                        "corrupt_restore_journal": str(journal.relative_to(root)),
                        "phase": "CORRUPT_DB_COMMITTED_GUARDED",
                        "requires_recovery_health": True,
                        "activated": False,
                        "restored": False,
                    }
                )
            )
            return 0
        except DataRootBusy as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(
                f"Guarded corrupt copy refused: {type(exc).__name__}; keep guard/DB/sidecars/backups/partial evidence",
                file=sys.stderr,
            )
            return 4
    if (
        args.verify_corrupt_preparation is not None
        or args.reviewed_preparation is not None
    ):
        if (
            args.verify_corrupt_preparation is None
            or args.reviewed_preparation is None
            or any(
                value
                for name, value in vars(args).items()
                if name not in ("verify_corrupt_preparation", "reviewed_preparation")
            )
        ):
            parser.error(
                "Stage verification requires only explicit manifest and reviewed digest"
            )
        from .corrupt_stage_validation import hold_corrupt_preparation

        try:
            with hold_corrupt_preparation(
                data_root(), args.verify_corrupt_preparation, args.reviewed_preparation
            ) as record:
                receipt = {
                    key: record[key]
                    for key in (
                        "stage_revalidated",
                        "preparation_manifest_sha256",
                        "schema_to",
                        "current_source_assessed",
                        "apply_authorized",
                        "activated",
                        "restored",
                    )
                }
            print(json.dumps(receipt))
            return 0
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(
                f"Stage verification refused: {type(exc).__name__}; keep all evidence",
                file=sys.stderr,
            )
            return 4
    if args.prepare_corrupt_restore is not None or args.raw_source_manifest is not None:
        if (
            args.prepare_corrupt_restore is None
            or args.raw_source_manifest is None
            or args.restore_candidate is None
            or args.installation_root is None
            or args.reviewed_restore is None
            or not args.confirm_lost_changes
            or any(
                value
                for name, value in vars(args).items()
                if name
                not in (
                    "prepare_corrupt_restore",
                    "raw_source_manifest",
                    "restore_candidate",
                    "installation_root",
                    "reviewed_restore",
                    "confirm_lost_changes",
                    "confirm_media_issues",
                )
            )
        ):
            parser.error(
                "Corrupt staging requires explicit backup/candidate/archive/review/loss consent only"
            )
        from .corrupt_restore_preparation import prepare_corrupt_restore

        try:
            root = data_root()
            manifest = prepare_corrupt_restore(
                root,
                args.prepare_corrupt_restore,
                args.installation_root,
                args.restore_candidate,
                args.raw_source_manifest,
                reviewed_identity=args.reviewed_restore,
                confirm_lost_changes=args.confirm_lost_changes,
                confirm_media_issues=args.confirm_media_issues,
            )
            print(
                json.dumps(
                    {
                        "corrupt_restore_preparation": str(manifest.relative_to(root)),
                        "raw_source_preserved": True,
                        "apply_authorized": False,
                        "activated": False,
                        "restored": False,
                    }
                )
            )
            return 0
        except DataRootBusy as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(
                f"Corrupt staging refused: {type(exc).__name__}; keep DB/sidecars/backups/partial evidence",
                file=sys.stderr,
            )
            return 4
    if args.verify_preserved_source is not None or args.reviewed_damage is not None:
        if (
            args.verify_preserved_source is None
            or args.reviewed_damage is None
            or any(
                value
                for name, value in vars(args).items()
                if name not in ("verify_preserved_source", "reviewed_damage")
            )
        ):
            parser.error(
                "Raw-source verification requires only an explicit manifest and reviewed damage identity"
            )
        from .preserved_source_validation import hold_preserved_source

        try:
            with hold_preserved_source(
                data_root(), args.verify_preserved_source, args.reviewed_damage
            ) as verified:
                receipt = {
                    **verified,
                    "current_source_assessed": False,
                    "restored": False,
                }
            print(json.dumps(receipt))
            return 0
        except (OSError, ValueError, RuntimeError) as exc:
            print(
                f"Raw-source verification refused: {type(exc).__name__}; keep archive/live DB/sidecars/backups",
                file=sys.stderr,
            )
            return 4
    if args.preserve_corrupt_source is not None:
        if any(
            value
            for name, value in vars(args).items()
            if name != "preserve_corrupt_source"
        ):
            parser.error(
                "Raw-source preservation cannot be combined with other operations"
            )
        from .corrupt_source_preservation import preserve_corrupt_source

        try:
            root = data_root()
            manifest = preserve_corrupt_source(root, args.preserve_corrupt_source)
            print(
                json.dumps(
                    {
                        "raw_source_manifest": str(manifest.relative_to(root)),
                        "raw_source_preserved": True,
                        "consistent_backup": False,
                        "restore_authorized": False,
                        "restored": False,
                    }
                )
            )
            return 0
        except DataRootBusy as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(
                f"Raw-source preservation refused: {type(exc).__name__}; keep DB/sidecars/backups/partial evidence",
                file=sys.stderr,
            )
            return 4
    if args.inspect_corrupt_restore is not None:
        if (
            args.restore_candidate is None
            or args.installation_root is None
            or any(
                value
                for name, value in vars(args).items()
                if name
                not in (
                    "inspect_corrupt_restore",
                    "restore_candidate",
                    "installation_root",
                )
            )
        ):
            parser.error(
                "Damaged source inspection requires an explicit backup/candidate/installation only"
            )
        from .corrupt_restore_assessment import assess_corrupt_restore

        try:
            print(
                json.dumps(
                    assess_corrupt_restore(
                        data_root(),
                        args.inspect_corrupt_restore,
                        args.installation_root,
                        args.restore_candidate,
                    )
                )
            )
            return 0
        except DataRootBusy as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(
                f"Damaged source inspection refused: {type(exc).__name__}; keep DB/sidecars/backups",
                file=sys.stderr,
            )
            return 4
    if args.inspect_components:
        if any(
            value for name, value in vars(args).items() if name != "inspect_components"
        ):
            parser.error("Component history cannot be combined with other operations")
        from .component_selection import inspect_components_cli

        return inspect_components_cli(data_root())
    if args.reviewed_components is not None and args.select_components is None:
        parser.error("--reviewed-components requires --select-components")
    if args.components:
        if any(
            value
            for name, value in vars(args).items()
            if name not in ("components", "ui_smoke")
        ):
            parser.error("Component UI cannot be combined with other operations")
        from .component_ui import run_components

        return run_components(data_root(), ui_smoke=args.ui_smoke)
    if args.check_components is not None:
        if any(
            value for name, value in vars(args).items() if name != "check_components"
        ):
            parser.error("Component checks cannot be combined with other operations")
        from .local_components import check_components_cli

        return check_components_cli(args.check_components)
    if args.select_components is not None:
        if any(
            value
            for name, value in vars(args).items()
            if name not in ("select_components", "reviewed_components")
        ):
            parser.error("Component selection cannot be combined with other operations")
        from .component_selection import select_components_cli

        return select_components_cli(
            data_root(), args.select_components, args.reviewed_components
        )
    if args.backup and (args.smoke or args.ui_smoke):
        parser.error("--backup cannot be combined with smoke modes")
    root = data_root()
    if args.maintenance:
        if any(
            value
            for name, value in vars(args).items()
            if name not in ("maintenance", "installation_root", "ui_smoke")
        ):
            parser.error("Maintenance UI cannot be combined with other operations")
        ensure_data_root(root)
        from .maintenance_ui import run_maintenance

        return run_maintenance(root, args.installation_root, ui_smoke=args.ui_smoke)
    if (
        args.apply_restore is not None
        or args.recover_restore is not None
        or args.reviewed_restore is not None
        or args.confirm_lost_changes
        or args.confirm_media_issues
    ):
        if (
            bool(args.apply_restore) == bool(args.recover_restore)
            or args.installation_root is None
            or args.inspect_restore is not None
            or args.inspect_update
            or args.resume_update
            or args.release_manifest
            or args.backup
            or args.smoke
            or args.ui_smoke
            or args.stage_update
            or args.prepare_update
            or args.activate_update
            or args.repair_update_metadata
            or args.launch_managed
            or args.compatible_only
            or args.health_check
            or (
                args.apply_restore
                and (
                    args.restore_candidate is None
                    or args.reviewed_restore is None
                    or not args.confirm_lost_changes
                )
            )
            or (
                args.recover_restore
                and (
                    args.restore_candidate is not None
                    or args.reviewed_restore is not None
                    or args.confirm_lost_changes
                    or args.confirm_media_issues
                )
            )
        ):
            parser.error(
                "Choose confirmed restore with candidate/review identity, or recover an existing restore journal"
            )
        from .restore_apply import apply_restore, recover_restore

        try:
            if args.apply_restore:
                restored = apply_restore(
                    root,
                    args.apply_restore,
                    args.installation_root,
                    args.restore_candidate,
                    reviewed_identity=args.reviewed_restore,
                    confirm_lost_changes=args.confirm_lost_changes,
                    confirm_media_issues=args.confirm_media_issues,
                )
            else:
                restored = recover_restore(
                    root, args.recover_restore, args.installation_root
                )
            print(
                f"Restore state resolved: {restored.name}; retain DB backups and media"
            )
            return 0
        except DataRootBusy as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(
                f"Restore refused or incomplete: {type(exc).__name__}; preserve journal/backups/guard and inspect actual state",
                file=sys.stderr,
            )
            return 4
    if args.inspect_restore is not None or args.restore_candidate is not None:
        if (
            args.inspect_restore is None
            or args.restore_candidate is None
            or args.installation_root is None
            or args.inspect_update
            or args.resume_update
            or args.release_manifest
            or args.backup
            or args.smoke
            or args.ui_smoke
            or args.stage_update
            or args.prepare_update
            or args.activate_update
            or args.repair_update_metadata
            or args.launch_managed
            or args.compatible_only
            or args.health_check
        ):
            parser.error(
                "Restore inspection requires explicit backup ID, --restore-candidate and --installation-root only"
            )
        from .restore_assessment import assess_restore

        try:
            print(
                json.dumps(
                    assess_restore(
                        root,
                        args.inspect_restore,
                        args.installation_root,
                        args.restore_candidate,
                    ),
                    sort_keys=True,
                )
            )
            return 0
        except DataRootBusy as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(
                f"Restore inspection refused: {type(exc).__name__}; no DB restore performed",
                file=sys.stderr,
            )
            return 4
    if args.inspect_update or args.resume_update:
        if (
            (args.inspect_update and args.resume_update)
            or args.installation_root is None
            or args.release_manifest is not None
            or args.backup
            or args.smoke
            or args.ui_smoke
            or args.stage_update
            or args.prepare_update
            or args.activate_update
            or args.repair_update_metadata
            or args.launch_managed
            or args.compatible_only
            or args.health_check
        ):
            parser.error(
                "Choose update inspection or resume with --installation-root only"
            )
        from .update_recovery import inspect_update, resume_update

        try:
            if args.inspect_update:
                print(
                    json.dumps(
                        inspect_update(
                            root, args.inspect_update, args.installation_root
                        ),
                        sort_keys=True,
                    )
                )
            else:
                resumed = resume_update(
                    root, args.resume_update, args.installation_root
                )
                print(
                    f"Resumed health-validated installation: {resumed.name}; no DB restore"
                )
            return 0
        except DataRootBusy as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(
                f"Update recovery refused: {type(exc).__name__}; keep backups and inspect journal",
                file=sys.stderr,
            )
            return 4
    if args.compatible_only and (
        args.backup
        or args.stage_update
        or args.prepare_update
        or args.activate_update
        or args.repair_update_metadata
        or args.launch_managed
        or args.health_check
        or args.release_manifest is not None
        or args.installation_root is not None
    ):
        parser.error("--compatible-only allows only normal UI or smoke modes")
    if args.launch_managed:
        if (
            args.backup
            or args.stage_update
            or args.prepare_update
            or args.activate_update
            or args.repair_update_metadata
            or args.health_check
            or args.release_manifest is not None
            or args.installation_root is None
            or (args.smoke and args.ui_smoke)
        ):
            parser.error(
                "Managed launch requires --installation-root and at most one smoke mode"
            )
        from .managed_launcher import launch_managed

        try:
            return launch_managed(
                root, args.installation_root, smoke=args.smoke, ui_smoke=args.ui_smoke
            )
        except DataRootBusy as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(f"Managed launch refused: {type(exc).__name__}", file=sys.stderr)
            return 4
    if args.health_check:
        if (
            args.backup
            or args.smoke
            or args.ui_smoke
            or args.stage_update
            or args.prepare_update
            or args.activate_update
            or args.repair_update_metadata
            or args.release_manifest is not None
            or args.installation_root is not None
        ):
            parser.error("--health-check cannot be combined with other modes")
        from .update_health import readonly_health

        try:
            health_result = readonly_health(root)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(f"Health refused: {type(exc).__name__}", file=sys.stderr)
            return 4
        print(json.dumps(health_result, sort_keys=True))
        return 0
    if args.activate_update or args.repair_update_metadata:
        if (
            args.backup
            or args.smoke
            or args.ui_smoke
            or args.stage_update
            or args.prepare_update
            or args.release_manifest is not None
            or args.installation_root is None
            or (args.activate_update and args.repair_update_metadata)
        ):
            parser.error(
                "Choose activation or metadata repair with --installation-root"
            )
        from .update_activation import activate_prepared_update
        from .update_metadata import repair_update_metadata

        operation = "Metadata repair" if args.repair_update_metadata else "Activation"

        try:
            if args.repair_update_metadata:
                active = repair_update_metadata(
                    root, args.repair_update_metadata, args.installation_root
                )
            else:
                active = activate_prepared_update(
                    root, args.activate_update, args.installation_root
                )
        except DataRootBusy as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            print(
                f"{operation} refused: {type(exc).__name__}; retain backup and inspect journal",
                file=sys.stderr,
            )
            return 4
        print(f"{operation} completed for health-validated installation: {active.name}")
        return 0
    if args.stage_update and args.prepare_update:
        parser.error("Choose stage-only or update preparation")
    if args.stage_update or args.prepare_update:
        if (
            args.backup
            or args.smoke
            or args.ui_smoke
            or args.release_manifest is None
            or args.installation_root is None
        ):
            parser.error(
                "Staging requires --release-manifest and --installation-root, without other modes"
            )
        from .installation_stage import stage_installation
        from .update_preparation import prepare_update

        try:
            if args.prepare_update:
                candidate = prepare_update(
                    root,
                    args.prepare_update,
                    args.release_manifest,
                    args.installation_root,
                )
            else:
                candidate = stage_installation(
                    args.stage_update,
                    args.release_manifest,
                    args.installation_root,
                    user_data_root=root,
                )
        except DataRootBusy as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except (
            OSError,
            ValueError,
            RuntimeError,
            sqlite3.Error,
            zipfile.BadZipFile,
        ) as exc:
            print(f"Update/staging refused: {exc}", file=sys.stderr)
            return 4
        result_type = (
            "Prepared update journal"
            if args.prepare_update
            else "Verified installation staged"
        )
        print(f"{result_type}: {candidate.name}; activation pending")
        return 0
    if args.release_manifest is not None or args.installation_root is not None:
        parser.error("Staging paths require --stage-update")
    if args.backup:
        from .update_backup import create_update_backup

        try:
            result = create_update_backup(root)
        except DataRootBusy as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            print(f"Backup refused: {exc}", file=sys.stderr)
            return 4
        print(f"Validated DB-only backup: {result.name}; media is not included")
        return 0
    if not args.compatible_only:
        ensure_data_root(root)
    try:
        with AppDataLock(root) as coordination:
            launch_result = _run(args, root, coordination=coordination)
        if launch_result == MAINTENANCE_REQUESTED:
            from .maintenance_ui import run_maintenance

            return run_maintenance(root, args.installation_root)
        if launch_result == COMPONENTS_REQUESTED:
            from .component_ui import run_components

            return run_components(root)
        return launch_result
    except DataRootBusy as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        from .restore_guard import PendingRestore

        if isinstance(exc, PendingRestore):
            print(str(exc), file=sys.stderr)
            return 4
        if not args.compatible_only:
            raise
        print(f"Compatible launch refused: {type(exc).__name__}", file=sys.stderr)
        return 4


def _run(
    args: argparse.Namespace, root: Path, *, coordination: AppDataLock | None = None
) -> int:
    """Initialize and run only while the app/updater coordination lock is held."""
    from .restore_guard import require_no_pending_restore

    require_no_pending_restore(root)
    db_path = root / "creator_loop.sqlite3"
    if not args.compatible_only:
        initialize(db_path)
    with closing(open_readonly(db_path)) as db:
        validate(db)
    if args.smoke:
        print(f"Creator Loop {__version__}: schema OK at {db_path}")
        return 0
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("PySide6 is required for the desktop UI", file=sys.stderr)
        return 2
    from .library_ui import LibraryWindow

    recovery = None
    runtime_recovery = None
    if not args.ui_smoke:
        from .processing_recovery import recover_processing_startup
        from .runtime_recovery import recover_runtime_startup

        if coordination is None:
            raise RuntimeError("UI startup recovery requires the app coordination lock")
        recovery = recover_processing_startup(root, coordination)
        runtime_recovery = recover_runtime_startup(root, coordination)
    app = QApplication(sys.argv)
    window = LibraryWindow(root)
    if (
        recovery is not None
        and runtime_recovery is not None
        and (
            recovery.interrupted_run_ids
            or recovery.unverified_running_ids
            or runtime_recovery.cleaned
            or runtime_recovery.preserved
        )
    ):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QLabel

        messages = []
        if recovery.interrupted_run_ids:
            messages.append(
                f"Đã ghi nhận {len(recovery.interrupted_run_ids)} tác vụ thumbnail bị gián đoạn; "
                "chọn ảnh gốc trong Library để chạy thumbnail lại."
            )
        if recovery.unverified_running_ids:
            messages.append(
                f"Còn {len(recovery.unverified_running_ids)} tác vụ từ phiên trước chưa xác minh; "
                "ứng dụng giữ nguyên trạng thái và không tự chạy lại."
            )
        if runtime_recovery.cleaned:
            messages.append(
                f"Đã dọn {len(runtime_recovery.cleaned)} workspace decoder từ phiên đã kết thúc."
            )
        if runtime_recovery.preserved:
            messages.append(
                f"Giữ nguyên {len(runtime_recovery.preserved)} workspace chưa đủ bằng chứng dọn an toàn."
            )
        notice = QLabel(" ".join(messages))
        notice.setTextFormat(Qt.TextFormat.PlainText)
        notice.setWordWrap(True)
        central = window.centralWidget()
        if central is not None and central.layout() is not None:
            central.layout().addWidget(notice)
    window.show()
    if args.ui_smoke:
        from PySide6.QtCore import QTimer

        QTimer.singleShot(200, app.quit)
    result = app.exec()
    if window.components_requested:
        return COMPONENTS_REQUESTED
    return MAINTENANCE_REQUESTED if window.maintenance_requested else result


if __name__ == "__main__":
    raise SystemExit(main())
