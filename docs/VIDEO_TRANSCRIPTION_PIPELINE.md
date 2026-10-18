# Video → PCM → RAW — checkpoint service

Service/recovery đã merge PR78, task/UI/budget/log privacy đã merge PR79.
Caller là owned I/O process, giữ app lock đúng root,
Qt event loop và timeout cấp tiến trình cho toàn lượt gồm preflight/SQLite/I/O.
Source UI đã nối owned task/progress/cancel và Library handoff. Exact CI79
frozen task/decoder đã chạy selected engine trên known English VIDEO và kiểm
RAW/lineage/reopen trong phạm vi SOURCE Qt caller; bằng chứng/bytes tại
[HANDOFF](HANDOFF.md). Chưa whole frozen frontend/quality/resource/release.
Không gọi services trực tiếp trên GUI thread.

Trong Library, chọn original VIDEO rồi **Chép lời Video bằng engine/model đã
chọn**. Library đóng và nhả app lock trước khi mở cửa sổ tác vụ. Chọn ngôn ngữ
và budget khớp component selection đã lưu, rồi tạo PCM/chép lời. Worker lấy
lock mới, kiểm bytes/lineage và xử lý tuần tự; GUI không hash model hoặc ghi DB.
**Mở lại Library** chỉ sau khi cây worker thoát và lấy lock mới để đọc/reconcile.

Task outer Job1024MiB/deadline420s bao phủ preflight/I/O/decode/transcription;
decoder cap512MiB/120s giữ nguyên, selected Whisper worker512 hoặc768MiB/240s
được cấp rõ (mặc định component CLI vẫn512). Không tăng budget tự động khi lỗi.
Request/workspace và native child identity được bind trước resume; private CLI
từ chối unbound/malformed request trước DB. Hủy/đóng gửi cancel hợp tác; sau12s
chưa terminal mới dùng owned-tree fallback, chờ tối đa8s rồi giữ cửa sổ/ownership
nếu chưa chứng minh thoát. Interrupted run dùng witness recovery khi mở lại.

`create_video_audio_derivative` kiểm VIDEO ORIGINAL MP4 bất biến và duration,
chạy Qt streaming decoder trong Job512MiB; giữ inherited read-only original
handle và khôi phục file position. PCM16/16k/mono giới hạn64MiB, complete
zero-origin track; gap/overlap vượt1sample timestamp rounding, thiếu audio,
vượt budget, deadline/cancel bị từ chối, không stitch/clipping. Derived WAV mới
ghi/fsync/verify trước atomic `asset_files` lineage + run SUCCEEDED. Mỗi lượt
tạo file/run riêng, không ghi đè original hoặc output cũ.

`transcribe_audio_derivative` nhận DERIVED_AUDIO thuộc VIDEO, parent ORIGINAL
và decode run SUCCEEDED; tạo run AUDIO_TRANSCRIPTION riêng. [Adapter](WHISPER_ADAPTER.md)
giữ verified input/components và chạy selected engine với explicit budget.
RAW JSON giữ exact bytes trong workspace; đăng ký roleOTHER/MIME JSON với
parent PCM/run, cùng immutable provenance artifact liên kết RAW. Provenance
giữ identity/source/license/hash của sáu components, settings/ownership và số
đo thực; SQL vẫn là nguồn chuẩn domain state. Không dùng provenance JSON làm
projection khác của trạng thái run hoặc nguồn chuẩn quan hệ nghiệp vụ.

Run đi QUEUED → RUNNING → SUCCEEDED/FAILED/CANCELLED; terminal có finished_at.
Decode và transcription độc lập: transcription lỗi không đổi decode success.
Lỗi registration/cancel giữ RAW/provenance/workspace, không suy success từ file
còn trên đĩa. Crash reconcile theo [processing recovery](PROCESSING_RECOVERY.md)
chỉ với known executor witness và native owner dead; unknown/live giữ nguyên.
Không tạo Evidence hoặc review event tự động; machine text cần kiểm với nguồn.

Finding thực: selected Whisper base sinh lời trên silence/tone, có lỗi tiếng
Việt và có thể đề xuất range ngoài nguồn ngắn. Adapter từ chối range invalid,
không tự clipping/ACCEPT hoặc đổi model. Bằng chứng scoped/real và FAIL trong
HANDOFF; full838/CI PR77 thuộc foundation trước PCM/pipeline WIP, không chứng
minh service mới. Full869/12SKIP service checkpoint đã PASS, source260 hash không đổi;
Service đã qua requiredCI PR78; task UI/budget/log privacy còn LOCAL: full885
(873PASS/12SKIP) đã đạt, source265 hash không đổi; chưa requiredCI/exact artifact
mới. Probe source UI với synthetic known
English MP4 + selected engine thật đã lưu RAW/provenance và khớp3segments;
không nghiệm thu chất lượng tiếng Việt hoặc long input/RAM pressure. Native
stdout/stderr bị đưa vào NUL; log chỉ ownership/outcome/resource, RAW giữ nguyên.
Source GUI-parent crash với engine thật đã xác minh nested task/engine chết trước
outer Job cleanup, lock lấy lại được và known ASR run thành FAILED khi recovery.
Còn resource/long input, prerequisites, exact frozen build, walkthrough và release.
