# RAW → Evidence MODEL

## Đối chiếu và tạo Evidence

1. Trong Library, chọn task **AUDIO_TRANSCRIPTION / SUCCEEDED** rồi mở
   **Chọn đoạn từ task chép lời máy**. Bấm **Đọc lại RAW** và chọn rõ một đoạn;
   app không chạy lại engine khi đọc bản chép lời đã lưu.
2. Đọc toàn bộ text máy, bấm **Đối chiếu âm thanh gốc** rồi nghe đoạn đã chọn.
   Chỉ dùng kết quả sau khi đã đối chiếu; text máy có thể sai hoặc bịa nội dung.
3. **Tạo Evidence MODEL chờ duyệt** tạo Version mới ở PENDING, không ghi ACCEPT.
   **Mở lại Library** để mở Evidence Audio, sửa thành Version HUMAN nếu cần
   và review riêng. RAW và các Version trước vẫn được giữ.

Nếu thao tác bị hủy hoặc kết quả chưa rõ, kiểm lịch sử trước khi tạo lại.
Không coi task SUCCEEDED hoặc tạo Evidence thành nghiệm thu chất lượng.

## Ràng buộc dữ liệu và tác vụ

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

## Phạm vi nghiệm thu

Thao tác trên bản cụ thể cần đối chiếu checksum, manifest và thông tin build.
Hướng dẫn này không chứng nhận chất lượng tiếng Việt, tài nguyên máy8GB hoặc
release. Tạo MODEL, sửa HUMAN và quyết định review là các bước riêng.
