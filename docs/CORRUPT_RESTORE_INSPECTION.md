# Kiểm trạng thái thực của guarded corrupt copy

Sau [guarded copy](CORRUPT_RESTORE_COPY.md) hoặc lỗi/cancel/crash của nó, giữ mọi
file, runtime guard và journal. Lệnh inspection chỉ đọc; không tạo/migrate DB,
replay WAL, re-copy backup, chuyển/xóa nguồn, sửa guard hoặc activate installation.

```powershell
$env:PYTHONPATH = 'app'
$env:CREATOR_LOOP_DATA_ROOT = 'D:\Dữ liệu Creator Loop'
& .\scripts\run_test_with_timeout.ps1 -Executable .\.venv\Scripts\python.exe `
  -TimeoutSeconds 90 -CommandArgs @('-m','creator_loop',
  '--inspect-corrupt-copy','D:\Dữ liệu Creator Loop\manifests\corrupt-restore-ID.json')
```

Chọn journal rõ ràng, không chọn theo timestamp hoặc sửa phase bằng tay.
Inspection giữ app lock, journal/guard, stage, raw archive và file evidence;
kiểm root/ID/consent/preparation binding, metadata/hash/schema/counts của stage,
raw archive và các original files ở vị trí live/retention. Live DB được đọc
immutable nên không replay sidecar hoặc tạo DB đang thiếu. Size/hash/stat,
inventory và path vốn vắng được kiểm lại trước khi trả kết quả. Native sharing
giữ file/namespace; portable chỉ có snapshot checks, không Windows ownership.

| `actual_state` | Ý nghĩa và việc còn thiếu |
|---|---|
| `SOURCE_NOT_MOVED_GUARDED` | Bytes nguồn cũ vẫn ở live; guard đã ghi nhưng không chứng minh copy đã bắt đầu. |
| `SOURCE_PARTIALLY_RETAINED_GUARDED` | Original files nằm ở cả live và retention; không mở Library/tự dọn sidecar. |
| `SOURCE_RETAINED_LIVE_MISSING_GUARDED` | Original files đã giữ riêng, DB live thiếu; không initialize/smoke để tạo DB mới. |
| `EMPTY_CURRENT_DATABASE_GUARDED` | Live main đang rỗng, originals giữ đủ; không suy từ phase rằng copy thành công. |
| `VALIDATED_COPY_GUARDED` | Main thực khớp schema/integrity/FK/history/logical identity/counts của stage, original files giữ đủ và không có live sidecar chưa rõ. Vẫn cần recovery/fresh health/activation. |
| `UNKNOWN_*` | Bytes/inventory/current DB khác, thiếu, trùng hoặc chưa rõ. Giữ tất cả, không tự overwrite/re-copy/clear guard. |

Phase được trả để đối chiếu lịch sử; `actual_state` đến từ bytes/vị trí thực,
không suy từ phase. Phase không nhận diện được chỉ in `UNRECOGNIZED`, không
echo nội dung tùy ý trong journal. Physical digest đã ghi khác bytes hiện tại
vẫn là unknown dù logical identity khớp. Current row content không được in.

Exit0 báo trạng thái quan sát, kể cả unknown; **không là restore thành công**.
`guard_retained`, `requires_recovery_health` true; `activated`, `restored` false.
`inspection_identity` ràng buộc root, journal/preparation và state proof tại mốc
đọc; format2 có `current_files` SHA/size/presence của từng main/sidecar, kể cả
bytes foreign đang UNKNOWN. Không là chữ ký hoặc quyền apply/clear guard. Input/trạng thái đổi thì proof
cũ hết giá trị. Exit2 mixed args, exit3 app lock bận, exit4 metadata/binding/lỗi;
chỉ in loại lỗi. Budget60s mặc định, cần process timeout ngoài cho native I/O.

Với `VALIDATED_COPY_GUARDED`, dùng [recovery có consent và fresh health](CORRUPT_COPY_RECOVERY.md).
Known partial/unmoved/missing/empty dùng [resume có consent riêng](CORRUPT_COPY_RESUME.md).
UNKNOWN/nonempty partial, UI và final release vẫn cần triển khai/nghiệm thu. Không dùng
restore thường hoặc xóa marker để bypass. Test crash tiến trình không chứng minh
power-loss hoặc mọi filesystem. Log/FAIL/giới hạn ở [HANDOFF](HANDOFF.md);
phạm vi V1 vẫn theo [bảng nghiệm thu](PRODUCT_ACCEPTANCE.md).
