# Bàn giao hiện tại — Gate 2 Library/Evidence

**Ghi nhận:** 01/10/2026. Đây là trạng thái làm việc tại thời điểm ghi, cần đối chiếu lại với Git khi tiếp tục. [Biên bản Gate 1](../Creator_Loop_Gate_1_Status.md) được giữ làm lịch sử, không phản ánh tiến độ hiện tại.

## Mục tiêu và điều kiện hoàn thành

Theo [Data Architecture V1.1](Creator_Loop_Data_Architecture_V1_1.md), [CI/CD & Release Contract V1](Creator_Loop_CICD_Release_Contract_V1.md), [Layout Contract V1](Creator_Loop_Layout_Contract_V1.md) và [Master Prompt V2](Codex_Master_Prompt_V2.md), Gate 2 cần Library/Evidence cho Video, Image, Text: Asset là identity logic, nhiều Source có thể nối một Asset; original được giữ bất biến dưới data root, có `asset_files` và `processing_runs`; Evidence/version gắn đúng `anchor_file_id` và digest, locator có schema và mở lại đúng nguồn. `TIME_RANGE` dùng milliseconds, `IMAGE_REGION` dùng tọa độ chuẩn hóa, `TEXT_RANGE` dùng chỉ số Unicode code point trên snapshot NFC, `WHOLE_ASSET` chỉ dùng khi toàn nguồn liên quan. Giữ lịch sử version, phân biệt xử lý với review, giữ đúng nghĩa NULL và các ràng buộc SQLite/FK. Chỉ coi Gate 2 hoàn tất sau khi các phần áp dụng được triển khai, kiểm thử phù hợp qua local và required CI; một slice TEXT intake không đóng toàn Gate 2. Claim/review workflow và các gate sau không thuộc slice hiện tại.

## Git và slice đang làm

