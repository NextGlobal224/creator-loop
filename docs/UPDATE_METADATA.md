# Metadata của update và data root

Sau activation/health và journal COMPLETED đã bền vững, updater bổ sung metadata
vào `manifests/storage-roots.json`. Đây vẫn là registry điều phối hiện có, không
tạo nguồn domain JSON khác với SQLite. Giữ nguyên data-root ID, registered root
IDs/paths/volume identities, default root và các field/component registrations
đã có, kể cả root đang offline. Nếu chưa có registry, tạo data-root ID UUID mới
và danh sách storage roots/component installations rỗng; không suy quyền sở hữu
engine/model từ tệp tồn tại trong thư mục.

Schema version là schema thực tế quan sát dưới app/SQLite writer lock.
`last_successful_update` ghi update ID, app version/commit, candidate name và thời
điểm activation/health hoàn tất. `last_backup_id` là snapshot DB gắn với update
thành công này; manual backup riêng vẫn có ID/manifest riêng dưới backups. Không
ghi metadata thành công trước health và không sửa DB/media khi ghi manifest.

Nếu ghi manifest lỗi **sau core activation thành công**, journal chuyển thành
COMPLETED_METADATA_PENDING, activation_pending=false và giữ pointer ACTIVE đã
health-validated. Đây là lỗi metadata, không phải migration rollback/health fail;
không tự restore DB hoặc chuyển app cũ. Exit4 yêu cầu kiểm journal/log và repair:

```powershell
$env:CREATOR_LOOP_DATA_ROOT = 'C:\Users\YourName\AppData\Local\CreatorLoop'
& .\CreatorLoop.exe --repair-update-metadata 'C:\Users\YourName\AppData\Local\CreatorLoop\manifests\update-ID.json' `
  --installation-root 'C:\Users\YourName\Apps\CreatorLoop'
```

Repair chỉ nhận COMPLETED/COMPLETED_METADATA_PENDING thuộc data root hiện tại,
đúng active pointer/update ID. Nó kiểm DB/history/integrity/FK/storage, candidate
inventory/digest/range và backup opens/digest/schema, chạy fresh readonly health
với native deadline dưới app+DB writer lock rồi ghi manifest/journal. ID và
timestamp update gốc không đổi khi repair lặp lại. Health lỗi giữ metadata cũ,
ghi error type và log directory; không ghi nội dung riêng vào journal.

Registry/journal và bản copy backup/installation vẫn phải còn hợp lệ. Manifest
malformed/linked, candidate hoặc backup thay đổi bị từ chối; không tự ghi đè
registry để che lỗi. Crash có thể để lại mốc metadata pending hoặc journal chưa
ghi phase cuối dù manifest đã atomic replace; repair dựa trên trạng thái DB,
pointer và fresh health, không dựa riêng tên phase. Phase core activation còn
dở vẫn cần recovery, lệnh này không activate app hoặc restore DB.

CI kiểm metadata của exact prepared activation rồi chạy repair từ exact EXE,
đối chiếu data-root ID và giữ fixture manifest/journal/log trong artifact. UI
recovery/restore và component manager/engine/model thật còn triển khai tiếp.
