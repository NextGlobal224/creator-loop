# Chuẩn bị DB phục hồi khi nguồn bị hỏng

Luồng này tạo **DB staging riêng**, không thay DB hiện tại, không activate app
và không mở candidate để health check. Chỉ dùng backup đã công bố qua SQLite
Backup API và candidate đã stage/kiểm compatibility/migrations. Raw archive giữ
bytes nguồn để điều tra, không phải backup SQLite nhất quán hoặc WAL salvage.

Đóng Library và mọi worker thuộc app. Giữ installation cũ, backup, media và
mọi partial evidence. Chọn đúng data root, backup ID, candidate và installation
root; không tự chọn backup mới nhất. Thực hiện [assessment](CORRUPT_RESTORE_ASSESSMENT.md),
đọc cảnh báo mất mọi DB change sau backup và các media issue. Current counts/schema
không biết được; inspection không chứng minh WAL/journal không thể cứu.
Sau đó [giữ raw source](CORRUPT_SOURCE_PRESERVATION.md) bằng damage identity đó.

Ví dụ source (PowerShell, Python 3.12, root đã có; EXE dùng cùng arguments):

```powershell
$env:PYTHONPATH = 'app'
$env:CREATOR_LOOP_DATA_ROOT = 'D:\Dữ liệu Creator Loop'
& .\scripts\run_test_with_timeout.ps1 -Executable .\.venv\Scripts\python.exe `
  -TimeoutSeconds 210 -CommandArgs @('-m','creator_loop',
  '--prepare-corrupt-restore','BACKUP_ID_32_HEX',
  '--installation-root','D:\Bản cài Creator Loop',
  '--restore-candidate','D:\Bản cài Creator Loop\candidates\CANDIDATE',
  '--raw-source-manifest','D:\Dữ liệu Creator Loop\backups\raw-source-UUID\raw-source-manifest.json',
  '--reviewed-restore','ASSESSMENT_IDENTITY_64_HEX',
  '--confirm-lost-changes')
```

`--confirm-lost-changes` là xác nhận tường minh sau khi đọc assessment, không
cung cấp mặc định trong UI hoặc automation. Chỉ thêm `--confirm-media-issues`
khi assessment có issue và người dùng đã xác nhận chúng. Media lỗi/thiếu không
được sửa hoặc xóa. Input đổi làm review cũ bị từ chối; inspect và xác nhận lại.
Không kết hợp lệnh với smoke, restore thường, component hay update operation.

Chuẩn bị giữ app lock, nguồn/sidecar và raw archive; kiểm fresh assessment,
archive bytes/manifest, backup/schema/candidate/registry/media và dung lượng.
SQLite Backup API ghi vào tệp CREATE_NEW được giữ identity trong thư mục UUID
`backups/corrupt-restore-UUID/`. Tệp này cho SQLite VFS đọc/ghi và chặn native
replacement/delete; nó không chặn arbitrary byte writes từ phần mềm ngoài app.
DB staging được chuyển riêng sang journal mode DELETE, migration theo transaction,
kiểm media refs không đổi, integrity/FK/schema/history/checksums và logical identity.
Processing QUEUED/RUNNING trong backup phải recovery riêng trước khi stage.

Manifest `preparation.json` chỉ công bố sau fsync/hash/readback và fresh inputs.
Exit0 trả đường dẫn manifest tương đối cùng `raw_source_preserved: true`;
`apply_authorized`, `activated`, `restored` đều false. Exit2 là input thiếu/mixed,
exit3 là app lock bận, exit4 là từ chối/lỗi. Chỉ in metadata và loại lỗi, không
in bytes riêng. Work budget mặc định180s/max600s; runner ngoài chặn native I/O treo.

Sau lỗi/cancel/crash, giữ nguyên partial DB, manifest `.pending`, mọi nguồn và
archive. Lỗi sau publication có thể để lại `preparation.json` mà caller chưa
thành công; sự tồn tại của manifest không cấp quyền apply. Không tự sửa manifest
hoặc tái sử dụng partial folder. Portable CI kiểm snapshot; native sharing và
crash tiến trình không chứng minh mọi power-loss/filesystem outcome.

Guarded replacement, durable runtime guard, actual-state crash recovery, fresh
runtime health, UI và final artifact/release vẫn cần triển khai/nghiệm thu riêng.
Apply sau này phải revalidate source/archive/stage/backup/candidate/media và các
xác nhận dưới lock, journal trước mutation; không tin consent hoặc checksum cũ.
Xem [HANDOFF](HANDOFF.md) và [nghiệm thu V1](PRODUCT_ACCEPTANCE.md).
