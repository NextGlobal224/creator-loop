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
