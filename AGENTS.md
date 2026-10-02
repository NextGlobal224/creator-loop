# Hướng dẫn làm việc — Creator Loop

Khi nhiệm vụ đụng đến luật domain, CI/release hoặc layout, đọc phần liên quan của các contract dưới đây. Giữ thứ tự ưu tiên đã chốt:

1. [Creator_Loop_Data_Architecture_V1_1.md](docs/Creator_Loop_Data_Architecture_V1_1.md) — luật domain, dữ liệu và bất biến.
2. [Creator_Loop_CICD_Release_Contract_V1.md](docs/Creator_Loop_CICD_Release_Contract_V1.md) — CI, build, update, migration và recovery.
3. [Creator_Loop_Layout_Contract_V1.md](docs/Creator_Loop_Layout_Contract_V1.md) — repo, installation, user data và ownership.
4. [Codex_Master_Prompt_V2.md](docs/Codex_Master_Prompt_V2.md) — cách thực thi và gate.

Nếu tài liệu xung đột, dừng phần công việc phụ thuộc, nêu rõ xung đột và xử lý theo quy tắc trong Master Prompt; không tự chọn luật thuận tiện.

- Khi tiếp tục công việc, đọc [docs/HANDOFF.md](docs/HANDOFF.md), rồi đối chiếu branch, HEAD và worktree bằng Git. Handoff là ghi chú tiến độ, không thay thế contract hay trạng thái Git.
- Đọc source theo nhiệm vụ; mở rộng phạm vi đọc khi cần kiểm tra ảnh hưởng và bất biến liên quan.
- Chạy kiểm tra phù hợp với thay đổi và các yêu cầu validation của repo/CI. Kết quả kiểm tra cũ không xác minh code đã thay đổi sau đó.
- Làm theo từng gate/PR nhỏ có test và tài liệu phù hợp. Cập nhật handoff tại mốc quan trọng hoặc trước khi chuyển phiên, ghi rõ việc còn thiếu và kết quả chưa xác nhận.
- Với việc nhỏ, đọc và kiểm tra đúng phạm vi; không mặc định chạy toàn bộ test ứng dụng chỉ vì sửa tài liệu. Lệnh và cách bàn giao nằm ở [docs/CODEX_WORKFLOW.md](docs/CODEX_WORKFLOW.md).

## Tiếp tục theo checkpoint

- Bám thứ tự module/Gate và phụ thuộc trong [bảng nghiệm thu V1](docs/PRODUCT_ACCEPTANCE.md); cập nhật các dòng thay đổi tại mỗi checkpoint, không chia lại phạm vi.
- Chỉ chạy lại kiểm tra khi thay đổi liên quan, cần bằng chứng mới hoặc contract bắt buộc; giữ đủ validation, review và required CI tại mốc bàn giao. Dùng một agent chính; giữ review độc lập khi quy trình yêu cầu.
- Test có thể treo phải chạy với timeout ở cấp tiến trình và lưu stdout/stderr vào log; vòng chờ trong test hoặc thời gian yield của công cụ không thay timeout này. Chỉ dừng tiến trình đã xác minh thuộc lượt test.
- Nếu cùng cách thử không thêm thông tin sau hai lần, đổi hướng chẩn đoán; ghi blocker khi không còn hướng hợp lệ. Khi chờ CI dùng lệnh watch với khoảng kiểm tra hợp lý, tránh gọi model liên tục chỉ để hỏi trạng thái.
