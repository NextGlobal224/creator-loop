# Backup DB trước update

Đóng Creator Loop trước khi chạy backup. Với bản portable Windows:

```powershell
$env:CREATOR_LOOP_DATA_ROOT = 'C:\Users\YourName\AppData\Local\CreatorLoop'
& .\CreatorLoop.exe --backup
```

Với source, dùng Python 3.12 và `PYTHONPATH=app`:

```powershell
$env:PYTHONPATH = 'app'
python -m creator_loop --backup
```

Lệnh chỉ mở DB hiện có, không khởi tạo hoặc migrate DB. Exit0 trả ID backup đã kiểm tra; exit3 nghĩa là app/updater khác đang giữ data root; exit4 là backup bị từ chối. Không xóa lock hoặc runtime records để bỏ qua lỗi. Nếu có worker/processing run chưa kết thúc, cần xử lý recovery trước; lệnh không terminate tiến trình.

Backup hoàn chỉnh nằm tại `backups/<backup_id>/`, gồm `creator_loop.sqlite3` và `backup-manifest.json`. Manifest ghi thời điểm UTC, app/schema version, size/digest của DB, file IDs/storage keys/digest/size đã ghi trong SQLite và đăng ký storage roots tại thời điểm backup. Backup được tạo qua SQLite Backup API dưới app lock và writer reservation; DB backup được mở lại để kiểm integrity, foreign keys, schema version và migration checksums trước khi công bố.

**Đây là backup DB, không có media.** Originals/derived vẫn nằm ở data root hoặc volume đã đăng ký. Kiểm storage reference xác nhận đường dẫn an toàn, volume identity, tệp có mặt và kích thước khớp; không hash lại toàn bộ media. Digest media trong inventory là digest đã ghi ở DB. Nếu storage offline/missing hoặc sai kích thước, backup không được công bố và không fallback sang volume khác.

Preflight kiểm dung lượng và quyền ghi; timeout của Backup API mặc định30s, DB writer contention chờ tối đa30s. Backup lỗi chỉ dọn staging do lượt đó tạo, giữ backup cũ và không sửa domain rows/media. Nếu tiến trình crash, thư mục `.<id>.staging` có thể còn: nó chưa được công bố là backup hoàn chỉnh. Không dùng staging để restore.

Backup hoàn chỉnh được giữ vô thời hạn trong slice hiện tại, không tự xóa theo lịch. Người dùng cần dự trù dung lượng hoặc lưu riêng DB backup và media. Snapshot manifest là metadata điều phối cho recovery, SQLite vẫn là nguồn chuẩn domain.

Stage installation và preparation backup→stage→locked migration có hướng dẫn riêng tại [staging](INSTALLATION_STAGING.md) và [update preparation](UPDATE_PREPARATION.md). Activation/health trên DB người dùng và restore có xác nhận còn đang triển khai. Không chép DB backup đè lên DB đang mở. Khi restore được triển khai, phải kiểm compatibility và xác nhận rằng thay đổi sau thời điểm backup sẽ mất, đồng thời đánh giá media/storage đã thay đổi. Không chạy app cũ trên schema ngoài range hỗ trợ.
