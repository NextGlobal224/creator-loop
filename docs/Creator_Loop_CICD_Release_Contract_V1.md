# Creator Loop — CI/CD & Release Contract V1

**Cập nhật:** 29/09/2026. **Phụ thuộc:** `Creator_Loop_Data_Architecture_V1_1.md` là nguồn chuẩn luật dữ liệu. Đây là contract cho ứng dụng Windows local-first, không phải quy trình deploy server. Chưa tạo Bootstrap Gate 1.

## 1. Nguyên tắc phát hành

- Feature branch → PR → **required checks** → `main` → tag từ commit đã được kiểm tra → build Windows → test **chính artifact sẽ phát hành** → manifest/provenance → release → người dùng chủ động cập nhật.
- Không silent auto-update V1. Không Docker Hub/SSH production. Không đẩy dữ liệu người dùng vào GitHub Actions.
- Branch protection/ruleset buộc CI thành điều kiện merge; workflow xanh mà không required không phải gate. Quyền GitHub token theo job tối thiểu; release job có quyền ghi, PR không có secrets phát hành. Pin third-party actions theo full commit SHA hoặc theo chính sách repository; lock dependencies, kiểm license/vulnerability có ngưỡng và quy trình xử lý rõ.
- Tag/release tạo từ commit trên `main`; artifact phải mang commit SHA, app version, schema version, build time, target OS/arch, dependencies/components compatibility. Hash file công bố cùng manifest; cân nhắc code signing và artifact attestation. Hash đơn lẻ không chứng minh nguồn build.

## 2. Required CI trên PR

| Job | Nơi chạy | Điều kiện bắt buộc |
|---|---|---|
| Fast | Ubuntu | Formatting/lint/type check, schema/locator contract, domain/backend/frontend tests phù hợp mã thực tế; không đặt job giả cho phần chưa có. |
| Data & migration | Ubuntu + Windows cho đường dẫn/SQLite | DB mới, nâng cấp từ mọi schema version còn hỗ trợ, kiểm `PRAGMA foreign_keys=ON`, `integrity_check`, `foreign_key_check`, `user_version`/schema version; migration thất bại không để DB mở cho app mới. |
| Architecture invariants | Ubuntu | Project reference đúng một target; review event đúng một target; anchor file cùng Asset; Claim→Evidence Version; Package/Draft cùng Project; Post/platform khớp Package; NULL khác 0; version và observation append-only. |
| Approval | Ubuntu | Public repository tạo Package building + items + seal nguyên tử; direct SQL building rỗng không seal/duyệt được; sau seal bất biến; caption đổi một ký tự hoặc media bytes đổi → fingerprint đổi → approval cũ không hợp lệ; REVOKED ngăn publish; kiểm race giữa duyệt và tạo lệnh đăng. |
| Windows lifecycle | Windows hosted | Path có khoảng trắng/Unicode, launcher, SQLite, subprocess, cancel/timeout/crash; chỉ dừng process thuộc Creator Loop. Fake engine dùng trong PR để test kiểm soát tiến trình, không tải model 488 MB mỗi lần. |
| Security | Ubuntu/Windows tùy tool | Dependency audit, secret scan, permissions workflow, package manifest và các nguồn tải component. Không làm job báo xanh vô điều kiện. |

Mọi test dùng fixture giả, không chứa dữ liệu riêng của người dùng. Windows runner kiểm tra tương thích OS; **không chứng minh** hiệu năng trên máy người dùng 8 GB.

## 3. Ownership và lifecycle

Runtime ownership record gồm `run_id`, PID, canonical executable path, thời điểm/identity tạo process, parent/job identity khi có và component version. Trước terminate phải đối chiếu với tiến trình hiện tại để chống PID reuse; không kill process ngoài quyền sở hữu. Trên Windows ưu tiên quản lý process tree bằng primitive phù hợp (ví dụ Job Object) nếu triển khai bảo đảm được. Cancel, normal exit và crash recovery có test riêng. Mục tiêu sau khi đóng app: không còn worker **do app sở hữu**; worker của ứng dụng khác không bị ảnh hưởng.

## 4. Release gate

