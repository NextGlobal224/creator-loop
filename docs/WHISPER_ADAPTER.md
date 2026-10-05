# Adapter Whisper — checkpoint phát triển

`whisper_adapter.py` là adapter PCM WAV cục bộ; service và source task UI gọi nó
qua [pipeline Video](VIDEO_TRANSCRIPTION_PIPELINE.md).
Không ghi DB, tạo Evidence, review hoặc tự chấp nhận nội dung AI. Caller phải
chạy trong owned I/O worker có timeout cấp tiến trình; hashing/file I/O không
được đặt trên GUI thread. Caller hiện giữ app lock của đúng data root.

Profile đã chọn: whisper.cpp1.8.7 CPU và base multilingual tại revision trong
[quyết định engine/model](ENGINE_MODEL_PROPOSAL.md). Caller cung cấp sáu
ComponentSpec được chọn rõ: EXE, bốn DLL và model. Adapter giữ verified leases
suốt tác vụ, từ chối file lạ trong runtime directory, kiểm identity/digest WAV
PCM16/16kHz/mono; một tác vụ tuần tự, noGPU/OMP2/beam1/best1, Job memory cap
và deadline được cấp rõ. Không đổi component hoặc tăng cap tự động khi lỗi.

[Preflight version](LOCAL_COMPONENTS.md#kiểm-phiên-bản-engine-đã-lưu-trên-windows)
là lệnh riêng `--check-whisper-runtime` và action Components: fresh hashes/leases,
held PE headers và actual CLI `--version` dưới native Job/private bounded pipe.
Nó không gọi adapter inference, không nâng saved compatibility, không chứng
minh model/quality/resource8GB hoặc clean-target prerequisite. Bằng chứng source
version-only và build tương ứng tra HANDOFF; không gán cho artifact cũ.

RAW JSON giữ nguyên bytes/text/hash trong workspace có parent/child creation
identity ghi trước resume. Adapter trả RAW, suggested segments, input/component
hashes và native ownership/memory/timing. Offset không hợp lệ bị từ chối, không
clipping; parse thành công không chứng minh speech/quality/locator/human review.
Giữ workspace/log khi lỗi, cancel hoặc crash; chưa cleanup/DB recovery cho loại
workspace này. Không xóa component/input ngoài quyền sở hữu.
CLI có thể in transcript lên stdout/stderr, nên output native mặc định chuyển
vào NUL. Log engine chỉ có ownership và outcome/exit/resource; RAW trong
workspace là bản nội dung nguyên vẹn. Log cũ giữ nguyên đúng scope lịch sử.

Fake native CLI trong tests kiểm normal/cancel/timeout/native failure/input
digest/parent crash, không tải weights vào CI. Real tests và FAIL gốc tra HANDOFF:
512MiB init FAIL, 768MiB/default5beams KV cache FAIL; 768MiB/beam1/best1 chạy
được silence và synthetic known English, peak commit khoảng750–751MiB. Margin
còn hẹp, không lấy số này làm final resource budget. Silence sinh câu không
có trong nguồn, nên chất lượng tiếng Việt và dữ liệu thực vẫn chưa xác minh.

PCM/durable RAW (`asset_files.role=OTHER`, MIME JSON, lineage/run)/terminal states
và known-executor recovery đã có trong service PR78. Budget/progress/cancel UI
đã merge PR79 với required checks đạt; artifact/probe scopes ở HANDOFF. Còn Vietnamese
quality, long input, RAM/disk pressure/prerequisites và exact final artifact.
Không thêm enum/table chỉ để lưu transcript; SQLite vẫn là nguồn chuẩn metadata.
