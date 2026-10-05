# Giữ riêng DB hỏng và sidecar trước recovery

Đóng app và giải quyết runtime work còn giữ trước khi chạy. Dùng
[đánh giá nguồn hỏng](CORRUPT_RESTORE_ASSESSMENT.md) với backup/candidate được chọn
rõ ràng, xem cảnh báo thay đổi không thể đánh giá. Lấy giá trị
`damage.damage_identity` từ JSON vừa kiểm, không dùng `assessment_identity` thay thế.
Giá trị này gắn với canonical data root cùng inventory, size và SHA256 của DB,
`-wal`, `-shm`, `-journal`; bytes giống nhau ở root khác không dùng chung review.

```powershell
$env:CREATOR_LOOP_DATA_ROOT = 'D:\Dữ liệu Creator Loop'
$env:PYTHONPATH = 'app'
& .\scripts\run_test_with_timeout.ps1 -Executable .\.venv\Scripts\python.exe `
  -TimeoutSeconds 240 -CommandArgs @('-m','creator_loop','--preserve-corrupt-source',
  'DAMAGE_IDENTITY_64_HEX_FROM_FRESH_REVIEW')
```

Bản đóng gói có entry point `CreatorLoop.exe --preserve-corrupt-source TOKEN`.
Chạy bằng runner tương tự, bỏ `-m creator_loop`; luôn giữ process timeout bên
ngoài cho I/O có thể block. Budget nội bộ 180 giây không thay hard timeout.
Không kết hợp lệnh với smoke, restore, update hoặc xác nhận mất thay đổi.

Lệnh kiểm lại nguồn hỏng và token dưới app lock, giữ mọi native readonly source
handle tới cuối. Sau kiểm dung lượng, tạo thư mục riêng
`backups/raw-source-<UUID>`; ghi độc quyền, fsync và đọc lại từng file. Kiểm lại
nguồn/inventory trước công bố `raw-source-manifest.json`. Windows giữ handle
đích độc quyền và thư mục không cho delete sharing; rename manifest dùng chính
handle tạo file, không overwrite đích lạ. Xem
[SetFileInformationByHandle](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-setfileinformationbyhandle)
và [FILE_RENAME_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_rename_info).
Portable CI dùng exclusive link/unlink và fsync thư mục, kiểm snapshot; không
chứng minh native sharing hoặc chống writer đồng thời trên Windows.

Exit0 trả đường dẫn manifest tương đối và `raw_source_preserved: true`.
Manifest chứa thời gian UTC, version app, root/damage identity và inventory/hash.
`consistent_backup`, `wal_recoverability_assessed`, `restore_authorized`,
`media_included`, cùng `restored` trong receipt đều false. Bytes DB/WAL hỏng là
bằng chứng giữ nguyên, chưa phải SQLite snapshot nhất quán hoặc dữ liệu đã cứu
được. Backup hợp lệ để restore vẫn cần SQLite Backup API, integrity/FK/schema,
compatibility/media và xác nhận riêng theo contract.

Exit3 là app lock bận; exit2 là mixed arguments; exit4 là từ chối/lỗi. Nguồn
thiếu/đọc được, alias/hardlink, review cũ, runtime chưa giải quyết, thiếu đĩa,
cancel/deadline, short write hay fsync lỗi đều không cấp quyền restore. Log chỉ
in loại lỗi/metadata; nội dung riêng vẫn nằm trong raw files cục bộ.

Giữ nguyên mọi thư mục lưu dở và `.pending` sau lỗi/cancel/crash. Không tự xóa
nguồn, sidecar, partial evidence, media, backup hay installation trước. Lỗi sau
rename có thể để lại manifest dù caller chưa nhận thành công; manifest tồn tại
đơn lẻ không đủ để apply. Trên portable, crash giữa link/unlink còn có thể để lại
hai tên hardlink. Recovery phải kiểm lại toàn bộ bytes, inventory, identity và
manifest; loader chỉ đọc/xác minh archive, apply dùng archive vẫn còn thiếu.
Không tự dùng lại
partial folder; lần thử rõ ràng tiếp theo tạo UUID mới. Bản giữ raw chưa có cơ
chế tự hết hạn hoặc xóa; chỉ quản lý khi có quy trình/quyền rõ ràng.

Để kiểm riêng archive, dùng chính manifest đã công bố và damage identity đã
được review cho nó:

```powershell
& .\scripts\run_test_with_timeout.ps1 -Executable .\.venv\Scripts\python.exe `
  -TimeoutSeconds 120 -CommandArgs @('-m','creator_loop','--verify-preserved-source',
  'D:\Dữ liệu Creator Loop\backups\raw-source-UUID\raw-source-manifest.json',
  '--reviewed-damage','DAMAGE_IDENTITY_64_HEX')
```

Lệnh xác minh root/retention/damage binding, metadata format/UTC/flags, duplicate
JSON fields, exact inventory và fresh size/SHA256 của từng raw file dưới retained
handles. Manifest tối đa16KiB; hash theo chunk1MiB với budget60s và hard timeout
ngoài. Partial/extra/missing entries, hardlink/alias, manifest/bytes bị sửa, review
hoặc root không khớp đều bị từ chối, không sửa/xóa evidence. Native lease giữ
archive qua lượt đọc; portable chỉ kiểm snapshot. Có thể đọc khi live DB thiếu,
không tạo DB mới. Exit0 trả `archive_revalidated: true` và manifest digest;
`current_source_assessed`, `consistent_backup`, `restore_authorized`, `restored`
đều false. Exit2 là thiếu/mixed arguments, exit4 là từ chối/lỗi. Lệnh này không
lấy app writer lock hay đánh giá DB/media hiện tại: apply vẫn phải giữ lock,
fresh review và các xác nhận riêng. Không sửa manifest bằng tay để đổi binding.

Test crash là crash tiến trình trên fixture, không phải nghiệm thu mất điện,
ổ đĩa lỗi hoặc mọi filesystem. Phần tiếp theo còn thiếu:
[staging với fresh review/loss/media consent](CORRUPT_RESTORE_PREPARATION.md), guarded apply/crash recovery/fresh health,
UI và exact final artifact. Trạng thái/log ở [HANDOFF](HANDOFF.md); yêu cầu V1
vẫn theo [PRODUCT_ACCEPTANCE](PRODUCT_ACCEPTANCE.md).
