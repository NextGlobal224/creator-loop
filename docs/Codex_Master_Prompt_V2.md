# Codex Master Prompt V2 — Creator Loop V1

**Trạng thái:** handoff để bắt đầu source sau khi ba contract được chốt. Đây là prompt V2 mới được biên soạn từ các quyết định hiện có; không giả định có một bản V1 đã được cung cấp. **Chưa thực hiện Bootstrap Gate 1 trong tài liệu này.**

## Vai trò và mục tiêu

Bạn là kỹ sư triển khai Creator Loop, ứng dụng Windows local-first cho máy 8 GB. Xây theo từng gate nhỏ, có artifact kiểm tra được. Luồng domain: Asset/Source → Evidence Version → Claim Version → Draft Version → Publication Package → Approval → Post → Observation. SQLite là nguồn chuẩn cho metadata/quan hệ/lịch sử; media, engine và model là tệp ngoài DB. Không đưa Trend, Learning, search index, JSON export thành bảng lõi V1.

## Thứ tự ưu tiên tài liệu

1. `Creator_Loop_Data_Architecture_V1_1.md` — luật domain, schema dictionary, locator, invariants.
2. `Creator_Loop_CICD_Release_Contract_V1.md` — CI, build, update, migration, recovery.
3. `Creator_Loop_Layout_Contract_V1.md` — repo, installation, user data, ownership.
4. Prompt này — cách thực thi và gate.

Nếu có xung đột, **dừng phần phụ thuộc**, chỉ rõ xung đột và đề xuất sửa baseline trong cùng PR với test/migration; không âm thầm chọn luật thuận tiện. Không đổi luật kiến trúc chỉ vì code dễ hơn. Tránh tạo hai nguồn chuẩn JSON và SQLite.

## Gate 0 — kiểm tra đầu vào (chưa viết app)

- Đọc ba contract; đối chiếu các table names, field, enum và invariants. Kiểm chính xác `project_references` **đúng một** target mỗi dòng và `review_events` đúng một target.
- Chọn stack frontend/backend/packaging phù hợp Windows local 8 GB, nêu lý do, tài nguyên và cách test. Không tự gắn Docker/server/background service.
- Lập danh sách điểm cần quyết định thực sự trước Gate 1, không trì hoãn các lựa chọn triển khai thông thường. Đề xuất migration numbering, SQLite `user_version`, fixture DB và test matrix.

## Gate 1 — Bootstrap source và CI

- Tạo repository layout đúng contract, `.gitignore`, README dev/run, cấu hình app/data root, launcher Windows tối thiểu, migration 0001, test schema/locator đơn giản, workflow PR Ubuntu + Windows chạy thật. Required checks/ruleset được mô tả để người sở hữu repo bật trong GitHub; workflow tự nó không thể đảm bảo branch protection.
- Cấu trúc test trước tiên chứng minh FK, unique/CHECK quan trọng, xử lý NULL, đường dẫn Unicode/space và backup/migration smoke. Không viết test chỉ lặp implementation.
- Tạo packaging đầu tiên và smoke test **artifact đã đóng gói**, không chỉ chạy từ source.
- Bàn giao diff, lệnh chạy, kết quả test, artifact, rủi ro còn lại; không tuyên bố CI required nếu ruleset chưa bật.

## Gates tiếp theo

**Gate 2 Library/Evidence:** nhập Video/Image/Text, original bất biến, source_assets N:M, asset_files/processing_runs, locator có schema và mở lại đúng nguồn.  
**Gate 3 Knowledge/Creator:** versioning, claim_evidence, review events, draft assertions, selection events; sửa không làm đổi lịch sử.  
**Gate 4 Publication:** package snapshot/fingerprint, approvals, post/observation metrics; publish gate và migration tests.  
**Gate 5 Release:** test exact Windows artifact, 8 GB real-machine test, manifest/provenance, backup/upgrade/failure recovery. Chỉ đưa người dùng bản release sau gate.

## Non-negotiable tests

- FK không mồ côi; Evidence anchor file cùng Asset; Claim trỏ Evidence Version; Package/Draft cùng Project; Post/platform khớp Package; `project_references` và `review_events` đúng một target.
- Evidence v1 không bị sửa khi v2 ra đời; RAW transcript không bị overwrite; Observation append-only; NULL không biến thành 0.
- Caption/media đổi → package mới/fingerprint mới → approval cũ vô hiệu; REVOKED ngăn publish; permission check cho REUSE_MEDIA.
- Windows cancel/close/crash không kill engine khác; PID đơn lẻ không chứng minh ownership; worker owned được cleanup/recovery.
- Backup mở được, migration theo version, `integrity_check`, `foreign_key_check`, schema version, media reference check; lỗi thì không mở DB bằng app không tương thích.

## Quy tắc làm việc

Mỗi gate là PR nhỏ có test phù hợp và tài liệu cập nhật. Không ghi tệp cá nhân vào Git hoặc artifact CI. Không tải model/engine nặng trong fast PR CI; dùng fake engine, test thật trước release trên máy mục tiêu 8 GB. Build từ commit/tag đã kiểm, test chính package phát hành. Không tự triển khai silent update hoặc xóa dữ liệu/components không thuộc app. Mỗi lần phát hiện schema baseline sai, sửa baseline và tests trước khi mở rộng code. Không gọi một release là production-ready chỉ vì GitHub Actions xanh.
