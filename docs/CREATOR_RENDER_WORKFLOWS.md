# Creator và render local — bổ sung baseline schema 9

Các bảng bổ sung nằm trong SQLite, không thay Source/Asset/Evidence/Claim/Draft/Package. Migration `0009_creator_render_workflows` không sửa các migration đã phát hành. Khi nâng cấp, bootstrap giữ backup nhất quán và kiểm schema/checksum/integrity/FK theo cơ chế hiện có.

`creator_workflows` giữ Project, mode, mục đích/khán giả/phong cách, định dạng, backend/version, fingerprint và bản phân tích/hướng đề xuất. `creator_workflow_materials` ghim Source và/hoặc file ID/digest, Evidence Version nếu đã có; `creator_workflow_claims` ghim exact Claim Versions. `creator_workflow_drafts` chỉ nối tới Draft sealed cùng Project. Máy không tự tạo review ACCEPT, Selection hoặc Approval. Kết quả cũ không ghi đè; sửa tạo workflow/Draft Version phù hợp.

`draft_render_requests` ghim exact sealed Draft Version và processing run `DRAFT_VIDEO_RENDER`, kế hoạch/cấu hình/fingerprint. Input chính của run là original TEXT thực được nhập từ script của Draft; không gán input của Asset khác vào processing_runs. `draft_render_inputs` ghi file ID/digest và vai trò SCRIPT/IMAGE/CLIP/AUDIO; sau seal tập input bất biến. Đây là lineage nhiều Asset hợp lệ, không dùng parent_file_id qua Asset.

`draft_render_outputs` chỉ nhận MP4 thực đã kiểm và run SUCCEEDED: storage key trong derived, digest, bytes, thời lượng/kích thước, tool/version. Tệp đang ghi, lỗi hoặc Hủy không có output thành công. MP4 tổng hợp chưa trở thành Asset nhập/Publication Package; muốn đưa vào Library/publication cần nhập file thực qua intake và giữ lineage tới render. Không giả một original VIDEO từ URL hoặc script TEXT. Render mới giữ output cũ.

Workflow và output metadata append-only. Request chỉ được cập nhật một lần từ building sang sealed. JSON kế hoạch trong SQLite là payload snapshot, không là nguồn chuẩn thứ hai ở file JSON. Các file runtime chỉ là IPC/ownership; không cấp review hay quyền media.

Render request/cache ghim cả implementation/source fingerprint, font/version/digest, component và cấu hình codec/budget. Worker đối chiếu snapshot trước chạy và cấu hình thực tế trước ghi output; thay đổi làm từ chối/cache miss. GUI chỉ đọc media options; worker xác minh các file được sử dụng với deadline và Hủy.
