# Recovery sau guarded corrupt copy

Chỉ dùng sau [inspection trạng thái thực](CORRUPT_RESTORE_INSPECTION.md) trả
`VALIDATED_COPY_GUARDED`. Đọc kết quả hiện tại và chọn đúng journal/installation;
không sửa phase hoặc xóa guard để mở app.

```powershell
$env:PYTHONPATH = 'app'
$env:CREATOR_LOOP_DATA_ROOT = 'D:\Dữ liệu Creator Loop'
& .\scripts\run_test_with_timeout.ps1 -Executable .\.venv\Scripts\python.exe `
  -TimeoutSeconds 240 -CommandArgs @('-m','creator_loop',
  '--recover-corrupt-copy','D:\Dữ liệu Creator Loop\manifests\corrupt-restore-ID.json',
  '--reviewed-inspection','SHA256 từ inspection hiện tại',
  '--installation-root','D:\Installation Creator Loop', '--confirm-recovery')
```

Recovery chỉ hoàn tất bản copy đã được xác minh. Nó không recopy/migrate/repair
DB, replay WAL, chuyển/xóa originals hay dùng executable cũ thay DB restore.
Journal copy gốc, raw archive, consistent BackupAPI backup, staged DB, media và
installation trước được giữ lại. Format2 recovery có journal riêng
`manifests/corrupt-recovery-ID.json`; không tạo pre-corruption backup giả.

Lệnh giữ app lock, native inode/file/namespace leases và SQLite writer
reservation xuyên kiểm backup/candidate/media, pointer switch và health child.
Candidate phải khớp manifest đã chuẩn bị, có packaged migrations đúng checksum
và đọc được schema hiện tại. Mọi media reference phải valid; acknowledgement
media issues ở bước copy không bỏ qua điều kiện này. Health child chạy trong
native owned Job với timeout, đọc schema/query/storage và không chạy model nặng.

Sau health, success metadata giữ registered roots/component ownership; DB,
originals, raw/stage và media được kiểm lại. Guard chỉ được clear bằng đúng native
handle của marker đang khớp, cùng evidence/media leases và writer reservation.
Normal Library/managed launch vẫn bị chặn khi guard còn tồn tại.

`UNKNOWN_*`, partial, unmoved, missing hoặc empty current DB đều bị từ chối;
không initialize main đang thiếu. Thay đổi sau inspection làm proof cũ hết giá
trị. Health/metadata lỗi hoặc crash giữ guard/evidence; chạy inspection mới rồi
recovery rõ ràng sẽ health lại. Pointer fallback chỉ tới previous installation
đã xác minh đọc được schema thực; guard vẫn chặn launch khi recovery chưa đạt.
Foreign pointer được giữ, không overwrite để che lỗi. Không đoán từ phase.

Exit0 trả `activated/restored: true`, `guard_retained: false`; exit2 thiếu/mixed
consent, exit3 app lock bận, exit4 từ chối/lỗi với loại lỗi, không private content.
Nếu crash xảy ra quanh guard clear, đối chiếu marker, pointer, DB và journal thực;
không suy từ exit/log chưa ghi rằng cần recopy hoặc restore lại.

Budget mặc định180s, health tối đa60s trong budget; native I/O cần process timeout
ngoài. Process-crash tests không chứng minh power loss/mọi filesystem hoặc sandbox
chống arbitrary raw writers. Known partial states có [resume giữ bytes/guard](CORRUPT_COPY_RESUME.md);
UNKNOWN/nonempty partial, UI, exact final artifact,
required CI, actual engines/models và máy8GB vẫn cần nghiệm thu theo
[PRODUCT_ACCEPTANCE](PRODUCT_ACCEPTANCE.md). Bằng chứng/FAIL ở [HANDOFF](HANDOFF.md).
