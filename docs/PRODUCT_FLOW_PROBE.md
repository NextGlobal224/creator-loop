# Kiểm luồng Qt trên supplied artifact

Đây là QA opt-in cho fixture riêng, không phải thao tác trên kho người dùng.
Mode `--product-flow-smoke WORK_ROOT` chạy trong chính interpreter/EXE được chọn;
không nhận script bên ngoài. Chỉ dùng work-root absolute có `inputs/Original.txt`,
`Original.png`, `Original.mp4` mẫu và chưa có `data/`. Checker tạo mới toàn bộ
fixture, không tải model/engine và từ chối work-root đã có.

```powershell
$env:PYTHONPATH = 'app'
$env:QT_QPA_PLATFORM = 'offscreen'
& .\scripts\run_test_with_timeout.ps1 -Executable .\.venv\Scripts\python.exe `
  -TimeoutSeconds 180 -CommandArgs @('scripts/check_product_flow.py', `
  '--executable','D:\Bản đã giải nén\CreatorLoop\CreatorLoop.exe', `
  '--work-root','D:\QA riêng\product-flow-new')
```

Checker làm việc trong thư mục mới, lưu stdout/stderr của supplied CLI và các
refusal/reopen trong đó; outer runner giữ process deadline/log theo
[WORKFLOW](CODEX_WORKFLOW.md). Không dùng yield hoặc vòng Qt làm timeout tiến trình.
Input là Text NFC mẫu, PNG tổng hợp và MP4 fixture của repo, không phải dữ liệu
người dùng. `CREATOR_LOOP_DATA_ROOT` được đặt vào một sentinel riêng để kiểm
mode không khởi tạo hoặc sửa kho mặc định không được chọn.

Scenario dùng real Qt widgets/slots/workers và SQLite: ba loại intake, TEXT
Evidence create/reopen/ACCEPT; IMAGE_REGION, Video/Audio TIME_RANGE create,
correction thành version mới, ACCEPT và mở lại cả cũ+mới sau khi đóng Library.
WHOLE_ASSET TEXT/IMAGE/VIDEO có consent mặc định bỏ chọn, review và mở đúng
toàn nguồn (ảnh 1:1). Các modal media dùng event loop thật, chỉ tự cung cấp input.
Playback nhận frame/PCM thật từ owned child, kiểm timestamps nằm trong range
đã lưu, rồi chờ process/pipe/anchor lease/workspace được retired trước khi tiếp.
Checker đọc DB độc lập để kiểm 10 version, exact locator và CORRECT/ACCEPT history.
Luồng tiếp tục qua FACTUAL Claim ACCEPT, Project/reference, hai
Drafts/assertions/review/Selection, Package với OWNED image, default-off
Approval/Post confirmation, Post thủ công PUBLISHED trong fixture, Observation
zero/NULL, revoke và historical reopen. Chỉ input dialog/file-picker/message
được điều khiển tự động; không mock domain/DB/worker result, không gửi bài ra
nền tảng. Source tests dùng chung scenario với mode compiled để tránh hai bộ
logic riêng. Checker còn đọc SQLite/digests độc lập, kiểm existing-root/mixed
flags refusal và normal supplied launcher `--compatible-only --ui-smoke` reopen.

Mode giữ app lock cho private data root; không mở DB hiện có. Input thiếu/link/
quá 1 MiB hoặc linked root phải bị từ chối; root đã có `data/` không bị ghi đè.
Failures giữ fixture/log để chẩn đoán. Không tự xóa root, lock, guard hoặc bằng
chứng để chạy lại; chọn root mới khi có căn cứ retry. Các quyền runtime/cancel
vẫn theo contract, không kill process ngoài cây do runner sở hữu.

PASS chỉ áp dụng cho source hoặc exact artifact đã chạy. `frozen=true` phải đến
từ actual supplied EXE, không từ patch `sys.frozen` trong caller. Offscreen và
programmatic inputs không nghiệm thu thao tác người dùng trên Windows vật lý,
manual walkthrough/các tình huống lỗi media trên artifact cuối, engine/model thật,
RAM/đĩa/volumes, powerloss,
independent review, required CI hay tag/release. Giữ các điều kiện đó trong
[bảng V1](PRODUCT_ACCEPTANCE.md); kết quả mới cần source/build/hash/log tại
[HANDOFF](HANDOFF.md). Full798/553.310s và exact070dd73 trước mở rộng media không xác minh
WIP sau mở rộng; kiểm mới phải ghi rõ source/build/hash tương ứng.
