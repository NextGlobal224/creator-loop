# Sao lưu, cập nhật và khôi phục qua giao diện

Trong Library, chọn **Đóng Library để sao lưu / cập nhật / khôi phục**. Library
đợi tác vụ đang chạy kết thúc và đóng trước khi nhả khóa dữ liệu. Nếu ứng dụng
không mở được vì đang có restore guard, mở cửa sổ riêng:

```powershell
$env:CREATOR_LOOP_DATA_ROOT = 'D:\Dữ liệu Creator Loop'
CreatorLoop.exe --maintenance
```

Bản source dùng `python -m creator_loop --maintenance` với `PYTHONPATH=app`.
Cửa sổ maintenance không khởi tạo, migrate hoặc mở DB lúc khởi động. Nó có thể
mở khi DB hỏng hoặc đang có guard; điều này **không** chứng minh DB có thể
khôi phục. Từng thao tác bên dưới vẫn kiểm compatibility, locks và dữ liệu thật.

**Backup:** đóng các phiên dùng cùng data root rồi chọn **Tạo backup DB hiện tại**.
Backup có ID, thời điểm, schema, digest và được mở/validate trước khi công bố
trong `backups/<id>/`. Backup chỉ chứa SQLite, không chứa media. Giữ media riêng.
Chưa tự động xóa backup hay bản cài cũ; người dùng quản lý dung lượng sau khi
có bằng chứng khôi phục. Xem [backup](UPDATE_BACKUP.md).

**Cập nhật:** chọn thư mục chứa các bản cài, ZIP và manifest đi cùng ZIP. Chọn
**Kiểm ZIP và staging** để kiểm exact bytes, file inventory và migration package.
Đọc cảnh báo không có downgrade, tích xác nhận nâng schema rồi chọn
**Backup và nâng schema DB**. Backup hiện tại được giữ trước migration. Chọn
đúng journal vừa tạo trong `manifests/`, sau đó **Activate và kiểm sức khỏe**.
Chỉ bản qua health mới thành active. Không chọn bản cài theo thư mục mới nhất.
Xem [preparation](UPDATE_PREPARATION.md) và [activation](UPDATE_ACTIVATION.md).

**Cập nhật gián đoạn:** chọn journal cụ thể và thư mục các bản cài; chọn
**Kiểm trạng thái cập nhật** trước khi quyết định. **Tiếp tục cập nhật đã chọn**
cần xác nhận nâng schema và giữ backup current-state. Nó dựa vào schema/history
thật, không đoán commit từ phase và không tự restore DB. Xem
[update recovery](UPDATE_RECOVERY.md).

**Khôi phục DB:** chọn thư mục các bản cài, candidate đã staging và backup ID
cụ thể (nút chọn backup lấy ID từ thư mục backup). Chọn **Đánh giá backup trước
khôi phục**. Đọc thời điểm backup, schema, số dòng và các media thiếu/thay đổi.
Mọi thay đổi DB sau thời điểm đó có thể mất, kể cả sửa mà số dòng không đổi.
Media mới và các backup/bản cài cũ được giữ; backup DB không khôi phục media bytes.

Xác nhận mất thay đổi mặc định chưa chọn. Nếu có media issues, phải xác nhận
riêng rằng đã xem chúng. Sau khi đọc và tích các xác nhận, chọn **Khôi phục DB
theo đánh giá đã xác nhận**. Đổi bất kỳ lựa chọn nào sẽ xóa đánh giá và các xác
nhận; backend cũng kiểm lại DB/WAL/backup/candidate/registry/media dưới locks.
Lệnh bảo toàn backup current-state trước copy. Acknowledgement không biến media
lỗi thành healthy: activation vẫn bị chặn đến khi mọi reference có đúng bytes.
Xem [confirmed restore](RESTORE_APPLY.md).

**Khôi phục gián đoạn:** giữ DB, backup, journal, guard và log. Chọn đúng journal
được ghi trong `runtime/restore-in-progress.json`, rồi **Giải quyết khôi phục bị
gián đoạn**. Recovery không copy backup lần nữa. Nếu DB gốc còn nguyên, journal
ghi `RESTORE_NOT_APPLIED`; nếu DB restored đúng, nó kiểm media và chạy fresh
health/metadata trước khi bỏ guard. Trạng thái không rõ hoặc dữ liệu bị đổi vẫn
bị chặn. Không xóa guard để ép Library mở.

**Copy từ nguồn hỏng đã có guard:** dùng [workflow damaged-source riêng](CORRUPT_RESTORE_PREPARATION.md)
để review/giữ raw/stage/copy trước; các bước ban đầu này vẫn dùng CLI. Sau đó chọn
đúng `corrupt-restore-ID.json` và thư mục các bản cài, chọn **Đánh giá copy có guard**.
UI đọc published backup/thời điểm/schema/candidate, proof trạng thái thực và
media hiện tại; không initialize DB thiếu/hỏng hoặc cấp quyền restore từ phase.
Đổi lựa chọn hay lỗi sẽ xóa proof và mọi consent.

**Tiếp tục copy đã đánh giá** cần xác nhận mất mọi thay đổi sau backup.
Target rỗng cần xác nhận giữ riêng; UNKNOWN partial cần consent riêng giữ nguyên
DB và các tệp phụ, chỉ khi originals còn retained đầy đủ/đúng một bản và copy
gốc chưa có completed physical receipt. Nếu media có vấn đề, phải xác nhận riêng.
Copy giữ original/raw/partial/journals, không xóa/overwrite/replay WAL; giữ guard.
Đọc [resume](CORRUPT_COPY_RESUME.md) cho refusal và split bundle sau crash/cancel.

Sau copy, **đánh giá lại**. Chỉ actual `VALIDATED_COPY_GUARDED` cùng mọi media valid
mới bật **Khôi phục copy và kiểm sức khỏe**, sau consent chuyển sang candidate đã
chọn và fresh health. Backend giữ locks/evidence/writer reservation xuyên real
health, pointer/metadata và guard clear. Consent mất thay đổi/media ở copy không
thay consent health hoặc chữa media lỗi. Xem [recovery](CORRUPT_COPY_RECOVERY.md).
Originals thiếu/trùng/foreign hoặc completed copy đổi bytes cần fresh restore
decision riêng; không bật nút để đoán/xóa evidence. UI có scroll khi thiếu chiều cao.

Mỗi thao tác chạy trong native Windows Job riêng, được quan sát bằng Qt timer
với deadline 300s. **Hủy tác vụ đang chạy** hoặc đóng cửa sổ chỉ dừng cây process
của thao tác đó. Hủy/timeout sau khi copy bắt đầu có thể để lại guard cần recovery;
không hứa dữ liệu chưa đổi. Output vượt 1 MiB cũng dừng thao tác để bảo vệ budget.
UI giữ phản hồi khi child chạy. Log/ownership được giữ tại `logs/maintenance-<id>/`;
không kill process theo PID được ghi trong file.

Exit 0 báo lệnh kết thúc; đọc journal để phân biệt `NOT_APPLIED`, `PREPARED` và
`COMPLETED`. Exit 2 là tham số không hợp lệ, 3 là contention, 4 là refusal hoặc
incomplete. Không tiếp tục activation/release dựa trên thông báo chung hoặc
phase chưa kiểm chứng. Các giới hạn corrupt/missing/future-schema restore,
physical-volume, engine/model/máy8GB và final release còn ghi trong nghiệm thu.
