# Adapter Whisper — checkpoint phát triển

`whisper_adapter.py` là adapter PCM WAV cục bộ, chưa nối Library/pipeline/UI.
Không ghi DB, tạo Evidence, review hoặc tự chấp nhận nội dung AI. Caller phải
chạy trong owned I/O worker có timeout cấp tiến trình; hashing/file I/O không
được đặt trên GUI thread. Caller hiện giữ app lock của đúng data root.

Profile đã chọn: whisper.cpp1.8.7 CPU và base multilingual tại revision trong
[quyết định engine/model](ENGINE_MODEL_PROPOSAL.md). Caller cung cấp sáu
ComponentSpec được chọn rõ: EXE, bốn DLL và model. Adapter giữ verified leases
suốt tác vụ, từ chối file lạ trong runtime directory, kiểm identity/digest WAV
PCM16/16kHz/mono; một tác vụ tuần tự, noGPU/OMP2/beam1/best1, Job memory cap
và deadline được cấp rõ. Không đổi component hoặc tăng cap tự động khi lỗi.

RAW JSON giữ nguyên bytes/text/hash trong workspace có parent/child creation
identity ghi trước resume. Adapter trả RAW, suggested segments, input/component
hashes và native ownership/memory/timing. Offset không hợp lệ bị từ chối, không
clipping; parse thành công không chứng minh speech/quality/locator/human review.
Giữ workspace/log khi lỗi, cancel hoặc crash; chưa cleanup/DB recovery cho loại
workspace này. Không xóa component/input ngoài quyền sở hữu.

Fake native CLI trong tests kiểm normal/cancel/timeout/native failure/input
digest/parent crash, không tải weights vào CI. Real tests và FAIL gốc tra HANDOFF:
512MiB init FAIL, 768MiB/default5beams KV cache FAIL; 768MiB/beam1/best1 chạy
được silence và synthetic known English, peak commit khoảng750–751MiB. Margin
còn hẹp, không lấy số này làm final resource budget. Silence sinh câu không
có trong nguồn, nên chất lượng tiếng Việt và dữ liệu thực vẫn chưa xác minh.

Còn thiếu: video→bounded PCM, durable RAW registration (`asset_files.role=OTHER`,
MIME JSON, lineage/run đúng schema hiện hành), QUEUED/RUNNING/terminal states,
recovery dưới fresh app lock, component selection budget, progress/cancel UI,
Vietnamese quality, long input, RAM/disk pressure và exact final artifact.
Không thêm enum/table chỉ để lưu transcript; SQLite vẫn là nguồn chuẩn metadata.