- Branch: `gate2-text-original-intake`.
- Commit nền, cũng là `main` và `origin/main` tại lúc kiểm tra: `1f0f32973e03b3b89ae18013a5291aa8d2ba3437` (merge PR #4, original integrity verifier).
- **Mốc code đã local validation:** `25ea4e40e38204cfce4c8427e96eb74297c35708` — commit 1, chỉ gồm `app/creator_loop/library.py`, `app/creator_loop/text_intake.py`, `app/creator_loop/windows_owned_file.py`, `tests/test_text_intake.py`. Local validation bên dưới áp dụng cho đúng nội dung code/test của commit này.
- `AGENTS.md` và `docs/HANDOFF.md` được lưu trong commit tài liệu riêng (commit 2). Hash commit tài liệu và HEAD hiện hành lấy từ Git; tài liệu này không tự ghi hash commit chứa chính nó. Hai file này là hướng dẫn/bàn giao, không phải thay đổi code/test.
- Hai commit chỉ lưu cục bộ trên branch; chưa push, chưa tạo PR. Required CI chưa xác nhận.
- Slice được duyệt: intake vật lý TEXT original, giữ byte gốc, tạo đích độc quyền, đăng ký Asset/ORIGINAL trong transaction, rollback chỉ xóa file được sở hữu qua Windows handle. Không đổi schema, UI, Evidence reopening, Video/Image hay Gate 3.

## Đã làm và bằng chứng trong phiên

- `library.py` cho phép hoãn commit của `create_asset_file` để transaction của intake bao cả Asset và ORIGINAL.
- TEXT intake đọc source ở chế độ nhị phân, tạo đích bằng `CreateFileW(CREATE_NEW)` với share mode 0; giữ một handle qua copy, đọc lại byte để tính SHA-256/size và quyết định commit/rollback. Lỗi đăng ký dùng `SetFileInformationByHandle(FileDispositionInfo)` trên handle đang giữ; không có fallback xóa theo pathname trong production. Lỗi cleanup giữ orphan và báo lỗi. Đã thêm vòng lặp write-all; write không tiến triển làm intake thất bại.
- Tám test TEXT intake tập trung **PASS 8/8 sau sửa short-write** bằng `$env:PYTHONPATH = 'app'; python -m unittest discover -s tests -p test_text_intake.py -v`. Gồm short write hoàn tất với byte chính xác, write dừng tiến triển không để bản ghi DB hợp lệ, và test Windows bằng process thứ hai: rename bị từ chối mã 32, replace bị từ chối mã 5.
- Kết quả 8/8 tập trung phía trên thuộc lần kiểm tra trước; đợt validation sau sửa định dạng bên dưới là căn cứ cho mốc code đã commit.

## Validation và review ngày 01/10/2026

Đã chạy đủ các lệnh liệt kê trong handoff trước đó trên branch `gate2-text-original-intake`, trước khi tạo commit code. Khi đó HEAD là `1f0f32973e03b3b89ae18013a5291aa8d2ba3437` (cùng commit với `main` và `origin/main`). Không sửa code/test trong đợt kiểm tra này.

| Lệnh | Kết quả lần này |
|---|---|
| `ruff check app tests scripts` | PASS |
| `ruff format --check app tests scripts` | **FAIL**: `tests/test_text_intake.py:162` và `:188` cần xuống dòng lời gọi `patch.object`; 1 file cần format, 25 file đã đúng. |
| `mypy app/creator_loop` | PASS, 15 source files. |
| `python -m compileall -q app tests` | PASS. |
| `$env:PYTHONPATH = 'app'; python -m unittest discover -s tests -v` | PASS, 63 test: 62 qua, 1 skip. Tám test TEXT intake đều qua; test Windows xác nhận rename bị từ chối mã 32 và replace mã 5. |
| `$env:PYTHONPATH = 'app'; python -m unittest discover -s tests -p test_originals.py -v` | PASS, 12 test: 11 qua, 1 skip. |
| `git diff --check` | PASS; chỉ bao phủ thay đổi tracked. |
| `git status --short` | Chạy thành công; tại thời điểm đó `library.py` modified, ba file code/test mới và hai file tài liệu untracked. |

Test bị skip trong cả hai lần unittest là `test_symlink_escape_is_rejected`: tài khoản Windows hiện tại không có quyền tạo symlink (`WinError 1314`). Không có lệnh nào trong danh sách chưa chạy. Required CI chưa được chạy/xác nhận cho commit code.

Review đã đọc diff `library.py` và toàn bộ ba file code/test untracked. `create_asset` không tự commit; `create_asset_file(..., commit=False)` giữ Asset và ORIGINAL trong cùng transaction. Intake đọc/ghi byte nhị phân, xử lý short write, flush/fsync rồi đọc lại từ handle để tính digest/size. Tạo đích độc quyền và rollback qua handle đang giữ; các test kiểm việc giữ file có sẵn, lỗi ghi/DB, orphan khi disposition thất bại, và chặn rename/replace từ process khác. Chưa thấy lỗi logic cụ thể khác trong phạm vi review này. Lỗi format ở hai vị trí trên là kết quả lịch sử của lần kiểm tra trước; đã sửa và kiểm lại bên dưới. `git diff --check` không kiểm được ba file untracked; đã đọc trực tiếp và Ruff có bao phủ chúng.

## Sau sửa định dạng ngày 01/10/2026

Chỉ xuống dòng hai lời gọi `patch.object` tại `tests/test_text_intake.py:162` và vị trí trước đó ở `:188` (nay dịch dòng); không đổi biểu thức, assertion, dữ liệu kiểm thử hoặc điều kiện skip. Đã review hai hunk trước/sau sửa; thay đổi chỉ là định dạng. Không sửa code production.

| Kiểm tra trên bản sau sửa | Kết quả |
|---|---|
| `ruff format --check app tests scripts` | **PASS**, 26 file đúng định dạng; lỗi trước đã hết. |
| `ruff check app tests scripts` | PASS. |
| `mypy app/creator_loop` | PASS, 15 source files. |
| `python -m compileall -q app tests` | PASS. |
| `$env:PYTHONPATH = 'app'; python -m unittest discover -s tests -v` | PASS, 63 test: 62 qua, 1 **SKIP**. Tám test TEXT intake qua; Windows rename bị từ chối mã 32, replace mã 5. |
| `git diff --check` | PASS cho thay đổi tracked; file test untracked được bao phủ bởi Ruff. |

Test vẫn **SKIP**: `test_symlink_escape_is_rejected` vì tài khoản Windows không có quyền tạo symlink (`WinError 1314`). Không chạy lặp riêng `test_originals.py`: 12 test của file này đã nằm trong full discovery lần này; lần chạy riêng trước sửa có 11 qua, 1 skip và không được dùng làm kết quả mới. Sau validation chỉ stage và commit đúng bốn file code/test, không sửa code thêm. Required CI trên PR, workflow security và Windows artifact chưa được chạy/xác nhận cho commit code. Commit tài liệu không yêu cầu chạy lại test ứng dụng vì không đổi code/test.

## Việc tiếp theo

1. Khi tiếp tục, đối chiếu branch, HEAD và worktree với Git; sau đó quyết định push/tạo PR cho **slice TEXT original intake** theo chỉ đạo tiếp theo. Cần required CI trên PR trước khi coi slice được xác nhận đầy đủ; test symlink vẫn cần môi trường Windows có quyền tạo symlink để kiểm được nhánh đó.
2. Audit riêng các khoảng trống Library/Evidence của Video, Image, Text và việc mở lại Evidence/locator trước khi chọn slice kế tiếp. **Toàn bộ Gate 2 chưa hoàn tất** chỉ vì slice TEXT intake qua local validation.
