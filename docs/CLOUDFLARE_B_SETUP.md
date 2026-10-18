# Cấu hình Cloudflare B riêng tư

Template public chỉ có endpoint/model/Worker version/key_file, không có credential.
Người vận hành Worker cấp key riêng qua kênh riêng. Giữ key tại
`<data root>/secrets/creator-cloud-key` (UTF-8, không xuống dòng ngoài ý muốn).
Tạo `creator-cloud-key.binding.json` cùng thư mục với đúng hai trường:
`endpoint` giống template và `key_sha256` là SHA-256 của byte key đã lưu.
Không đưa key/binding/data root vào Git, artifact hay log. Giới hạn ACL cho user.

Chọn template trong B → Chọn cấu hình backend AI. App sao chép cấu hình công khai
vào manifests và giữ bản cấu hình cũ. Không tự copy key từ máy tác giả. Sai/mất key,
binding hoặc version dừng trước/ở request; không fallback tự động hoặc gọi model lại.

Worker Free chỉ dùng khi quota còn đủ toàn chuỗi cộng dự phòng và chi phí $0.
Model Workers AI hiện hỗ trợ TEXT; các format khác cần backend riêng đã kiểm.
Health chỉ chứng minh metadata/liveness; bằng chứng release phải nối exact app
source/config/deployment với live run và Draft thực đã lưu/mở lại.
