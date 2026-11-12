# B Web beta: audio khoảng 10 phút

## Tiêu chí phát hành

- Người dùng chọn bản ghi thực từ điện thoại, gồm MP3 và M4A với codec thường gặp, khoảng 10 phút. Tên đuôi không đủ để xác minh codec. B báo rõ tệp lỗi, codec không hỗ trợ, hoặc vượt giới hạn an toàn; không làm mất bản gốc.
- B lưu byte gốc và hash trong đúng Project, sau đó người dùng có thể bấm **Trích lời nói** để hệ thống tự tạo transcript trên cloud. Nhập transcript bằng tay là lựa chọn bổ sung, không thay thế ASR. Windows 8 GB chỉ cần trình duyệt; không yêu cầu chạy model hoặc chuyển mã nặng trên máy.
- Lưu bản gốc, kết quả máy nguyên vẹn, cảnh báo phần không rõ, bản sửa, các version và trạng thái review riêng. Người dùng nghe, tua tới timestamp, đối chiếu, sửa/chọn, lưu và mở lại sau đăng nhập mới. Không tự duyệt nguồn hoặc duyệt đăng.
- Nếu chia đoạn, phải kiểm tra ghép theo thứ tự nguồn, thời gian quy về file gốc, ranh giới và overlap: không mất câu, lặp câu hoặc trôi timestamp; đánh dấu phần xử lý thiếu hoặc không chắc. Đối chiếu bằng bản thu thật và transcript tham chiếu, không dùng fixture làm bằng chứng chất lượng.
- Nghiệm thu với bản ghi tiếng Việt khoảng 10 phút thực tế, ít nhất một MP3 và một M4A có nguồn/quyền dùng; kiểm tiếng nói, thời gian, lưu/mở lại, Dừng và khôi phục. Nghiệm thu phải gắn artifact/source/deployment và provider thật. Giữ quyền riêng tư và công bố nơi xử lý.
- Guard chi phí, reservation, dữ liệu production, job/alarm và receipt cũ phải an toàn trước cloud QA/deploy. Phí ngoài gói tối đa 0 USD; usage thiếu hoặc trễ không là 0. Các test giả lập, upload và player không chứng minh ASR thật đạt.

## Trạng thái thực tế của source PR trước thay đổi này

Intake giới hạn 8 MiB/tệp, tổng media 10,5 MiB, body request 16 MiB và đường ASR hiện xác thực WAV PCM/float32 tối đa 120 giây. Các ngưỡng này chưa đáp ứng tiêu chí. Ví dụ tính theo bitrate danh nghĩa: 10 phút, 128 kbit/s khoảng 9.600.000 byte trước overhead, đã vượt 8 MiB; không coi ví dụ là kích thước cố định cho mọi tệp. Chưa có giải mã MP3/M4A hay chia/ghép 10 phút được nối vào ASR hiện tại. Sửa nhận diện tên tệp chỉ giúp chọn loại AUDIO, không xác nhận codec hoặc khả năng trích lời. Route `/media/` hiện chỉ phát inline audio vượt qua `probe_pcm_wav`; MP3/M4A và WAV dài hơn 120 giây bị trả dạng attachment, nên player còn cần đường xác thực nội dung phù hợp.

## Hướng kỹ thuật để đánh giá, chưa bật dịch vụ

1. **Cloudflare Workers AI Whisper đang có trong source.** Cloudflare công bố model chạy trên Cloudflare, không lưu dữ liệu tại model và giá tham chiếu $0,000513/phút audio; Workers AI có phần neurons bao gồm nhưng Paid có thể tính phí vượt phần đó. Đây không phải bằng chứng zero overage cho account. Đường hiện có vẫn cần hỗ trợ định dạng, dung lượng, 10 phút, chia/ghép nếu cần, lịch sử và kiểm chứng cloud thật. [Model](https://developers.cloudflare.com/workers-ai/models/whisper-large-v3-turbo/), [giá](https://developers.cloudflare.com/workers-ai/platform/pricing/).
2. **Deepgram Nova-3 là phương án khảo sát, chưa chọn/tích hợp.** Tài liệu của hãng liệt kê MP3, M4A và tiếng Việt (`vi`); API ghi âm sẵn nêu giới hạn file 2 GB và thời gian xử lý request tối đa 10 phút, khác với thời lượng audio. Trang giá hiển thị Nova-3 đơn ngữ pay-as-you-go $0,0077/phút cho streaming, khoảng $0,077 cho 10 phút một kênh trước các khoản khác; trang nói ghi âm sẵn có giá thấp hơn nhưng chưa xác minh được mức cụ thể cho account này. Hãng công bố credit dùng thử nhưng chưa có bằng chứng credit, account hoặc hard stop áp dụng cho dự án. Không tạo tài khoản hay phát sinh khoản phí mới. [Định dạng](https://developers.deepgram.com/docs/supported-audio-formats), [ngôn ngữ](https://developers.deepgram.com/docs/models-languages-overview), [giới hạn](https://developers.deepgram.com/docs/pre-recorded-audio), [giá](https://deepgram.com/pricing).

Deepgram mô tả việc giữ dữ liệu để cải thiện model là mặc định; `mip_opt_out=true` loại request khỏi chương trình và không giữ audio/transcript sau phản hồi, nhưng metadata/usage logs vẫn được giữ. Nơi xử lý phụ thuộc endpoint, cần kiểm tra quyền và chính sách dữ liệu trước khi chọn provider. [Chính sách dữ liệu](https://developers.deepgram.com/trust-security/your-data).

Không có phương án nào ở đây đã chứng minh giới hạn chi phí account 0 USD hoặc đạt chất lượng với file 10 phút của B. Chỉ triển khai provider khi có bằng chứng, quyền và quyết định cần thiết; tiếp tục giữ mọi dữ liệu, ledger và reservation.
