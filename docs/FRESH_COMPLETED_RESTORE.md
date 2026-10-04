# Fresh restore cho completed copy đã thay đổi

Áp dụng khi original guarded copy có receipt vật lý đã hoàn tất, nhưng DB hiện
tại thiếu, rỗng, đã đổi hoặc có WAL/SHM/hot journal mới. Interrupted resume không
cấp quyền chép lại completed copy. Không tự xóa sidecar, chọn backup theo thời
gian hoặc mở app để tạo DB mới. Bản DB còn đúng validated copy dùng
[recovery/health riêng](CORRUPT_COPY_RECOVERY.md).

Đóng app và owned workers. Chọn original copy journal trong `manifests/`, data
root, backup ID và candidate installation cụ thể. Đường fresh đầu tiên chỉ hỗ
trợ backup/candidate đã được binding trong original copy/preparation; chọn lại
chúng một cách rõ ràng. Muốn dùng backup khác cần quy trình được hỗ trợ riêng,
không thay metadata để ép binding. Review thông báo thời điểm backup, media
issues và mất thay đổi; raw current bundle không chứng minh toàn bộ thay đổi
sau backup và không phải consistent backup.

```powershell
$env:CREATOR_LOOP_DATA_ROOT = 'D:\Dữ liệu Creator Loop'
CreatorLoop.exe --review-completed-copy-restore 'D:\Dữ liệu Creator Loop\manifests\corrupt-restore-COPY_ID.json' --fresh-restore-backup BACKUP_ID --installation-root 'D:\Bản cài Creator Loop' --restore-candidate 'D:\Bản cài Creator Loop\CANDIDATE'
```

Review readonly, không initialize/replay/repair. `apply_supported:true` là khả
năng của backend; `apply_authorized:false` cho biết chưa có consent. Lấy
`assessment_identity` mới; đổi lựa chọn hoặc bytes/IDs/evidence làm token cũ
không còn hợp lệ. Đọc cảnh báo rồi xác nhận fresh copy và mất thay đổi riêng:

```powershell
CreatorLoop.exe --apply-completed-copy-restore 'D:\Dữ liệu Creator Loop\manifests\corrupt-restore-COPY_ID.json' --fresh-restore-backup BACKUP_ID --installation-root 'D:\Bản cài Creator Loop' --restore-candidate 'D:\Bản cài Creator Loop\CANDIDATE' --reviewed-fresh-restore ASSESSMENT_ID --confirm-fresh-restore --confirm-lost-changes
```

Chỉ thêm `--confirm-media-issues` sau khi đã xem và chấp nhận issues hiện tại.
Consent partial/UNKNOWN của interrupted resume không thay consent fresh.
Thiếu/mixed arguments exit2, lock bận exit3, stale/binding/bytes/lỗi exit4. Lỗi
chỉ được báo theo loại, không đưa raw nội dung DB ra log CLI.

Copier giữ nguyên original journal/raw archive/backup/preparation và
installation trước. Original guard được lưu nguyên bytes tại
`backups/corrupt-guard-COPY_ID.json`; live guard bổ sung danh sách fresh journals
trước mọi chuyển current bytes, không có khoảng guard bị thiếu. Chuyển cả
current bundle còn hiện diện sang retention mới
bằng native inode, không overwrite hoặc cleanup. File chưa có không bị tạo như
original. Target mới dùng CREATE_NEW và SQLite Backup API từ closed validated
stage; kiểm schema/integrity/FK/logical counts/media. Receipt
`corrupt-fresh-restore-COPY_ID-FRESH_ID.json` giữ review/binding/current native
IDs và physical result, không được coi là backup nhất quán của current bundle.
Copy hoàn tất vẫn `FRESH_DB_COMMITTED_GUARDED`, chưa activate hoặc clear guard.

Review original copy journal lại bằng `--inspect-corrupt-copy`. Inspector pin và
kiểm tất cả fresh journals/bundles đã neo trong guard và original guard archive,
tìm exact native IDs qua các fresh retries,
không chọn receipt theo timestamp hoặc phase. Chỉ actual validated DB cùng
bằng chứng giữ đủ mới được [recovery với consent và health mới](CORRUPT_COPY_RECOVERY.md).
Health cần media hợp lệ; consent copy không bỏ qua điều này. Recovery không
recopy current DB, và kiểm lại evidence trước khi xóa native guard.

Cancel/crash giữ guard cùng mọi bytes/records. Nếu crash sau khi pending journal
được publish nhưng trước khi neo guard, journal chỉ được chấp nhận khi mọi
current native originals còn live và chưa có copied receipt. Fresh decision
tiếp theo neo cả pending history trước khi chuyển bytes. Thiếu journal đã neo
hoặc original guard archive sẽ từ chối health. Review actual state mới trước
retry; bundle bị chia giữa live root và fresh retention được đối chiếu theo
native IDs. Không sửa/xóa journal hoặc gộp archive bằng tay. Evidence thiếu,
trùng, foreign hoặc bị đổi sẽ từ chối; giữ tất cả để chẩn đoán. History bị chặn
khi vượt bounded budget (64 records); không tự xóa lịch sử để vượt giới hạn.

Trong Maintenance, chọn **Copy đã đổi: quyết định khôi phục mới** để mở dialog
riêng. Chọn original copy journal, backup ID và candidate cụ thể rồi chọn
**Đánh giá quyết định mới**. Không tự chọn backup theo thời điểm. Đọc thời điểm
backup, schema, current bundle, media và cảnh báo mất thay đổi; xác nhận fresh
decision và mất thay đổi bằng hai ô riêng. Media có vấn đề cần xác nhận riêng.
Các ô mặc định tắt; đổi selector, lỗi hoặc đóng dialog xóa proof/consent.

**Copy mới và giữ guard** chạy owned command với deadline300s/output1MiB,
không activate/clear guard. Hủy/đóng chỉ dừng command của dialog, giữ mọi
evidence; không hứa chưa đổi dữ liệu khi đã bắt đầu copy. Maintenance nhận lại
original journal nhưng bỏ proof/health consent cũ ngay cả khi selector không
đổi. Chọn **Đánh giá copy có guard** lại rồi xác nhận health riêng; media chưa
hợp lệ vẫn chặn health. Đóng cửa sổ cha cũng đóng dialog và owned command.

GUI đang ở checkpoint triển khai; CLI/backend và SOURCE/EXE proofs có scope riêng trong
[HANDOFF](HANDOFF.md). Chưa là full frozen product, engine/model thật, Windows
8GB/physical volumes, required CI/tag/release acceptance. Các yêu cầu V1 trong
[PRODUCT_ACCEPTANCE](PRODUCT_ACCEPTANCE.md) giữ nguyên.
