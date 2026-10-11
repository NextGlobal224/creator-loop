# Tư liệu Facebook local

Mở shortcut Creator Loop beta, chọn **Tư liệu Facebook**. Nhập từ khóa, giữ giới hạn20 hoặc chọn1–100 rồi **Quét**. Ứng dụng chỉ dùng trình duyệt local riêng; không API Meta, dịch vụ scraping hay cloud.

Nếu cần, chọn **Đăng nhập riêng**, đăng nhập trực tiếp trong browser rồi bấm **Đã đăng nhập** trong app. Không gửi mật khẩu cho ứng dụng/agent. Phiên được giữ ở `facebook-browser-session/` trong data root, không ở Git/log/installation. Hết phiên/CAPTCHA dừng và báo; không retry tự động.

Bảng lưu URL bài/video/reel thật, nguồn và trích đoạn nếu liên kết với bài được nhận diện chắc chắn, từ khóa và thời điểm UTC. Chọn hàng rồi **Mở nguồn**. Quét lại cùng post ID bỏ qua trùng, không sửa Source/Original cũ. Chỉ có URL thì không tạo Asset/file; trích đoạn nhìn thấy được lưu dưới TEXT/ORIGINAL, không phải file video/ảnh hoặc Evidence đã duyệt. Quyền sử dụng vẫn UNKNOWN.

**Dừng** đóng riêng browser worker/driver/Chromium do app sở hữu; bản ghi đã commit được giữ. Đóng cửa sổ app sẽ chờ cleanup, không đụng browser thông thường của bạn.

Browser component Playwright1.63.0 / Chrome for Testing153.0.8010.12 được tải trực tiếp từ upstream khi cài local, giữ ngoài ZIP public và kiểm toàn bộ308file theo pin. Phiên đăng nhập tách component. Source/license/hash trong `FACEBOOK_DEPENDENCIES.md` và `licenses/facebook`.
