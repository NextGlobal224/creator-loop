# Native ownership và readonly health

Checkpoint này bổ sung primitive process tree và health cho updater. Phần
[activation](UPDATE_ACTIVATION.md) được nối ở checkpoint tiếp theo; managed
launcher, recovery/restore và engine/model thật vẫn chưa hoàn tất.

`OwnedWindowsProcess` chỉ chạy trên Windows. Nó tạo child suspended, gán vào
private Job Object có kill-on-close trước resume và giữ process handle suốt lượt.
Assignment thất bại thì child chưa chạy bị dừng bằng handle vừa tạo. Job handle
không được kế thừa; danh sách handle kế thừa chỉ gồm stdin/stdout/stderr. Không có
API nhận PID tùy ý để terminate và không reopen process theo PID. Handle giữ đúng
process object nên PID tái sử dụng không chuyển quyền dừng sang process khác.

Ownership record được fsync vào `ownership.json` trước resume: run/job ID, PID,
canonical executable, creation FILETIME, parent PID và component version. Record
phục vụ chẩn đoán, không tự cấp quyền kill khi chạy lại app. stdout/stderr được giữ
trong thư mục log mới của lượt. Deadline, cancel/context exception và normal exit
dừng toàn bộ Job, chờ active-process count về zero và đóng handle. Owner crash làm
OS đóng handle cuối, dừng cây sở hữu; test giữ riêng handle child/grandchild để
xác minh, có sentinel ngoài Job không bị dừng. Đây chưa phải crash recovery cho
processing_runs hay bằng chứng engine thật; PowerShell test runner là primitive
riêng, không suy kiểm host crash từ test này.

`CreatorLoop.exe --health-check` chỉ mở DB đã tồn tại bằng connection readonly,
kiểm schema/history/checksum/integrity/FK, truy vấn Asset tối thiểu và resolve một
asset-file reference mẫu nếu có. Missing/old schema, processing đang QUEUED/RUNNING,
missing media hoặc size mismatch bị từ chối; mode này không tạo DB, migrate, mở UI
hay chạy model. JSON thành công chứa schema/query/storage status, không chứa media
hay transcript. DB rỗng ghi `not_applicable`, không giả là đã resolve media.

`run_health_check` chạy đúng launcher được caller xác minh, có deadline mặc định
60 giây, native ownership và log riêng. Caller phải giữ app/DB lock của updater.
Exit zero chưa đủ: JSON phải đúng định dạng, schema và query/storage status; chỉ
đọc tối đa 1 MiB stdout để parse. Health failure không restore DB hoặc tự activate
bản cũ. Các log engine trong tương lai cần chính sách giới hạn dung lượng và che
nội dung riêng; primitive này hiện dùng health/fake fixtures, chưa nối engine.

CI giải nén exact ZIP, stage/preparation, rồi `check_candidate_health.py` chạy EXE
candidate dưới AppDataLock và BEGIN IMMEDIATE. Fixture thêm một TEXT reference
thật, yêu cầu `resolved` và kiểm bytes DB không đổi. Outer bounded runner120s giữ
log kể cả health failure; native child deadline60s. Artifact chứa ownership,
stdout/stderr và result. CI này bổ sung smoke schema hiện có; chưa nghiệm thu toàn
product flow, activation hay máy8GB.
