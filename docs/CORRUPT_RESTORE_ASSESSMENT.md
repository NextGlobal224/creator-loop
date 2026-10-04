# Đánh giá backup khi SQLite nguồn hỏng

Đóng app trước khi kiểm. Lệnh này chỉ đánh giá một backup DB đã công bố và một
candidate được chọn rõ ràng; chưa áp dụng restore hay khôi phục WAL/journal.
Giữ nguyên DB nguồn, mọi sidecar, backup, installation và log.

```powershell
$env:CREATOR_LOOP_DATA_ROOT = 'D:\Dữ liệu Creator Loop'
CreatorLoop.exe --inspect-corrupt-restore BACKUP_ID --restore-candidate 'D:\Bản cài Creator Loop\VERSION-COMMIT-ID' --installation-root 'D:\Bản cài Creator Loop'
```

Từ checkout, đặt `PYTHONPATH=app` và dùng runner để có process timeout và log:

```powershell
& .\scripts\run_test_with_timeout.ps1 -Executable .\.venv\Scripts\python.exe `
  -TimeoutSeconds 180 -CommandArgs @('-m','creator_loop','--inspect-corrupt-restore',
  'BACKUP_ID','--restore-candidate','D:\Bản cài Creator Loop\VERSION-COMMIT-ID',
  '--installation-root','D:\Bản cài Creator Loop')
```

Lệnh giữ app lock và handle chỉ đọc cho DB, `-wal`, `-shm`, `-journal` đang có.
Hash/size và inventory nhận diện bytes hiện tại, không phải backup nhất quán hay
bằng chứng các hàng dữ liệu còn phục hồi được. Windows từ chối write/delete/replace
trong lượt kiểm; CI portable chỉ kiểm snapshot trước/sau. Nguồn thiếu, linked hoặc
hardlinked, runtime chưa phục hồi, app lock bận và lỗi I/O không được coi là DB hỏng.

SQLite được mở với `mode=ro&immutable=1`, không tạo sidecar hay sửa/recover nguồn.
Kết quả chỉ nhận integrity failure hoặc `SQLITE_CORRUPT`/`SQLITE_NOTADB` của view
được kiểm. Nó không đánh giá khả năng phục hồi WAL/hot journal. DB vẫn đọc được,
kể cả schema tương lai, phải đi qua assessment/recovery phù hợp; không dùng tuyến
này để bỏ qua schema compatibility. Xem [SQLite URI](https://www.sqlite.org/uri.html)
và [WAL](https://www.sqlite.org/wal.html).

Kết quả format2 gắn dấu vết nguồn/sidecar, registry, backup, candidate và media
hiện tại vào `assessment_identity`. Thời gian backup, schema/counts của backup và
media issue hiển thị để review. `current_schema`/`current_counts` là `null`,
`current_changes_assessable` là `false`: không thể liệt kê chính xác dữ liệu sẽ
mất từ DB hỏng. Backup DB không chứa media; file mới vẫn còn, không được tự xóa.
Sửa nguồn, sidecar, registry, backup, candidate hoặc media cần đánh giá lại.

`raw_source_preserved: false` nghĩa là chưa tạo bản giữ bytes nguồn riêng;
`apply_supported: false` và `restored: false` là trạng thái bắt buộc của slice này.
Không đưa proof format2 cho `--apply-restore` thường, không thay file DB bằng tay,
không xóa WAL/guard hoặc đoán candidate. Phần còn thiếu: giữ raw source bền vững,
review/xác nhận mất thay đổi và media riêng, apply có journal/guard cùng crash
recovery và fresh health trên exact artifact. Raw-source preservation không được
thay backup DB đã validate bằng SQLite Backup API.

Exit0 là assessment thành công, không phải restore thành công. Exit3 là app lock
bận, exit4 là từ chối/lỗi, exit2 là thiếu/mixed CLI arguments. Log/JSON chỉ chứa
metadata/hash và loại lỗi, không đưa nội dung hàng DB hoặc diagnostic SQLite vào
output. Source tests dùng DB/sidecar/media/candidate **fixture**; chưa nghiệm thu
full corrupt recovery, engine/model, máy8GB hoặc release. Bằng chứng hiện tại ở
[HANDOFF](HANDOFF.md), điều kiện bắt buộc ở [PRODUCT_ACCEPTANCE](PRODUCT_ACCEPTANCE.md).
