# Tư liệu Facebook local

Mở shortcut Creator Loop beta, chọn **Tư liệu Facebook**. Ứng dụng dùng browser local; không API Meta, dịch vụ scraping hay cloud.

Mặc định **Chrome đang mở** dùng phiên Facebook bạn đã đăng nhập. Giữ tab Facebook mở. Trong chính Chrome144 trở lên, mở `chrome://inspect/#remote-debugging`, bật Remote Debugging, rồi quay lại app bấm **Kết nối Chrome đang mở**. Chọn **Allow** trong Chrome khi có yêu cầu kết nối. Nếu có nhiều tab Facebook, chọn đúng tab trong app rồi kết nối lại. App chỉ kiểm tra phiên trên tab được chọn; không đọc/xuất cookie, token hoặc mật khẩu. WebSocket DevTools chỉ dùng loopback cục bộ và không được ghi vào log/config. [Hướng dẫn Chrome](https://developer.chrome.com/blog/chrome-devtools-mcp-debug-your-browser-session), [Playwright CDP](https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp).

Sau khi app báo đã kết nối đúng tab Facebook, nhập từ khóa, giữ giới hạn20 hoặc chọn1–100 rồi **Quét**. App dùng chính tab đó để mở tìm kiếm Facebook; các tab khác được giữ nguyên. Nếu tab đóng/chuyển ra ngoài Facebook, hết phiên hoặc CAPTCHA, lượt dừng và báo, không tự đăng nhập/retry.

Nếu muốn dùng browser riêng của app, đổi sang **Trình duyệt riêng**, chọn **Đăng nhập riêng**, đăng nhập trực tiếp rồi bấm **Đã đăng nhập** trong app. Không gửi mật khẩu cho ứng dụng/agent. Phiên riêng ở `facebook-browser-session/` trong data root; phiên Chrome đang mở vẫn ở profile Chrome của bạn. Không copy/import phiên giữa hai profile. Hết phiên/CAPTCHA dừng và báo; không retry tự động.

Bảng lưu URL bài/video/reel thật, nguồn và trích đoạn nếu liên kết với bài được nhận diện chắc chắn, từ khóa và thời điểm UTC. Chọn hàng rồi **Mở nguồn**. Quét lại cùng post ID bỏ qua trùng, không sửa Source/Original cũ. Chỉ có URL thì không tạo Asset/file; trích đoạn nhìn thấy được lưu dưới TEXT/ORIGINAL, không phải file video/ảnh hoặc Evidence đã duyệt. Quyền sử dụng vẫn UNKNOWN.

**Dừng/Hủy/đóng app** ngắt kết nối và dừng riêng worker/driver của app. Với **Chrome đang mở**, Chrome, tab và phiên đăng nhập của bạn vẫn mở; app không gọi đóng browser/context/tab hoặc tạo context/profile mới. Với **Trình duyệt riêng**, app đóng Chromium do nó sở hữu. Bản ghi đã commit được giữ. **Mở nguồn** dùng tab Facebook đã kết nối và giữ Chrome mở sau khi mở bài.

SDK Playwright1.63.0 nằm trong package. Chrome của người dùng là browser bên ngoài, không thuộc quyền dọn/terminate của app. Chế độ **Trình duyệt riêng** dùng component Chrome for Testing153.0.8010.12 tải trực tiếp từ upstream khi cài local, giữ ngoài ZIP public và kiểm308file theo pin. Phiên đăng nhập tách component. Source/license/hash trong `FACEBOOK_DEPENDENCIES.md` và `licenses/facebook`.
