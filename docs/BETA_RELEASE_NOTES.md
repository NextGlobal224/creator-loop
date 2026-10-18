# Creator Loop — release notes beta dự thảo

**Trạng thái:** đề xuất local để duyệt, chưa public release; không phải V1 ổn định đã ACCEPT. Source/tag/CI run/ZIP checksum cuối sẽ được điền từ pipeline sau gate. Bản dùng cá nhân hiện tại giữ `app_version=0.1.0`, nguồn PR synthetic merge; không được gọi là tag/main release.

## Phạm vi sử dụng

Windows local-first: Library Video/Image/Text → Evidence version/review → Claim/citation → Draft/Selection → sealed Package/approval → Post thủ công/Observation. Original, RAW, version và review history được giữ; MODEL không tự ACCEPT. Có backup DB nhất quán, staging/update/restore có guard, owned cancel/crash và hướng dẫn offline. Không có giới hạn tính năng hoặc thời hạn sử dụng thêm cho beta.

Engine/model ngoài package giữ lựa chọn whisper.cpp1.8.7 CPU + Whisper base multilingual; phải kiểm nguồn/license/digest qua Components. Chỉ dùng cục bộ, không phân phối binaries/weights trong release app; không tự tải model lúc mở. Budget worker hiện tại giữ768MiB. Không tự xóa component người dùng cài ngoài ownership.

## Giới hạn công bố bắt buộc

- Chưa chứng nhận toàn bộ Windows8GB/thiếu RAM/đĩa thực; số đo hosted CI hoặc vài lượt native không chứng minh mọi thiết bị/input.
- Chất lượng ASR tiếng Việt chưa nghiệm thu. Corpus đọc có phạm vi hẹp và sai từ còn giữ; RAW/MODEL cần nghe/đọc, sửa HUMAN và review riêng. Không hứa độ chính xác đại diện.
- PUBLISHED–Observation ngoài ứng dụng chưa nghiệm thu vận hành; release beta không cấp quyền đăng nội dung hoặc ghi metadata/metric giả. Approval Package và review Draft/Evidence là các thao tác riêng.
- Chưa có xác nhận V1 ổn định hoàn tất; các FAIL/SKIP/giới hạn chứng cứ vẫn được giữ trong ledger.

## Mở, giữ dữ liệu, cập nhật và khôi phục

Giải nén nguyên onedir (EXE + `_internal` + offline guide); giữ installation tách data root. Launcher đặt `CREATOR_LOOP_DATA_ROOT` trong phiên và dùng cùng data root khi mở lại; kho mới hoặc snapshot/backup nhất quán, không ghi vào QA gốc. Không đòi Administrator theo mặc định. Mở `USER_GUIDE.html` trong package để dùng offline.

Đóng app/owned workers trước update; dùng Maintenance preflight và validated SQLite Backup API, kiểm schema/integrity/FK/media trước activation/health. Backup DB-only không chứa media. Giữ installation cũ và backup; chỉ chạy app cũ nếu readable schema range tương thích. Restore cần xác nhận mất thay đổi sau backup và đánh giá media; không copy đè DB đang mở, không xóa lock/restore guard để ép app chạy. Update do người dùng chủ động, không silent auto-update.

## Nguồn và điều kiện phát hành

Nguồn cuối trên `main`, tag khớp manifest/app version; checksum/provenance/compatibility và release notes phải sinh trung thực cho chính artifact cuối đã kiểm. Stable và beta đều giữ required checks/valid GitHub review/invariants/ownership/backup. Beta chỉ publish bằng gate beta fail-closed và GitHub pre-release; không upload bản PR đã kiểm như thể là build tag/main. Không sửa một manifest cũ để vượt checker.


## Facebook local beta

Keyword search/default20, progress/Stop/Open source, real post/video/reel URL filtering, Source/TEXT capture and cross-run dedup use a separate local Playwright browser session. Browser component is installed directly from its pinned official source, outside public app ZIP; no Meta API/scraping service/cloud. No downloaded media is inferred from a URL. Authentication expiry/CAPTCHA stops. Schema7 keeps existing data and requires a consistent pre-migration backup. Native final-artifact acceptance and valid required CI/review still gate publication.

Stable scopes **NOT ACCEPTED**: actual8GB RAM/disk pressure, representative Vietnamese quality, real external PUBLISHED→Observation. Beta publication does not accept V1 stable.
