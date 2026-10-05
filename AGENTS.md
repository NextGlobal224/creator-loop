# Hướng dẫn làm việc — Creator Loop

Khi nhiệm vụ đụng đến luật domain, CI/release hoặc layout, đọc phần liên quan của các contract dưới đây. Giữ thứ tự ưu tiên đã chốt:

1. [Creator_Loop_Data_Architecture_V1_1.md](docs/Creator_Loop_Data_Architecture_V1_1.md) — luật domain, schema dictionary, locator, dữ liệu và bất biến.
2. [Creator_Loop_CICD_Release_Contract_V1.md](docs/Creator_Loop_CICD_Release_Contract_V1.md) — CI, build, update, migration và recovery.
3. [Creator_Loop_Layout_Contract_V1.md](docs/Creator_Loop_Layout_Contract_V1.md) — repo, installation, user data và ownership.
4. [Codex_Master_Prompt_V2.md](docs/Codex_Master_Prompt_V2.md) — cách thực thi và gate.

Nếu tài liệu xung đột, dừng phần công việc phụ thuộc, nêu rõ xung đột và xử lý theo quy tắc trong Master Prompt; không tự chọn luật thuận tiện.

- Khi tiếp tục, đọc [HANDOFF](docs/HANDOFF.md), đối chiếu branch, HEAD và worktree bằng Git. Handoff không thay contract hoặc trạng thái Git.
- Bám thứ tự module/Gate và phụ thuộc trong [PRODUCT_ACCEPTANCE](docs/PRODUCT_ACCEPTANCE.md), không chia lại phạm vi. Làm từng gate/PR nhỏ có test và tài liệu phù hợp; cập nhật các dòng nghiệm thu thay đổi và HANDOFF tại checkpoint/mốc quan trọng hoặc trước chuyển phiên, ghi việc thiếu và kết quả chưa xác nhận.
- Đọc source theo nhiệm vụ; mở rộng khi cần kiểm tra ảnh hưởng và bất biến. Thực hiện quy trình đọc context, chọn công cụ, kiểm tra và bàn giao trong [CODEX_WORKFLOW](docs/CODEX_WORKFLOW.md).
- Chỉ chạy lại kiểm tra khi thay đổi liên quan, cần bằng chứng mới hoặc contract bắt buộc; giữ đủ validation, review và required CI tại checkpoint. Kết quả cũ không xác minh code đã đổi. Sửa tài liệu thuần túy không mặc định chạy full ứng dụng.
- Dùng một agent chính; giữ review độc lập khi quy trình yêu cầu. Không ghi tệp cá nhân vào Git hoặc artifact CI.
- Test có thể treo phải có timeout cấp tiến trình và stdout/stderr log; vòng chờ trong test/yield không thay timeout. Chỉ dừng tiến trình đã xác minh thuộc lượt test.
- Hai lần cùng cách thử không thêm thông tin thì đổi hướng chẩn đoán; ghi blocker khi không còn hướng hợp lệ. Chờ CI theo watch/wait trong WORKFLOW, tránh gọi model liên tục chỉ để hỏi trạng thái.
