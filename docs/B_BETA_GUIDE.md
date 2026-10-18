# Creator Loop B beta

B là phân tích tư liệu đã chọn → hướng biên tập → Draft chờ người dùng review.
Gói B riêng không chứng nhận A/Facebook discovery, ASR tiếng Việt hoặc C/MP4/voice.
Không tự đăng và không tự cấp Approval. Các mục V1 chưa nghiệm thu vẫn **NOT ACCEPTED**.

## Tải và chạy

Tải ZIP Windows x64 và SHA256SUMS của cùng pre-release. Kiểm SHA-256, giải nén
toàn bộ vào thư mục có quyền đọc, chạy `CreatorLoop.exe` hoặc `Open-CreatorLoop.ps1`.
Không chạy EXE ngay trong ZIP. Giữ `_internal`, docs và examples cùng EXE.
Đọc `USER_GUIDE.html` về chọn data root; installation và user data tách riêng.
Gói không kèm tài khoản, DB, token, model hoặc media cá nhân.

## Chọn backend, không tự chạy

Trong B, chọn cấu hình backend AI. Public template `examples/B/creator-cloud.json`
ghim endpoint, model và Worker version đã kiểm. Không có key trong template.
Workers AI chỉ nhận TEXT; IMAGE_CAPTION/VIDEO sử dụng backend Codex/local tương thích
đã cấu hình riêng. Không suy rằng Cloudflare phân tích pixel hoặc tạo MP4.
Người vận hành phải tự cung cấp credential của Worker và binding riêng trong
data root, theo `CLOUDFLARE_B_SETUP.md`. Không chia sẻ credential trong release.
Mở app/chọn backend không chạy model. Trước Chạy B, kiểm quota Free và dự phòng;
usage reset không cấp lại grant. Không chạy nếu không bảo đảm chi phí phát sinh $0.

## Tạo và mở Draft

Tạo/chọn Project, nhập tư liệu vào Library, mở B, chọn riêng tư liệu cần phân tích.
Thủ công: Phân tích → đọc nguồn và lý do → chọn/chỉnh hướng → sửa nội dung nếu cần
→ Lưu Draft. Tự động: Chạy B → đọc toàn bộ kết quả và citation.
Chọn lịch sử workflow để mở lại exact version; sửa tạo version mới, giữ bản cũ.
IMAGE_CAPTION: nhập PNG/JPEG vào Library, chọn ảnh trong B, ghi tác giả/nguồn và
căn cứ quyền sử dụng, lưu caption. Ảnh được ghim file ID/digest; đổi byte bị từ chối.
Ghi chú quyền không thay quyền thực tế hoặc duyệt xuất bản.
VIDEO của B gồm kịch bản/cảnh/asset/lời đọc/phụ đề/timing để chuyển tiếp C;
không phải chứng nhận file MP4 hay chất lượng giọng tổng hợp.

## Xem bộ đầu ra

Trong `examples/B`, mở `TEXT.md`, `IMAGE_CAPTION.md` cùng `image.png`, và
`VIDEO.md`. Đây là bản xuất nguyên văn Draft đã lưu, không phải bài viết lại cho
báo cáo. `outputs.json` ghi exact version, hash, brief/nguồn và giới hạn.
`image.svg` là artwork gốc có nhãn tình huống giả định, không là ảnh một vụ việc thật.
`VIDEO-plan.json` là view suy ra trực tiếp từ kịch bản đã lưu: sáu cảnh đồ họa chữ
inline, lời đọc/phụ đề tương ứng và timing 30 giây; không phải file MP4 đã render.

## Nâng cấp và quay lui

Thoát app và owned workers. Giữ nguyên data root và media. Dùng quy trình update
Sau một lượt B, đóng và mở lại app trước khi backup: runtime B vẫn được giữ làm
bằng chứng, chỉ cho phép backup khi owner/child cũ đã xác minh kết thúc.
trong `UPDATE_BACKUP.html`/`UPDATE_ACTIVATION.html`: SQLite Backup API, kiểm backup,
migration, integrity/FK/schema, storage references và health trước activate.
Schema 10 bổ sung lựa chọn/ảnh Draft, không sửa caption/version cũ. Không mở DB đã
nâng cấp bằng app chỉ hỗ trợ schema 9. Nếu cần quay lại, giữ installation cũ và
khôi phục backup DB tương thích theo `RESTORE_APPLY.html`, sau xác nhận mất thay đổi
sau backup; DB backup không thay backup media. Không chép riêng SQLite đang có WAL.

Trước đăng, người dùng phải đọc nguồn, xử lý factual claims/citations, kiểm quyền
media, chọn Package chính xác và duyệt thủ công. Beta không cấp phép đăng Facebook.
