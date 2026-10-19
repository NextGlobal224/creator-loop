# Creator Loop B Web — hướng dẫn beta

Trạng thái: đang kiểm chứng, chưa có bản Web được phát hành. Bản Windows
`v0.1.0-beta.2` vẫn dùng theo hướng dẫn riêng, không cần thay cài đặt hay data root.

## Đăng nhập và tạo Draft

Mở URL HTTPS được công bố trong release Web. Nhập mật khẩu riêng đã bàn giao qua
kênh local; không đặt mật khẩu vào URL, Git, ảnh chụp hay báo cáo. Cookie phiên
HttpOnly/Secure có hạn 12 giờ. Đăng xuất thu hồi phiên; mọi API/media đều yêu cầu
phiên hợp lệ. Không chia sẻ cookie, operator key hoặc credential Cloudflare.

1. Tạo/chọn Project ở Bước 1. Project chưa có bản nháp sẽ có trình sửa trống;
   nút lưu chỉ bật sau khi mở được phân tích/bản nháp của đúng Project.
2. Nhập text hoặc tải tư liệu, ghi nguồn và tình trạng quyền đã biết. Chọn các tư
   liệu text thực sự cần phân tích. Tệp text phải UTF-8/NFC; URL không thay cho
   nội dung đã đọc. Ảnh/video/audio chưa xử lý không tự thành bằng chứng text.
   Project mới có danh sách trống. Tư liệu cũ vẫn ở **Thư viện chung**; chủ động
   chọn và bấm **Thêm vào Project đang chọn** để tạo liên kết N:M, không sao chép
   hoặc xóa bản gốc. Chỉ tư liệu/ảnh đã liên kết mới dùng cho lượt AI của Project.
   Khi chọn tệp, xem tên, dung lượng và loại; tên tư liệu được điền nếu còn trống.
   Text dùng UTF-8/NFC; ảnh chỉ PNG/JPEG/WebP. Bấm lưu và chờ xác nhận; có thể
   mở/tải byte gốc từ danh sách. Tệp backup dùng mục Khôi phục, không nhập tư liệu.
3. Bước 3 mặc định **Thủ công**; nhập mục đích, người đọc, phong cách và hướng
   nội dung bạn chọn. Beta ưu tiên TEXT hoặc IMAGE_CAPTION. Với caption, chọn
   tệp ảnh đã tải ở **Ảnh của bản nháp**, xem ảnh rồi ghi nguồn/quyền sử dụng;
   lựa chọn này độc lập với ảnh của bản đang sửa ở Bước 5. Tự động còn trong
   backlog; VIDEO chờ C nghiệm thu và tích hợp được kiểm chứng.
4. Bấm **Sang Bước 4 · kiểm điều kiện** sau khi nhập Bước 3. Đây chưa phải lời gọi
   AI. Đọc trạng thái quyền; nếu chưa có quyền còn hiệu lực, operator
   mở mục **Operator · kiểm quota và cho phép một lượt** ngay trên
   Web. Kiểm đúng tài khoản Cloudflare của ứng dụng và Workers plan (Free/Paid);
   Paid vẫn chỉ bao gồm 10.000 Neurons/ngày, phần vượt bị tính phí. Chọn đúng plan,
   với Paid xác nhận chỉ dùng phần bao gồm và phí ngoài gói $0. Đọc Neurons còn
   lại, trừ cả usage đã gửi mà dashboard chưa cộng. Nhập số còn lại (ít nhất
   5.000), mã operator riêng của ứng dụng và xác nhận vừa kiểm trong 2 phút.
   Bấm **Cho phép một lượt**: quyền dành tối đa 4 call, hết hạn sau 4 phút;
   chưa tạo job AI. Mã operator được xóa khỏi form sau thao tác, không lưu vào
   browser storage. Không nhập API token Cloudflare vào form này, không đưa mã
   vào URL, ảnh chụp hoặc báo cáo. Quyền mới không xóa request/usage lịch sử.
   Sau đó chủ động bấm **Tạo bản nháp AI**. Thiếu tư liệu, mục đích, hướng hoặc
   lựa chọn ảnh thì form chỉ rõ bước cần sửa trước khi gửi; thiếu quyền operator
   thì mở hướng dẫn Bước 4, chưa tạo yêu cầu AI. Trạng thái quyền không phải
   phép đo quota trực tiếp. Nếu quota không đủ, chưa bấm cho phép hay tạo AI.
   Tác vụ
   lưu trên cloud, không phụ thuộc cửa sổ trình duyệt. Mở lại để đọc tiến độ/kết
   quả. Dừng ngăn bước tiếp theo và commit; lời gọi model đã gửi có thể vẫn dùng
   quota. Không tự retry khi kết quả request chưa rõ.
5. Đọc toàn bộ phân tích, nguồn và Draft. Tự động lưu sealed Draft; thủ công cho
   phép chọn/sửa rồi lưu. Mỗi lần sửa tạo phiên bản mới; chọn lịch sử để đọc lại.
   Sau mất kết nối, dùng nút kiểm tra yêu cầu chờ; không gửi lượt khác để đoán kết
   quả. Chỉ nút **Bỏ yêu cầu mà server xác nhận chưa có** mới xóa yêu cầu chờ
   sau xác nhận 404; kiểm tra trạng thái không tự gửi lại. Yêu cầu trước restore
   không được tự replay thành công. Đổi Project/bản/phiên bản khi có sửa chưa
   lưu cần xác nhận; hủy chuyển giữ nguyên nội dung đang sửa.

