# Khôi phục từ nguồn DB hỏng trong Maintenance

Đóng Library và mọi worker, đặt `CREATOR_LOOP_DATA_ROOT` trỏ đúng dữ liệu,
rồi chạy `CreatorLoop.exe --maintenance`. Chọn **Nguồn hỏng: giữ raw, staging
và copy**. Window mở mà không initialize/migrate hoặc replay DB nguồn.

1. Chọn installation root, candidate đã staging và **backup ID cụ thể**.
   Không chọn bản mới nhất theo timestamp. Candidate phải có release manifest,
   inventory/digests và schema range phù hợp. Nếu chưa có candidate, dùng
   [installation staging](MAINTENANCE_UI.md) trước.
2. **Đánh giá nguồn / backup / raw / stage**. Đọc thời điểm backup, schema,
   candidate và từng media issue. Không thể xác định chính xác thay đổi sẽ mất
   từ DB hỏng. Backup này chỉ chứa DB; originals/media tạo sau backup vẫn được giữ.
3. **Giữ nguyên raw source**. DB và sidecar hiện có được giữ riêng với hash,
   fsync và manifest. Raw không phải backup SQLite nhất quán, không chứng minh
   WAL/journal phục hồi được. Window điền raw manifest, xóa lựa chọn preparation
   cũ trong ô nhập; **mọi archive/stage cũ vẫn còn trên đĩa**.
4. Đánh giá lại với raw đã chọn. Chấp nhận mất **mọi thay đổi DB sau thời điểm
   backup**; nếu có media issue, xác nhận riêng đã xem chúng. Bấm **Staging từ
   backup đã đánh giá**. Đây là DB riêng qua Backup API và migration, chưa copy
   vào live DB, chưa activate hoặc khôi phục.
5. Đánh giá lại với preparation manifest vừa nhận. Consent luôn trở về chưa chọn
   sau staging hoặc đổi bất kỳ lựa chọn. Kiểm stage khớp nguồn/candidate/media
   hiện tại, xác nhận loss/media lại, rồi **Copy đã xác nhận và giữ guard**.
   Raw/stage cũ không khớp nguồn hiện tại không được dùng để copy; giữ raw mới và
   preparation mới. Không sửa/xóa bằng chứng cũ để làm cho review khớp.
6. Copy thành công bàn giao journal sang Maintenance và đóng dialog. Guard vẫn
   chặn Library. Chọn **Đánh giá copy DB hỏng**, đọc actual state/media, rồi chỉ
   khi `VALIDATED_COPY_GUARDED` và media hợp lệ mới xác nhận **fresh health** riêng.
   [Recovery](CORRUPT_COPY_RECOVERY.md) chạy actual candidate health, activation
   và metadata dưới lock trước khi clear guard. Exit0 của review/raw/staging/copy
   không có nghĩa toàn bộ restore đã hoàn tất.

Nếu hủy, timeout, crash hoặc lỗi, giữ mọi bytes, log, journal, original retention,
raw archive, backup, stage và installation trước. Dialog dùng owned command/Job,
không kill PID lấy từ record. Xem thư mục log hiển thị trong trạng thái; không
đưa nội dung riêng ra issue công khai. Đánh giá lại actual-state journal trong
Maintenance. [Resume](CORRUPT_COPY_RESUME.md) cần consent riêng cho empty target
hoặc UNKNOWN bundle và complete originals retained exactly once; không đoán DB
cần giữ hay tự xóa WAL/SHM/hot journal. Changed completed original-copy receipt
vẫn bị từ chối và cần fresh restore decision riêng; không dùng partial consent
để bỏ qua kiểm tra này.

Combined CLI dùng để đọc cùng proof, không cấp quyền apply:

```powershell
CreatorLoop.exe --review-damaged-restore BACKUP_ID --installation-root 'D:\Bản cài Creator Loop' --restore-candidate 'D:\Bản cài Creator Loop\CANDIDATE'
```

Thêm `--review-raw-source RAW_MANIFEST` và/hoặc
`--review-damaged-preparation PREPARATION_MANIFEST` khi muốn kiểm bằng chứng đã
giữ. Metadata/path/root binding, archive bytes và closed stage được kiểm lại
trong child có timeout; UI không tự hash DB/media. Exit2 thiếu/mixed arguments,
exit3 app lock bận, exit4 từ chối/lỗi. Nguồn thiếu hoặc guard đang có phải đi qua
actual-state inspection/recovery phù hợp, không initialize DB mới.

Source Qt/owned CLI và exact artifact proof có scope riêng trong
[HANDOFF](HANDOFF.md). Đây chưa là nghiệm thu full frozen parent/product,
engine/model thật, máy8GB, physical volumes hoặc release. Điều kiện V1 giữ trong
[PRODUCT_ACCEPTANCE](PRODUCT_ACCEPTANCE.md).
