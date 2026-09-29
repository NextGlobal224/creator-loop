# Creator Loop — Bootstrap Gate 1 Status

**Ngày:** 29/09/2026. **Trạng thái:** nền source đã tạo; **Windows artifact gate chưa được xác nhận**.

## Đã thực hiện

- Tạo repo mới từ bốn contract và Gate 0; không dùng prototype 0.5/0.6. Initial local commit `828aae7` trên `main`; chưa có remote.
- Python 3.12 source, data root tách installation, launcher `--smoke`, PySide6 Qt Widgets tối thiểu khi cài optional dependency.
- Migration `0001_initial.sql` tạo bảng lõi V1, FK/CHECK/UNIQUE, triggers immutable; `schema_migrations` checksum và `user_version=1`. Kết nối bật FK, WAL, busy timeout; Backup API qua `sqlite3.Connection.backup()`.
- Locator validator V1, Package creation API một transaction với nonempty item count và canonical fingerprint. Approval không chấp nhận Package thiếu item/fingerprint khác; package item cap và immutability triggers.
- GitHub Actions workflow: fast Ubuntu, Windows tests/build one-directory, nén ZIP, giải nén ZIP ở đường dẫn Unicode/space, chạy `--smoke` trên executable giải nén, kiểm hash, tạo manifest/SHA256SUMS. Tag trên main có release job publish bytes đã test.
- 11 unittest local qua; source smoke qua trên Python 3.12 Linux. Git working tree clean tại thời điểm đóng gói.

## Chưa qua gate

- Windows runner **chưa chạy**; chưa chứng minh PySide6/PyInstaller bundle hoặc exact artifact smoke trên Windows.
- Không có GitHub remote/ruleset; required checks chưa được bật. Workflow có sẵn trong repo nhưng chưa có run.
- Chưa thử máy Windows 8 GB/model thật; đây là release gate, không phải thay cho Gate 1 Windows artifact smoke.
- Chưa có updater hoàn chỉnh, migration nâng cấp sau 0001, restore UI, engine lifecycle thật, content ingestion hoặc full publication flow. Không phân phối bản này như một release cho dữ liệu người dùng.

## Rủi ro kỹ thuật cần giải quyết trước khi mở rộng domain

- `PublicationRepository` tạo Package building + items + seal trong một transaction; direct SQL có thể commit một dòng building, nhưng DB chặn seal/approval khi thiếu items hoặc sai Project/Draft. Sau seal, Package/items bất biến. Đây là boundary ứng dụng, không phải sandbox chống người sửa SQLite trực tiếp. Khi mở API rộng hơn, giới hạn quyền ghi và test đường tạo duy nhất.
- Locator JSON schema được validate ở service layer; SQLite hiện chỉ kiểm JSON hợp lệ, nên mọi đường nhập Evidence phải gọi validator. Không dùng SQL insert trực tiếp cho input không tin cậy.
- `processing_runs` có trạng thái được cập nhật khi chạy; các bảng version/event/snapshot đã có triggers append-only nền. Một số invariant liên bảng (selection event thuộc project, quyền REUSE_MEDIA, final publish decision) cần service tests ở gate domain sau. Audit fix đã thêm append-only cho review/approval và chặn UPDATE lineage, Post link.
- Workflow action pins và package versions cần xác nhận trên GitHub runner; `release-manifest.json` hiện là bootstrap metadata, chưa có chữ ký/attestation và chưa đủ cho public release.

## Bước đóng Gate 1

1. Đưa repo mới lên GitHub mà người dùng chọn, bật ruleset yêu cầu `fast-schema-domain` và `windows-artifact` trên PR/main.
2. Chạy PR workflow và đọc lỗi thực tế; sửa đến khi Windows artifact job build, extract và smoke **chính ZIP** thành công.
3. Ghi SHA256, CI run URL, artifact ID, kết quả vào biên bản; chỉ lúc đó đánh dấu Gate 1 PASS. Không tự nhận đã chạy Windows từ kết quả Linux.

## Audit fix trước GitHub (29/09/2026)

- 11 test local PASS, gồm direct SQL bypass, UPDATE cross-Asset, review/approval append-only, readonly public path và Package/Draft guard.
- Ruff lint/format và mypy PASS. `pip-audit -r requirements-release.txt`: không thấy lỗ hổng đã biết tại thời điểm kiểm. Workflow thêm `security-dependencies-workflow` với audit dependency và action-pinning policy.
- Migration `0001` đã chỉnh **trước release đầu tiên**. Bản ZIP cũ bị thay thế; không dùng checksum migration cũ với DB mới.
- Trạng thái vẫn **GATE 1 IN PROGRESS**: chưa có Windows runner, GitHub remote/ruleset, exact Windows artifact.

## Secret scan patch (29/09/2026)

The `security-dependencies-workflow` job now checks out full history, installs pinned Gitleaks v8.30.1 with pinned Go setup action, and runs `gitleaks git --redact --log-opts='--all' .` as a real secret scan. The workflow policy check asserts its presence. CI execution and scanner results remain **unverified until GitHub runner runs**; Gate 1 is still IN PROGRESS.

The same patch updates checkout/setup-python/upload-artifact/download-artifact to Node 24 versions pinned by full SHA; CI has not run yet, so their actual runner compatibility remains to be observed.
