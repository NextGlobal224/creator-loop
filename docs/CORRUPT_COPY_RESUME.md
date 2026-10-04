# Tiếp tục guarded copy bị ngắt

Chạy [inspection mới](CORRUPT_RESTORE_INSPECTION.md), đọc `actual_state` và dùng
đúng journal copy gốc. Chỉ tiếp tục các trạng thái source chưa chuyển, chuyển
một phần, đã giữ nguồn nhưng live thiếu, hoặc live main rỗng. Proof format2
ràng buộc SHA/size của cả main và các sidecar hiện tại; token cũ không dùng lại
sau khi bytes hoặc vị trí đổi. Cần đóng app/worker và xác nhận mất thay đổi sau
backup. UNKNOWN, nonempty partial hoặc copy đã validated bị từ chối.

```powershell
$env:PYTHONPATH = 'app'
$env:CREATOR_LOOP_DATA_ROOT = 'D:\Dữ liệu Creator Loop'
& .\scripts\run_test_with_timeout.ps1 -Executable .\.venv\Scripts\python.exe `
  -TimeoutSeconds 240 -CommandArgs @('-m','creator_loop',
  '--resume-corrupt-copy','D:\Dữ liệu Creator Loop\manifests\corrupt-restore-ID.json',
  '--reviewed-inspection','SHA256 từ inspection hiện tại',
  '--installation-root','D:\Installation Creator Loop', '--confirm-lost-changes')
```

Với `EMPTY_CURRENT_DATABASE_GUARDED`, phải thêm `--confirm-keep-partial` sau khi
đọc trạng thái. Empty target được chuyển bằng native inode handle vào thư mục
`backups/corrupt-original-RESUME_ID`; không xóa/overwrite và không gọi nó là
backup SQLite nhất quán. Originals còn ở live được giữ dưới thư mục retention
của copy gốc; originals đã giữ được kiểm hash và giữ nguyên. Journal riêng
`manifests/corrupt-resume-RESUME_ID.json` ghi việc đang dở; journal copy gốc
không bị sửa. Backup đã published, stage, raw archive và installation cũ giữ lại.

Resume giữ cùng app lock và native file/namespace leases, kiểm backup/candidate,
stage/schema/history/counts, current media và dung lượng. Media thiếu/đổi cần
`--confirm-media-issues`; consent này không bỏ qua fresh health sau đó. Main mới
được tạo bằng CREATE_NEW rồi copy bằng SQLite Backup API từ stage đã validated.
Không replay damaged WAL hoặc khởi tạo/migrate nguồn bị hỏng. Các reference,
digest, inventory và runtime ownership được kiểm trước receipt guarded.

Exit0 chỉ xác nhận `RESUME_DB_COMMITTED_GUARDED`: `restored/activated:false`,
`guard_retained:true`. Tiếp inspection mới rồi [recovery/fresh health có consent](CORRUPT_COPY_RECOVERY.md).
Exit2 thiếu/mixed consent, exit3 app lock bận, exit4 từ chối/lỗi chỉ in loại lỗi.
Budget mặc định180s; native I/O cần timeout cấp tiến trình như ví dụ trên.

Cancel/lỗi/crash giữ mọi bytes đã chuyển, partial target, journal và guard.
Không suy từ phase rằng đã hoàn tất; inspection trạng thái thực quyết định có
thể tiếp tục hay phải giữ UNKNOWN để xử lý riêng. Chưa có automatic recovery
cho nonempty/foreign partial bundle; không xóa marker hoặc chạy smoke để bypass.
Actual process-crash tests không chứng minh power loss. UI/whole flow, exact
final artifact/required CI, actual engines/models và máy8GB vẫn theo
[PRODUCT_ACCEPTANCE](PRODUCT_ACCEPTANCE.md); logs/FAIL/scope ở [HANDOFF](HANDOFF.md).
