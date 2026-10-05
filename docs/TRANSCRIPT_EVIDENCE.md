# RAW → Evidence MODEL — source đang phát triển

`transcript_evidence.py` đọc bản chép lời lịch sử đã đăng ký, không chạy lại
engine hoặc yêu cầu component selection hiện tại. Caller phải là owned I/O
process có deadline toàn lượt và giữ app lock của đúng root; không gọi trên
GUI thread. Không initialize/migrate DB khi đọc hoặc tạo Evidence.

SQLite xác nhận VIDEO còn hoạt động, ORIGINAL MP4, PCM cùng Asset với decode
run SUCCEEDED và RAW/provenance với transcription run SUCCEEDED/profile đã chọn.
Reader giữ verified leases của original, PCM, RAW và provenance. JSON provenance
có digest và ràng buộc IDs khớp SQL; nó chỉ cung cấp settings lịch sử, không
thay nguồn chuẩn quan hệ hoặc trạng thái run. RAW bị giới hạn8MiB, provenance64KiB;
range phải nằm trong PCM và original, không tự clipping hoặc nối range.

Chọn một segment bằng index cùng digest đã đọc tạo Evidence SPEECH/version1:
`producer_type=MODEL`, `processing_run_id` là run chép lời, `created_by=NULL`,
content giữ đúng text máy. Anchor là ORIGINAL VIDEO với TIME_RANGE milliseconds
và `track=audio`; không neo RAW JSON như một đoạn TEXT. Mỗi lựa chọn tạo identity
mới, không overwrite RAW/version cũ. Không tạo review event hoặc ACCEPT; Evidence
còn PENDING. Việc sửa nội dung của người dùng phải append HUMAN version theo
flow hiện có, rồi review riêng sau khi đối chiếu nguồn.

Library chọn RAW chính xác theo parent PCM/cùng Asset/run, không lấy output
đầu tiên (có thể là provenance). Library đóng/nhả lock trước cửa sổ lựa chọn;
owned worker giữ app lock mới và native request/parent/child/Job binding trước
khi đọc hoặc ghi. Khi quay lại Library, launcher lấy lock mới trước recovery.

Worker có deadline120s, Job256MiB; preview Qt/decoder dùng outer Job512MiB,
deadline giữ nguyên. Giao diện nhận frame UTF8 tối đa1MiB qua private stdout
pipe, đọc overlapped không chờ trên GUI, tối đa128KiB/tick. Nội dung chép lời
không vào stdout/stderr log mặc định; logs chỉ giữ ownership, workspace giữ
request IDs/settings và native binding. RAW/provenance gốc vẫn nằm trong storage.
Danh sách mỗi trang25 đoạn, preview200 ký tự; chọn đoạn mới đọc toàn text giữ
nguyên. Không mặc định chọn row/tạo Evidence. Nghe source dùng playback owned
hiện có; cửa sổ preview/Library handoff phải chờ native tree và pipe cleanup
được xác nhận. Pending cancellation giữ buffer/lease; unknown outcome giữ
workspace/lịch sử và yêu cầu kiểm SQL trước khi tạo lại, không tự retry.

Source WIP có scoped service/worker/GUI/privacy/native cancellation/budget,
actual range PCM và Library reopen tests; probe service trên bản sao RAW thực
English giữ phạm vi riêng. Full source919/907PASS/12localSKIP đạt; actual source
launcher/copied real English RAW/private worker/source PCM/MODEL PENDING/Library
reopen và parent-crash worker-dead-before-outer-cleanup đạt phạm vi riêng, log
và source hash tại HANDOFF. Chưa public payload/CI, build mới hoặc nghiệm thu
chất lượng tiếng Việt/resource/release. CI883/build CI79 xác
minh task UI trước RAW review, không xác minh WIP này.
