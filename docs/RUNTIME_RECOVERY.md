# Dọn workspace decoder sau crash

Startup Library bình thường và compatible launcher giữ app-data lock, kiểm DB/schema/restore guard, reconcile thumbnail rồi kiểm các thư mục trực tiếp `runtime/decode-*` trước khi tạo worker mới. Bốn chế độ smoke bỏ qua recovery; read-only health không dọn runtime.

Decoder mới ghi/fsync `ownership.json` với canonical data root, workspace, component version và parent PID/creation FILETIME/executable. Child được tạo suspended, gán private Job và ghi/fsync `child-ownership.json` trước ResumeThread. Callback thất bại đóng Job khi child chưa chạy. Logs ownership riêng vẫn được giữ sau cleanup.

Chỉ workspace có tên được hỗ trợ và binding hợp lệ được xét dọn. App mở process handle để chứng minh parent và child đã kết thúc; process còn sống phải đối chiếu creation FILETIME/executable, PID tái sử dụng không bị terminate. Process terminal có thể không còn đọc được image name, nhưng trạng thái terminal của retained handle chứng minh nó không còn chạy. Access denied hoặc identity không đủ bằng chứng giữ workspace. Không kill process từ marker hay log.

App giữ native directory/file handles không cho thay thế, kiểm target canonical, loại reparse point và file hardlink. Chỉ nhận inventory `ownership.json`, `child-ownership.json`, `request.json`, `response.json`, `frame.png`; thư mục có file lạ giữ nguyên. Xóa theo handle các payload đã xác minh, rồi markers và thư mục rỗng; không recursive delete. Crash trước child binding chỉ cho dọn parent marker/request. Original, derived media, DB, logs và engine/model người dùng không thuộc cleanup này.

Library báo số workspace đã dọn và số workspace giữ lại vì thiếu bằng chứng. Legacy workspace chưa có marker, workspace live hoặc malformed được giữ nguyên; thông báo này không có nghĩa tác vụ đã thành công. Lỗi giữa cleanup có thể để lại workspace đã dọn một phần; không suy hoàn tất từ file bị thiếu và không dọn file lạ để ép thư mục rỗng.

Source tests kiểm actual decoder-owner crash rồi cleanup, terminal/live process, PID reuse, access denied, malformed/foreign binding, unowned inventory, hardlink, replacement bị chặn, idempotence và original/logs giữ nguyên. `scripts/check_processing_recovery.py` chạy candidate EXE staged: bốn smoke không đổi runtime/history; UI bình thường dọn workspace bound của một owner/child thực đã exit và giữ live/unbound workspace, original và logs. Fixture không thay nghiệm thu playback/cancel UI, engine/model thật, máy8GB hay toàn luồng/release cuối theo [PRODUCT_ACCEPTANCE](PRODUCT_ACCEPTANCE.md).
