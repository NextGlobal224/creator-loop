# Module và interface Creator Loop

Implementation chỉ nằm ở package mới. Các module tên cũ là alias tới cùng module object, để entrypoint và public imports tiếp tục dùng được; không có implementation song song.

| Chức năng | Source | Test | Phụ thuộc |
|---|---|---|---|
| Facebook: kết nối, parser, thu và đánh giá | `backend/facebook/` | `tests/backend/test_facebook_*.py`, `tests/frontend/test_facebook_ui.py` | Library, runtime; Chrome của user nằm ngoài ownership |
| Library, nhập original, storage, metadata | `backend/library/` | `tests/backend/test_library.py`, intake/storage; Library UI | Runtime, Knowledge locator; media ngoài SQLite |
| Evidence, Claim, review và locator | `backend/knowledge/` | Evidence/Claim/locator backend; UI tương ứng | Library, runtime transactions |
| Phân tích/transcription local | `backend/analysis/` | transcription/Whisper backend | Verified components, owned workers; chỉ chạy khi yêu cầu |
| Project, Draft exact version, Selection | `backend/creator/` | Draft/Project/Selection backend và UI | Knowledge, runtime transactions |
| Decode/playback và MP4 local | `backend/video/` | Video backend, UI và integration | Shared DTO, Library, owned runtime; độc lập Facebook |
| Package, quyền media, Approval, Post/Observation | `backend/publication/` | Publication/policy/package backend | Exact Draft, Knowledge, Library |
| DB/migration, locks, components, update/recovery, processes | `backend/runtime/` | Runtime backend, migration/recovery integration | Không import frontend |
| Màn hình/dialog, worker dispatch và preview | `frontend/` | `tests/frontend/` | Gọi service backend; không SQL/viral/render command |
| DTO/interface không thực thi | `shared/workflow.py` | Boundary/integration tests | Python standard library |
| Probe luồng sản phẩm được gọi rõ ràng | `integration/product_flow_smoke.py` | `tests/integration/` | UI + backend; không chạy khi import |

`shared/workflow.py` chốt `ResearchMaterial`, `CreatorRequest`, `DraftSnapshot`, `VerifiedMedia`, `Scene`, `Subtitle`, `RenderPreset`, `RenderRequest`, `Progress`, `RenderResult` và protocol. Draft/render ghim exact Draft Version; inputs ghim file ID/digest. Renderer nhận output directory riêng, callback tiến trình và cancellation, trả status; không tự cấp review/Selection/Approval hoặc publish.

UI workers giữ Qt signals và dispatch. Service `*_operations.py` thực thi thao tác hiện có; `*_snapshots.py` sở hữu transaction đọc, trả snapshot rời DB. Các worker backend được gọi qua port của worker; không import class frontend. Entry `creator_loop.__main__` là composition root duy nhất, chọn worker task hoặc UI theo yêu cầu CLI.

Discovery: `python -m unittest discover -s tests -t .`; test có thể chạy riêng bằng `python -m unittest tests.backend.test_facebook_research`. Dùng runner timeout/log theo WORKFLOW cho test có thể treo. Không chạy toàn bộ local chỉ vì thay cấu trúc.

Mốc tách module: lint/format và mypy đạt; boundary/import, bootstrap, Facebook research/discovery/UI đạt. Creator/Project UI đạt sau cập nhật seam mock; log lỗi đầu giữ riêng. Chưa suy các kết quả này thành nghiệm thu Facebook mới, phân tích/tạo Draft tự động hoặc MP4.
