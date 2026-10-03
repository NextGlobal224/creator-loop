# Backup, stage và migrate dưới lock

Đóng app và hoàn tất/recover các worker trước khi chuẩn bị update. Dùng updater cùng bộ migration với candidate đã chọn; manifest từ nguồn phân phối cần có migration IDs/checksums. Lệnh này **có thể nâng schema DB**, giữ installation cũ và backup trước update, nhưng chưa activate candidate:

```powershell
$env:CREATOR_LOOP_DATA_ROOT = 'C:\Users\YourName\AppData\Local\CreatorLoop'
& .\CreatorLoop.exe --prepare-update 'C:\Downloads\CreatorLoop.zip' `
  --release-manifest 'C:\Downloads\release-manifest.json' `
  --installation-root 'C:\Users\YourName\Apps\CreatorLoop'
```

Source dùng `PYTHONPATH=app` và `python -m creator_loop` với cùng tham số. Installation root phải tồn tại và tách khỏi data root/source checkout. Candidate có schema hoặc SQL migration khác với updater hiện hành bị từ chối; dùng updater đi cùng phiên bản candidate, không đoán cách chạy migration không biết.

Quy trình giữ app lock và một SQLite writer transaction xuyên backup → staging → migration. Preflight kiểm installation disk/write và backup disk/write. DB backup mở được và đạt integrity/FK/schema/checksum trước khi stage. Sau đó kiểm bytes của candidate, checksum SQL migration đã đóng gói, chạy migration tăng tuần tự trong transaction và kiểm integrity/FK/schema/storage inventory trước commit. Không sửa hoặc xóa media; không cho migration đổi storage inventory trong V1 update này.

Lệnh trả `update-<id>.json` dưới `manifests/`, với backup ID, candidate directory, schema và trạng thái. Exit3 là app lock đang dùng; exit4 là lỗi update/staging. `PREPARED` nghĩa là migration đã commit và còn `activation_pending=true`. `FAILED_ROLLED_BACK` nghĩa là lượt xử lý lỗi trước commit đã rollback; `FAILED_POST_COMMIT` nghĩa là DB đã commit, không được hiểu là rollback. Backup và candidate đã stage vẫn được giữ khi lỗi.

Nếu tiến trình crash, phase trong journal chỉ là mốc cuối đã lưu, có thể là `STARTED`, `BACKUP_VALIDATED`, `INSTALLATION_STAGED` hoặc `MIGRATION_VALIDATED`. Phải kiểm schema/history thực tế cùng backup trước quyết định recovery; không suy rằng DB chưa commit chỉ từ journal chưa ghi `PREPARED`.

Không tự phục hồi DB hoặc đổi về executable cũ. App cũ chỉ mở được DB nếu schema hiện tại nằm trong range nó hỗ trợ. Restore phải xác nhận mất thay đổi sau backup và xem xét storage/media; DB backup không bao gồm media. Activation/health trên DB người dùng và UI recovery/restore đang được triển khai tiếp. Giữ backup vô thời hạn trong slice hiện tại; xem [backup](UPDATE_BACKUP.md) và [staging](INSTALLATION_STAGING.md).
