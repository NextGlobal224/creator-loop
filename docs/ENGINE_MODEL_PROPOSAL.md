# Engine/model V1 — lựa chọn đã chốt, runtime chưa nghiệm thu

Người dùng chốt ngày05/10/2026: whisper.cpp v1.8.7 Windows x64 CPU + Whisper base
multilingual theo nguồn/revision dưới đây; cho phép tải vào thư mục riêng ngoài
Git/build, kiểm license/hash/runtime rồi triển khai/test adapter. Nghiệm thu
engine/model vẫn chưa xác minh; quyền này không chứng minh compatibility.

Đối chiếu trước quyết định: Gate0 chọn adapter subprocess, fake engine cho CI và
component ngoài application release; LOCAL_COMPONENTS có kiểm khai báo/history,
chưa chọn hoặc nghiệm thu engine/model thật. FFmpeg/whisper.cpp/vision trong
decision record là hướng tích hợp, không chứng minh đã có file/phiên bản/license
được chọn. Đề xuất này không đổi contract, phạm vi hoặc trạng thái nghiệm thu.

| Thành phần | Đề xuất cụ thể | Nguồn/license |
|---|---|---|
| Engine ASR | whisper.cpp **v1.8.7**, Windows x64 CPU, asset `whisper-bin-x64.zip` (4,386,743bytes theo release API) | [Release chính thức](https://github.com/ggml-org/whisper.cpp/releases/tag/v1.8.7); [source license MIT](https://github.com/ggml-org/whisper.cpp/blob/v1.8.7/LICENSE). License/prerequisites của các file thực trong ZIP vẫn phải inspect sau khi có bytes. |
| Model ASR | OpenAI Whisper **base multilingual**, GGML `ggml-base.bin`, HF revision `5359861c739e955e79d9a303bcbc70fb988958b1` | [Nguồn conversion được upstream chỉ dẫn](https://github.com/ggml-org/whisper.cpp/blob/v1.8.7/models/README.md); [file tại revision](https://huggingface.co/ggerganov/whisper.cpp/blob/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-base.bin); [MIT cho code/weights OpenAI](https://github.com/openai/whisper#license). |

Model có147,951,465bytes; LFS metadata công bố SHA256
`60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe`.
Bytes đã tải ngoài Git/build khớp size và SHA256 này. Engine archive có SHA256
`d9627486e1c34a03745880485593473e047294260ce9a3cb0aa8deaf15b99af6`,
khớp digest release asset chính thức. Inventory riêng ghi hash của EXE và bốn
DLL CPU đã extract; không đưa component hoặc đường dẫn cá nhân vào Git.

MIT notices nguồn/weights được giữ cạnh component. Static PE inspection xác
nhận năm file AMD64; CLI `--version` chạy trên máy đích báo v1.8.7, exit0.
Runtime dùng MSVC/OpenMP (`MSVCP140`, `VCRUNTIME140`, `VCRUNTIME140_1`,
`VCOMP140`) và Windows UCRT; prerequisite phải khai báo/kiểm ở preflight.
Không tự phân phối DLL hệ thống. Version probe không chứng minh inference,
model compatibility, chất lượng, budget hoặc lifecycle của adapter.

[README v1.8.7](https://github.com/ggml-org/whisper.cpp/blob/v1.8.7/README.md#memory-usage)
ghi base khoảng142MiB disk/~388MB memory và hỗ trợ CPU-only/Windows. Chọn bản
multilingual cho tiếng Việt; chạy tuần tự, dự kiến thử Job budget512MiB hiện có.
Số upstream là ước lượng, không chứng minh peak commit hay tốc độ trên máy8GB.
Probe thực đã giữ FAIL512MiB lúc init và FAIL768MiB với default5beams. Explicit
CPU/OMP2/beam1/best1 tại768MiB chạy được silence và synthetic known English,
peak Job commit khoảng750–751MiB; margin hẹp, chưa final budget/quality/8GB gate.
Silence đã sinh câu không có trong nguồn, giữ như finding chất lượng chưa xử lý.
Windows10 x64/máy đích đã quan sát; CPU instruction support, DLL/runtime,
dung lượng, thời gian/peak memory và chất lượng tiếng Việt phải kiểm thực tế.
Nếu budget không đủ, giữ FAIL và giải quyết budget/component có căn cứ trước nghiệm thu.

Whisper CLI nhận WAV16-bit; audio cần được đưa về PCM16/16kHz/mono theo
[hướng dẫn upstream](https://github.com/ggml-org/whisper.cpp/blob/v1.8.7/README.md#quick-start).
Decode/preview hiện có dùng bundled Qt6.10.2/PySide6, không model ảnh ngoài.
Adapter PCM/WAV→whisper/transcript/provenance đã có LOCAL WIP và scoped real/fake
tests, xem [checkpoint adapter](WHISPER_ADAPTER.md); chưa video/DB/UI integration. Lựa
chọn này không tự xác minh RAW transcript, OCR/vision hoặc engine integration.
FFmpeg chưa được chọn thêm; nếu cần hỗ trợ input ngoài decode Qt đã kiểm,
phải pin và kiểm binary/license riêng trước sử dụng.

Tiếp theo: giữ component ngoài Git/build, hoàn tất prerequisite/runtime checks;
ghi manifest unowned/local-use bằng
đường dẫn thực, không tự cấp ownership hoặc cleanup. Triển khai adapter bounded
worker và kiểm real video/audio mẫu, cancel/timeout/crash/unowned preservation,
RAM/đĩa/thời gian trên máy8GB. Chỉ cập nhật compatibility/acceptance khi có bằng
chứng thật; giữ fake engine trong PR CI và không đưa model vào release.

Metadata nguồn đã lưu tại `.local-test-logs/whisper-v1.8.7-release-metadata.json`
và `whisper-base-model-metadata.json`. Trạng thái tải/file/license/runtime và
adapter thực tế tra HANDOFF; không coi expected source digest là verified bytes.

Probe tiếng Việt sau foundation (LOCAL, chưa full pipeline acceptance): một
FLEURS vi_vn/test CC-BY-4.0, pinned revision70bb2e8…, reference/attribution
giữ ngoài Git. Runtime PASS, WER17.4%/CER7.0%, vẫn có lỗi từ; không tự ACCEPT.
Qt chuyển float WAV sang PCM trong owned Job512MiB; selected Whisper cap768MiB
peak khoảng750MiB. Bằng chứng, source/hash và phạm vi chưa kiểm tra ở HANDOFF.
Không lấy một sample làm nghiệm thu chất lượng tiếng Việt hoặc đổi model tự động.