Nội dung vẫn CHƯA DUYỆT. Kiểm factual/nguồn, phạm vi suy luận và quyền media, rồi
duyệt thủ công trước đăng bằng quy trình publication riêng. Web không tự đăng
Facebook, tạo Evidence ACCEPT hay GitHub APPROVE. VIDEO là kịch bản có cảnh,
lời đọc/phụ đề/timing, không phải chứng nhận MP4/giọng đọc của C.

## Dữ liệu, backup và khôi phục

Domain SQLite schema10 và media nằm bền vững trên cloud. Giới hạn beta: mỗi tệp
tối đa8MiB, tổng media8MiB, domainDB16MiB và compressed checkpoint1.8MB. Hết giới
hạn báo lỗi; không tự nâng gói. Tải backup DB+media định kỳ vào nơi riêng an toàn.
Backup chứa dữ liệu riêng, không đưa vào Git/GitHub hay gửi công khai.

Khôi phục: chọn backup đã tải, xác nhận rằng thay đổi sau mốc backup sẽ mất rồi
bấm khôi phục. Server kiểm schema/checksum/integrity/FK/tệp/digest, giữ checkpoint
trước restore và media bất biến. Receipt/job của trạng thái cũ trở thành lịch sử;
run cloud gián đoạn trong backup không tự chạy lại. Không dùng bản desktop cũ
mở schema không tương thích hoặc coi backup JSON là một DB domain thứ hai.
Nếu cloud đã đổi sau lần mở trang, restore bị từ chối với 409: tải lại và xem dữ
liệu hiện tại trước khi chọn/xác nhận backup lại. Không tự làm mới revision rồi
gửi restore. Backup hỏng/sai schema/thiếu media bị hủy trong transaction; dữ liệu
hiện hành giữ nguyên. Sau restore thành công, chỉ chọn Project còn trong backup.

Không chuyển dữ liệu chính Windows vào Web khi canonical backup còn Access denied.
Phát triển/QA dùng dữ liệu cô lập. Web không đọc Chrome profile, session Facebook
hay data root Windows. Gỡ/sửa Worker code không đồng nghĩa được xóa dữ liệu DO.

## Vận hành trong $0

Worker dùng Workers plan hiện có, Python+SQLite Durable Object và model
`@cf/openai/gpt-oss-120b` qua pinned Workers AI SDK. Không Tunnel, R2/Access
subscription mới hoặc Containers. Credential nằm server; bundle công khai không
chứa credential. Operator phải kiểm account/plan/remaining Neurons thực tế, ước
lượng đủ cả chuỗi4stages và reserve trong sổ `B-finish-20261017-free` trước lượt
cloud. Grant/usage lịch sử không reset khi đổi tài khoản hay mở lại phiên.
User đã nâng Workers Paid riêng; chỉ phần tài nguyên bao gồm được phép sử dụng.
Kiểm usage Workers request/CPU và DO request/duration/SQL/storage cùng allocation
đang mua trước lượt cloud. Không coi $5 của plan là AI không giới hạn; không mua
credit hoặc phát sinh phí ngoài gói. Dự phòng cả chuỗi vẫn 5.000 Neurons, giữ
giới hạn đầu vào/đầu ra và trừ usage chưa cộng. Không đủ thì không inference.

Quota grant phía server có hạn ngắn, chỉ operator được thiết lập sau kiểm chứng;
hết hạn ứng dụng từ chối gọi model. Không bỏ guard để thử. Mọi call/failure/timeout
được đếm bảo thủ, không fallback/retry tự động. Usage UI cũ không là quota hiện tại.

Rollback code chỉ dùng Worker version đã biết tương thích schema hiện hành,
giữ nguyên DO namespace/migration và media. Nếu cần quay DB về mốc cũ phải dùng
confirmed restore kèm backup phù hợp; không hứa downgrade schema bằng code cũ.
Release Web phải công bố exact source/CI/deployment/bundle/checksum/evidence;
chưa có các bằng chứng đó thì không gọi bản Web hoàn tất.
# Phạm vi beta thủ công ưu tiên — 19/10/2026

Profile `B_WEB_MANUAL_BETA` dành cho **Thủ công → TEXT** và **Thủ công →
IMAGE_CAPTION**. Đăng nhập URL HTTPS, chọn Project, nhập/tải tư liệu, đọc phân tích
và nguồn, tự chọn hướng rồi tạo và sửa Draft. Ảnh phải xem/tải được và có nguồn,
quyền dùng; sau tải lại hoặc đăng nhập lại phải giữ nguồn, ảnh, version và review.
Chưa có đủ hai đầu ra đạt chất lượng và release gắn deployment thì chưa nghiệm thu.

Tự động vẫn giữ trong backlog/tính năng hiện có. VIDEO kế thừa sau khi C đạt
contract và tích hợp qua B được kiểm chứng; không chạy VIDEO để nghiệm thu beta
này. Beta thủ công không là toàn bộ B hoặc A/B/C hoàn tất. Luôn duyệt trước đăng.
