# Creator Loop — Gate 0 Decision Record

**Ngày:** 29/09/2026. **Trạng thái:** Gate 0 hoàn tất ở mức thiết kế; **chưa tạo source/commit, chưa chạy Gate 1**. Các quyết định dưới đây bổ sung cho bốn contract chính, không thay luật dữ liệu trong baseline.

## 1. Đối soát bốn contract

| Mục | Kết quả |
|---|---|
| `project_references` | Baseline V1.1 ghi đúng một target FK; CI test cùng luật. Không dùng bản V1 superseded. |
| `review_events` | Đúng một FK version target; không dùng polymorphic FK giả. |
| Evidence/Claim/Draft | Link theo version cụ thể; anchor file cùng Asset; locator JSON có schema. |
| Publication Package | Phát hiện chỗ diễn đạt chưa đóng: Package bất biến sau khi tạo nhưng items phải được thêm. Đã sửa baseline và release contract: insert Package + items + fingerprint **trong một transaction**; commit rồi bất biến/được duyệt. |
| Approval/Publish | Fingerprint đúng snapshot; thay content/media tạo Package mới, approval cũ không hợp lệ. |
| Repo/install/data | Ba vùng tách biệt; updater không xóa user data/components ngoài ownership. |
| Migration/release | Backup API, backup validation, integrity/FK/schema/media check; exact artifact test; không silent update. |

**Không còn xung đột chặn thiết kế Gate 1** sau sửa Package atomicity. Các chi tiết triển khai chưa biết thực tế máy/repo được ghi là điều kiện xác nhận trước release, không được tự suy đoán đã hoàn thành.

## 2. Stack V1 chốt

| Tầng | Quyết định | Lý do và giới hạn |
|---|---|---|
| OS mục tiêu | Windows x64, ưu tiên Windows 10/11; xác nhận OS build thực tế của máy 8 GB trước khi pin Qt/runtime và trước release | Không tuyên bố tương thích với một bản Windows chưa được thử. |
| Ngôn ngữ | Python 3.12 x64, pin patch/dependencies bằng lock file tại Gate 1 | Một stack cho UI, domain, SQLite, subprocess; tương thích local workflow. Không chọn “latest” không pin. |
| Desktop UI | PySide6 **Qt Widgets** | Desktop native, không cần browser/server nền. UI thread chỉ hiển thị; xử lý media/model ở worker riêng, không chặn UI. Cần test licensing/redistribution của phiên bản Qt/PySide đã pin trước release. |
| Domain & persistence | Python modules thuần, `sqlite3` stdlib, SQL migrations được version hóa; không ORM V1 | Giữ các CHECK/FK/transaction hiển thị rõ. Repository layer là ranh giới; không đưa SQL tùy tiện vào UI. |
| Engine integration | Subprocess adapter + fake engine cho CI; FFmpeg/whisper.cpp/vision component được pin version/digest và tách khỏi application release | Chạy tuần tự theo budget, ownership record, cancel/timeout/crash cleanup. Model thật không chạy trong PR CI. |
| Packaging | Windows **one-directory** bundle, thử PyInstaller ở Gate 1; ZIP artifact chứa launcher + runtime app, không chứa model/media người dùng | Dễ inspect/test exact extracted artifact; nếu hook PySide6 hoặc AV gây lỗi, đánh giá pyside6-deploy trước khi khóa packager. Không chọn one-file self-extract ở V1. |
| Local API | Không mở HTTP server mặc định | UI gọi service/domain in-process; engine là process riêng. Nếu cần plugin/API sau, thiết kế quyền truy cập và lifecycle riêng. |

Đây là lựa chọn triển khai để Gate 1 thử bằng artifact. Nếu Pyside/packaging không đạt test máy mục tiêu, phải cập nhật Gate 0 decision và contract trước khi đổi stack.

## 3. Migration/versioning strategy

