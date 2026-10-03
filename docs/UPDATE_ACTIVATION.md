# Activate bản đã chuẩn bị và kiểm health

Sau [preparation](UPDATE_PREPARATION.md), đóng app/worker và chọn đúng journal đã
trả về. Lệnh này chỉ nhận journal của data root hiện tại và installation root đã
stage; nó không chạy migration hoặc restore DB:

```powershell
$env:CREATOR_LOOP_DATA_ROOT = 'C:\Users\YourName\AppData\Local\CreatorLoop'
& .\CreatorLoop.exe --activate-update 'C:\Users\YourName\AppData\Local\CreatorLoop\manifests\update-ID.json' `
  --installation-root 'C:\Users\YourName\Apps\CreatorLoop'
```

Source dùng `PYTHONPATH=app`, `python -m creator_loop` với cùng tham số. Exit3 là
app lock bận; exit4 là activation bị từ chối/thất bại. Thư mục version cũ, backup,
DB và media luôn được giữ. Retention backup hiện vô thời hạn. DB-only backup không
chứa media; restore cần xác nhận mất thay đổi sau backup và đánh giá media riêng.

Service giữ AppDataLock và SQLite BEGIN IMMEDIATE xuyên kiểm DB, full candidate
file inventory/digest, backup opens/integrity/FK/schema/digest, storage references,
chuyển pointer và health. Runtime record chưa xử lý hoặc processing đang chạy
phải được recover trước; không tự kill PID. Journal phải là PREPARED hoặc
HEALTH_FAILED với migration đã commit; phase do crash đang dở cần recovery có
kiểm chứng, không retry mù.

Pointer `active-installation.json` trong installation root chỉ là điều phối chọn
version, không chứa domain data. Trước health, pointer ghi PENDING_HEALTH; native
owned launcher chạy `--health-check` readonly trên DB thật với deadline60s. Chỉ
sau JSON hợp lệ, candidate revalidation và storage inventory không đổi mới ghi
ACTIVE và journal COMPLETED/activation_pending=false. Health stdout/stderr,
ownership/result nằm dưới `logs/update-health-<id>-<run>/`. Không chạy model nặng.

Nếu health hoặc metadata write lỗi, journal ghi HEALTH_FAILED/error_type khi ghi
được. Chỉ phục hồi pointer cũ khi inventory/digest và schema readable range của
version cũ đã được xác minh; không restore DB. Nếu không có bản cũ tương thích,
pointer HEALTH_FAILED chặn managed launch. Không mở DB bằng executable cũ không
tương thích. Candidate mới và backup vẫn giữ để xử lý; lỗi ghi journal có thể để
lại phase cuối bền vững, nên phải đọc DB/pointer/log thực tế khi recovery.

CI chạy activation từ EXE exact ZIP trên prepared fixture và kiểm pointer ACTIVE,
lưu journal và log native health. Các test orchestration dùng health fixture có
kiểm lock; native health/owner crash được kiểm riêng và trên packaged candidate.
Đây là checkpoint activation, chưa phải nghiệm thu updater hoàn chỉnh: managed
launcher sử dụng pointer, metadata last-successful-update, crash recovery và
restore có xác nhận/UI đang làm tiếp. Không gọi artifact này là release V1.
