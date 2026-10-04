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
    if args.check_components is not None:
        if any(
            value for name, value in vars(args).items() if name != "check_components"
        ):
            parser.error("Component checks cannot be combined with other operations")
        from .local_components import check_components_cli

        return check_components_cli(args.check_components)
    if args.select_components is not None:
        if any(
            value for name, value in vars(args).items() if name != "select_components"
        ):
            parser.error("Component selection cannot be combined with other operations")
        from .component_selection import select_components_cli

        return select_components_cli(data_root(), args.select_components)
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
    return MAINTENANCE_REQUESTED if window.maintenance_requested else result


if __name__ == "__main__":
    raise SystemExit(main())