1. Build từ tag/commit xác định trên Windows sạch; đóng gói runtime cần thiết cho app hoặc nêu rõ prerequisite và kiểm tra ở preflight. Tách engine/model lớn qua component manager với version, digest, nguồn tải và quyền sở hữu.
2. Giải nén **artifact cuối** vào đường dẫn có khoảng trắng và tiếng Việt. Kiểm cấu trúc package, launcher, tạo DB mới, mở bản sao DB cũ sau migration, basic asset/evidence flow, đóng app sạch; so digest artifact trước/sau test.
3. Test thực trên máy Windows mục tiêu 8 GB trước release: engine/model thật, video/ảnh/text mẫu, thiếu RAM, thiếu dung lượng, cancel, crash recovery và thời gian khởi động. Ghi kết quả, không suy từ runner hosted.
4. Manifest ghi `app_version`, `git_commit`, `schema_from/schema_to`, `artifact_sha256`, file manifest/digest, minimum OS/runtime, component compatibility, migration IDs, release notes và hướng khôi phục. Chữ ký/attestation nếu bật phải kiểm được bằng quy trình công bố.
5. Release thủ công hoặc gated; chỉ upload đúng artifact đã test. Không rebuild khác bytes sau gate rồi dùng kết quả test cũ.

## 5. Update contract trên máy người dùng

`STOP OWNED WORKERS → exclusive app/DB lock → preflight disk/write permissions → consistent DB backup → validate backup opens + integrity/FK/schema → stage new installation → migration → integrity_check → foreign_key_check → schema_version check → media/storage reference check → activate new app → health check`.

- **Backup SQLite:** dùng SQLite Backup API (hoặc cơ chế snapshot SQLite được kiểm chứng), không copy riêng `.sqlite3` khi WAL còn hoạt động. Backup có ID, timestamp, app/schema version, size/digest; mở backup và kiểm tra nó trước khi migration.
- **Media consistency:** V1 update không sửa/xóa originals/derived user data. Trước và sau migration kiểm các `storage_key` bắt buộc; nếu chính migration thay quan hệ media, phải thiết kế backup/restore media nhất quán và kiểm thử riêng. Backup DB đơn lẻ không phải backup toàn bộ dữ liệu.
- **Package sealing:** tạo Package building, toàn bộ items, fingerprint và `sealed_at` trong một transaction; chỉ sealed snapshot mới được Approval tham chiếu. Test direct SQL building thiếu item, sai Project/Draft và UPDATE sau seal.
- **Migration:** chạy theo version tuần tự, có idempotency/guard rõ; mỗi migration bọc transaction khi SQLite operation cho phép. Không cho hai updater/app cùng mở DB để ghi. Ghi migration history và schema version sau khi thành công. `integrity_check` không thay `foreign_key_check`.
- **Failure:** không mở DB bằng app không tương thích; không cố rollback chỉ bằng executable. Giữ backup, bản installation trước và log lỗi không chứa nội dung riêng nhạy cảm. Restore DB chỉ theo quy trình có xác nhận rằng các thay đổi sau mốc backup sẽ mất; nếu migration chưa commit và transaction rollback được, ưu tiên giữ DB hiện tại.
- **Health:** mở connection, đọc schema version, truy vấn tối thiểu, resolve một storage reference mẫu, kiểm launcher/worker; có timeout. Không chạy model nặng chỉ để health check.

## 6. Rollback và tương thích

Mỗi release công bố schema range app có thể đọc. Có thể quay lại app cũ **chỉ khi** app cũ hỗ trợ schema hiện tại. Nếu không, khôi phục backup DB trước update cùng installation tương ứng, kèm đánh giá media thay đổi từ sau backup. Migration có thể chỉ đi tới; không hứa downgrade tự động. Bản backup và thời hạn giữ phải được nêu trong UI update.

## 7. Gates trước commit đầu tiên

Repository skeleton, `.gitignore`, docs baseline, workflow có kiểm tra thực, fixture DB, migration 0001, launcher Windows tối thiểu và một smoke test artifact. Không cần triển khai toàn bộ domain trước commit đầu, nhưng CI phải chạy và chặn vi phạm đang có. Mọi thay đổi luật dữ liệu đi qua PR cập nhật **baseline + migration + test** trong cùng thay đổi.
