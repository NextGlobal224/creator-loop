# Guarded copy khi DB hiện tại bị hỏng

Lệnh này **thay DB live bằng backup đã stage/migrate**. Mọi thay đổi DB sau mốc
backup sẽ mất trong DB đang dùng; current counts/schema của nguồn hỏng không
đánh giá được, WAL/journal chưa được salvage. Nó giữ inode DB/sidecar cũ riêng,
raw archive, backup nhất quán, staged DB, media và installation cũ. Chỉ dùng
sau [review, raw retention và preparation](CORRUPT_RESTORE_PREPARATION.md).

Đóng Library và mọi worker thuộc app. Chọn rõ preparation manifest, candidate,
installation và review; đọc lại cảnh báo mất dữ liệu/media issue. Nếu input đổi,
inspect và prepare lại; không lấy checksum mới để chấp nhận một manifest bị sửa.
Không tự cấp default consent hoặc chọn backup mới nhất.

```powershell
$env:PYTHONPATH = 'app'
$env:CREATOR_LOOP_DATA_ROOT = 'D:\Dữ liệu Creator Loop'
& .\scripts\run_test_with_timeout.ps1 -Executable .\.venv\Scripts\python.exe `
  -TimeoutSeconds 210 -CommandArgs @('-m','creator_loop',
  '--copy-corrupt-restore','D:\Dữ liệu Creator Loop\backups\corrupt-restore-UUID\preparation.json',
  '--reviewed-preparation','PREPARATION_MANIFEST_SHA256',
  '--reviewed-restore','ASSESSMENT_IDENTITY_64_HEX',
  '--installation-root','D:\Bản cài Creator Loop',
  '--restore-candidate','D:\Bản cài Creator Loop\CANDIDATE',
  '--confirm-lost-changes')
```

Thêm `--confirm-media-issues` chỉ sau khi người dùng xác nhận issue trong review.
Consent tại preparation không tự cấp quyền copy sau này. Fresh damage token,
assessment, stage/raw archive, backup/candidate/media/registry phải còn khớp.
Không kết hợp copy với smoke, verify, update, restore thường hoặc UI operation.

Luồng chỉ mutation trên Windows. Giữ app lock xuyên khoảng nhả source read lease
để mở exclusive native handles; hash/identity/size/inventory lại trước mutation.
Journal mới dưới `manifests/corrupt-restore-ID.json` và runtime guard
`runtime/restore-in-progress.json` được fsync/readback trước khi chuyển nguồn.
DB và sidecars chuyển bằng native handle/no-replace sang
`backups/corrupt-original-ID/`; không xóa hay overwrite destination. Thư mục
và các input leases được giữ xuyên copy. DB live mới CREATE_NEW, ghi bằng SQLite
Backup API từ stage đã migrate, kiểm FK/integrity/schema/history/logical identity,
counts/media, fsync và digest. Native pin chặn replacement/delete, không phải
sandbox chặn mọi arbitrary byte writer. Blocking I/O cần process timeout ngoài.

Exit0 trả journal và phase `CORRUPT_DB_COMMITTED_GUARDED`;
`requires_recovery_health: true`, `activated: false`, `restored: false`.
**Guard vẫn chặn Library/managed launch**: copy thành công chưa là restore hoàn
tất. Activation, actual-state recovery và fresh runtime health còn cần triển
khai/nghiệm thu riêng; không xóa guard hoặc dùng restore thường để bypass.
Exit2 là input thiếu/mixed, exit3 app lock bận, exit4 từ chối/lỗi.

Sau lỗi/cancel/crash, giữ mọi file, journal, marker, partial target và bytes
nguồn ở cả vị trí live/retention. Native rename có thể thành công rồi fsync lỗi;
journal phase hoặc exception không chứng minh outcome. Nếu source thiếu,
**không chạy smoke/initialize để tạo DB mới**. Recovery phải đối chiếu bytes và
inventory thực tế; không tự re-copy, xóa sidecar, rollback chỉ bằng EXE hoặc làm
mất thay đổi ngoài app. Test crash tiến trình không chứng minh mất điện hay mọi
filesystem outcome. Xem [HANDOFF](HANDOFF.md) và [nghiệm thu](PRODUCT_ACCEPTANCE.md).