- Schema bắt đầu `0001_initial.sql` và SQLite `PRAGMA user_version = 1` sau commit; app release manifest khai báo khoảng schema hỗ trợ. Bảng `schema_migrations(id, checksum, applied_at, app_version)` lưu lịch sử. Không sửa nội dung migration đã phát hành; migration mới có ID tăng tuần tự và checksum mới.
- Mỗi migration được chạy theo thứ tự trong exclusive update window. Transaction bao mọi DDL/data step có thể atomic; migration nào không thể atomic phải tách thành bước có checkpoint/recovery contract riêng, không đưa vào release nếu chưa có test lỗi từng điểm.
- Validate preflight DB version được hỗ trợ, backup mở được, then migration; sau đó `integrity_check == ok`, `foreign_key_check` trả 0 dòng, `user_version` và history đúng target, media references còn resolve. Kết quả được ghi vào local update log.
- DB phiên bản mới không tự động downgrade. Chạy app cũ chỉ khi schema range còn tương thích; rollback không tương thích cần restore backup tương ứng và thông báo dữ liệu phát sinh sau backup có thể mất.
- Migration test từ DB trống và **mọi schema version đang được hỗ trợ**; một release V1.0 đầu tiên chỉ cần fixture empty/0001, các bản kế tiếp lưu fixture DB lịch sử trong tests (dữ liệu tổng hợp, nhỏ, không chứa dữ liệu người dùng).

## 4. Fixture DB và test matrix

| Fixture | Nội dung tối thiểu | Test |
|---|---|---|
| `empty_pre_0001` | DB mới, user_version 0 | migration 0001, FK/unique/CHECK, app launch. |
| `sample_v1` | Video/Image/Text assets, derived file, Evidence v1/v2, Claim, Draft, Package, Approval, Post/Observation; media giả nhỏ | version provenance, locator, FK, media path, approval, metric. Tạo fixture bằng factory script có dữ liệu deterministic, không chép DB thật. |
| `invalid_references` | FK mồ côi, project reference 0/2 targets, review event 0/2 targets, cross-Asset anchor | insert/update phải fail, hoặc validator báo fail nếu bất biến liên bảng không nằm trong DB. |
| `upgrade_failure` | Snapshot version cũ + fault injection ở từng bước | transaction rollback/backup còn mở được; app không mở schema không tương thích. |
| `release_unicode_path` | Giải nén artifact ở path Unicode/space, user-data root khác installation | launch/create/open/close, không ghi user data vào installation. |
| `worker_ownership` | Fake engine owned, unowned, PID reuse simulation, cancel/crash | chỉ dừng owned worker; recovery dọn bản ghi hợp lệ. |

**PR Ubuntu:** lint/type, schema/domain/locator, migration, approval; **PR Windows:** launcher, path, subprocess/PID, SQLite, packaging smoke nhỏ. **Pre-release:** exact artifact test và 8 GB machine E2E với component/model thật. Test matrix được tăng theo chức năng đã triển khai; không tạo check tên đẹp nhưng chạy no-op.

## 5. Quyết định cần xác minh tại môi trường thật

1. Máy mục tiêu: Windows version/build/architecture, quyền cài phần mềm, ổ lưu data, dung lượng trống, anti-virus và cách gọi engine. Việc này không chặn skeleton Gate 1 nhưng chặn cam kết release.
2. GitHub repository và quyền quản trị: Codex có thể viết workflow; **required checks/ruleset phải được bật ở repository**. Chưa có repo nên chưa thể xác nhận gate merge có hiệu lực.
3. Quyền phân phối PySide6/Qt, FFmpeg, whisper.cpp và model cụ thể: kiểm license và nguồn component trước khi publish.
4. Gói công cụ Windows được pin sau một lần build smoke thành công; lock file ghi hashes/versions. Không giả định GitHub-hosted Windows runner giống máy 8 GB.

## 6. Gate 1 entry criteria

Bốn tài liệu hiện hành cùng decision record này là input. Bắt đầu từ repository skeleton, migration 0001, launcher, real CI và artifact smoke; chưa xây toàn domain trong một commit. Nếu một quyết định ở mục 5 ảnh hưởng package/release, ghi nó là release blocker thay vì giả định đã qua. Không khởi tạo Gate 1 trong Gate 0.

## 7. Tài liệu kỹ thuật đối chiếu

- [Qt for Python / PySide6 quickstart](https://doc.qt.io/qtforpython-6/quickstart.html) và [deployment](https://doc.qt.io/qtforpython-6/deployment/index.html).
- [PyInstaller operating modes](https://pyinstaller.org/en/stable/operating-mode.html): one-folder là phương án thử ở Gate 1.
- [Python sqlite3 backup](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup) và [SQLite Backup API](https://sqlite.org/backup.html).
- [GitHub ruleset required status checks](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets).
