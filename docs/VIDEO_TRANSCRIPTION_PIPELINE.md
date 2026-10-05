# Video → PCM → RAW — checkpoint service

LOCAL service foundation: caller là owned I/O process, giữ app lock đúng root,
Qt event loop và timeout cấp tiến trình cho toàn lượt gồm preflight/SQLite/I/O.
Chưa có GUI task/progress/cancel hoặc frozen final entry point cho pipeline này.
Không gọi services trực tiếp trên GUI thread.

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
còn requiredCI mới, GUI task/selection budget, real speech video,
resource/long input, exact frozen build, walkthrough và điều kiện release.
