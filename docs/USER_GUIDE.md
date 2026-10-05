# Sử dụng Creator Loop V1

Hướng dẫn này mô tả giao diện hiện có; không chứng nhận release. Xem
[checkpoint và bản chạy](HANDOFF.md) để biết đúng commit, checksum, log và phần
chưa kiểm chứng. Luồng thao tác đầy đủ trên artifact cuối, engine/model thật,
máy Windows 8 GB và release gate vẫn cần nghiệm thu theo
[bảng V1](PRODUCT_ACCEPTANCE.md).

## Mở app và giữ dữ liệu

1. Dùng Windows x64. Giải nén toàn bộ ZIP vào thư mục riêng có quyền đọc/chạy;
   giữ `CreatorLoop.exe` cùng thư mục `_internal`, không chỉ chép file EXE.
   Đối chiếu SHA-256 ZIP với checksum của đúng bản được cung cấp:

   ```powershell
   Get-FileHash -LiteralPath '.\CreatorLoop-win-x64.zip' -Algorithm SHA256
   ```

2. Mở `CreatorLoop.exe` trong thư mục đã giải nén. Bản source dành cho phát triển
   có lệnh riêng trong [README](../README.md); bản onedir đóng gói Qt/Python.
   Engine/model là thành phần riêng, chưa có lựa chọn thực tế được nghiệm thu.
   Với bản có `USER_GUIDE.html` cạnh EXE, mở file đó bằng trình duyệt để đọc
   hướng dẫn offline; `docs/` chứa các hướng dẫn sản phẩm liên quan. Thông tin
   source trong `build-info.json` chỉ nhận diện bản build, không thay checksum,
   manifest hay bằng chứng nghiệm thu. Link tài liệu phát triển ngoài bộ offline
   mở repo khi bạn chọn; app không tải engine/model qua các link này.
3. Dữ liệu mặc định ở `%LOCALAPPDATA%\CreatorLoop`, tách khỏi bản cài. Để dùng
   một data root đã chọn, mở PowerShell tại thư mục bản cài:

   ```powershell
   $env:CREATOR_LOOP_DATA_ROOT = 'D:\Dữ liệu Creator Loop'
   & .\CreatorLoop.exe
   ```

   Giữ cùng data root khi mở lại. Chọn thư mục trống sẽ tạo kho dữ liệu mới;
   đổi biến môi trường không chuyển kho cũ. Giữ đủ dung lượng cho media,
   backup và bản cài được lưu lại.

Chỉ mở một phiên app/updater cho một data root. Nếu báo đang bận, đợi phiên đó
đóng; không xóa lock hoặc kill PID để bỏ qua. Nếu có restore guard hoặc DB không
tương thích, dùng cửa sổ [maintenance](MAINTENANCE_UI.md) và giữ dữ liệu hiện có.

## Từ Library đến Evidence

**Nhập Text**, **Nhập Image**, **Nhập Video** nhận lần lượt TXT/MD, PNG/JPEG,
MP4 có track giải mã được. Đợi tác vụ hoàn tất rồi chọn hàng original. App giữ
bản sao bytes gốc trong kho đã chọn; nhập tệp không suy ra tác giả, URL hay quyền.
Với ảnh, **Tạo thumbnail Image** tạo derivative và processing run riêng; hàng
task thành công có thể dùng **Evidence từ thumbnail**.

Chọn **Nguồn của Asset** để tạo Source hoặc gắn Source đã có. Điền xuất xứ và
metadata biết được; để trống URL/ID/tác giả nếu chưa biết. Giữ quyền/quan hệ
`UNKNOWN` khi thiếu thông tin. Chỉ chọn `OWNED`/`LICENSED` khi có căn cứ; việc gắn
Source hoặc chọn `REUSE_MEDIA` trong Project không tự cấp quyền xuất bản.

| Tư liệu | Thao tác tạo | Phần cần kiểm trước khi lưu |
|---|---|---|
| Text | **Tạo Evidence Text** | Khoảng ký tự trên snapshot NFC; xem excerpt, end không bao gồm ký tự cuối khoảng. |
| Image | **Tạo Evidence Image** | X/Y/width/height chuẩn hóa; crop preview và quan sát do người dùng nhập. |
| Video | **Tạo Evidence Video** | Start/end mili giây; xem đoạn video và ghi quan sát. |
| Audio trong MP4 | **Tạo Evidence Audio** | Nghe đúng khoảng, chọn speech/âm thanh khác và ghi quan sát. |
| Toàn nguồn | **Tạo Evidence toàn nguồn** | Xem toàn text/ảnh hoặc phát toàn video, xác nhận rõ chọn toàn nguồn. |

Chọn Evidence đã lưu rồi dùng **Mở Evidence** đúng loại để kiểm lại anchor và
digest. **Review Evidence** ghi quyết định của người duyệt: ACCEPT, REJECT,
REQUEST_CHANGES hoặc REOPEN, cùng actor/lý do cần thiết. Tạo thành công không
đồng nghĩa ACCEPT. Nếu cần sửa, dùng **Sửa Evidence** đúng loại trên Version mới
nhất: nhập actor/lý do và kiểm preview. Version cũ/citation cũ vẫn còn; xem cảnh
báo Claim cần review, không coi ACCEPT của Version mới là ACCEPT cho Version cũ.

## Claim, Project, Draft và chọn phương án

1. Mở **Claims** → **Claim mới**. Chọn FACTUAL, INTERPRETIVE hoặc
   EDITORIAL_HYPOTHESIS, nhập nhận định và actor. Chọn từng Evidence Version,
   SUPPORTS/CONTRADICTS/CONTEXT rồi **Thêm citation** → **Tạo Claim Version**.
   Dùng **Đóng và mở Evidence trong Library** để kiểm nguồn. Sau đó chọn
   **Review phiên bản đã lưu** với quyết định của người duyệt. Sửa Version mới
   nhất tạo Version khác với lý do; Version cũ chỉ đọc, citation STALE cần xem lại.
2. Mở **Projects** → **Tạo Project**. Thêm tham chiếu Asset, Source hoặc exact
   Claim Version với RESEARCH, QUOTE hay REUSE_MEDIA. Project đã lưu trữ còn đọc
   được nhưng không dùng để tạo nội dung mới.
3. Mở **Creator**, chọn Project và viết phương án. Trong **Claim / assertion**,
   thêm exact Claim Version với ASSERTED/INSPIRATION/QUOTE/BACKGROUND. Đánh dấu
   khoảng `[start, end)` theo Unicode codepoint của body đang viết, xem preview,
   chọn Claim và state phù hợp. Dùng NEEDS_SOURCE hoặc EDITORIAL khi chưa có
   Claim; không tự đánh dấu một câu thiếu nguồn là SUPPORTED.
4. Lưu phương án. Nếu sửa chữ làm lệch khoảng đã đánh dấu, bỏ khoảng đó và đánh
   dấu lại trước khi lưu. Bỏ Claim không âm thầm bỏ assertion: nó còn NEEDS_SOURCE.
   Muốn kết hợp A+B, chọn ít nhất hai same-Project Versions trong **Parents A+B**,
   viết body/citation rồi **Lưu phương án kết hợp A+B**. Parent cũ được giữ.
5. **Review snapshot đã lưu** ghi quyết định riêng cho Draft; review không xóa
   NEEDS_SOURCE/stale hoặc cấp Approval. Mở **Selection**, tạo **Quyết định mới**,
   tích các ứng viên cùng Project và **Bản được chọn** thuộc tập, nhập actor/lý do
   rồi **Lưu quyết định**. Selection lưu lịch sử lựa chọn, không duyệt xuất bản.

## Package, duyệt và ghi nhận bài đăng

Mở **Package / Publication** → **Package mới**. Chọn exact Draft Version,
nền tảng/định dạng; caption lấy từ Draft đã chọn. Thêm tệp đã có quyền, sắp xếp
thứ tự; điền CTA/ALT_TEXT nếu cần rồi **Đóng Package mới**. Snapshot đã đóng không
sửa tại chỗ. Muốn đổi caption, dùng **Đóng và mở Draft để xem/sửa**, lưu Version
mới rồi tạo Package mới; Approval cũ không áp dụng cho fingerprint mới.

Trong **Snapshot / Approval**, kiểm từng text/media item và **Kiểm điều kiện
hiện tại**. Với mỗi FACTUAL Claim Version được Draft tham chiếu, cần review
hiện hành ACCEPT của người duyệt và ít nhất một SUPPORTS Evidence Version cũng
đang ACCEPT, còn hiệu lực và mở lại đúng anchor/digest. CONTEXT/CONTRADICTS không
thay SUPPORTS. Draft còn assertion NEEDS_SOURCE/UNREVIEWED sẽ bị chặn; sửa bằng
Version mới có nguồn hoặc thể hiện EDITORIAL đúng bản chất, không đổi state để
bỏ qua việc kiểm nguồn. Quyền media và Approval vẫn là các điều kiện riêng.

Nhập người duyệt, tích **Đã xem toàn bộ nội dung/media và cách diễn đạt giả
thuyết editorial** trước khi duyệt, chọn
**APPROVED**, **REJECTED** hoặc **REVOKED**; từ chối/rút duyệt cần lý do. App kiểm
lại điều kiện khi ghi quyết định và khi chuẩn bị/ghi
Post. Nếu bị chặn, đọc version/lý do hiển thị, mở nguồn để xử lý rồi tạo nội dung
hoặc quyết định mới phù hợp. REVOKED ngăn đăng tiếp, giữ nguyên lịch sử đã có.

Trong **Post thủ công**, chọn **Chuẩn bị Post thủ công (PENDING)** sau khi duyệt.
Dùng **Sao chép nguyên văn text item cho Post PENDING** và kiểm media snapshot;
người dùng tự đăng ra nền tảng. Sau khi đã đăng đúng snapshot, điền URL/External
ID nếu biết và thời điểm có timezone, tích xác nhận đã đăng đúng snapshot rồi
**Ghi nhận PUBLISHED**. App không gửi
bài ra nền tảng và không tự xác minh bài ngoài nền tảng. **Bài đăng khác của
Package này** tạo record khác; không ghi đè Post trước.

Chọn Post PUBLISHED/REMOVED → **Số liệu / Observation của Post đã chọn**.
Nhập thời điểm đo có timezone, không trước lúc đăng; **Thêm metric** với key,
scope POST/ACCOUNT/OTHER, số, unit và definition version. Để trống số chưa biết;
chỉ ghi `0` khi đã đo bằng 0. Count phải nguyên, không âm; số phải hữu hạn.
**Lưu lần đo mới**, sau đó chọn lại lịch sử để đọc snapshot; **Lần đo mới** bổ
sung một lần khác. Không sửa lần đo cũ; tự kiểm định nghĩa metric của nền tảng.

## Mở lại, đóng app và xử lý lỗi

Đợi thông báo lưu thành công trước khi đóng dialog. Mở lại cùng data root, chọn
exact Version/Package/Post/lần đo để kiểm lịch sử; sửa mới không thay snapshot
cũ. Khi đóng Library, app chờ cleanup worker do nó sở hữu. Tác vụ giải mã media
có **Hủy giải mã media**; dùng nút hủy của tác vụ, không kill process bên ngoài.

Nếu media unavailable, kiểm kết nối đúng volume trong **Kho media** → **Kiểm
tra lại kết nối kho**. Không thay file cùng tên để giả khớp digest. Chỉ dùng
**Chuyển media của Asset đã chọn** để relocate có kiểm chứng; chọn kho cho media
mới không chuyển originals cũ. Giữ tệp và log khi digest mismatch/crash; xem
[quy tắc storage](Creator_Loop_Layout_Contract_V1.md), [media cancel](MEDIA_CANCEL.md) và
[runtime recovery](RUNTIME_RECOVERY.md).

Chọn **Đóng Library để chọn engine/model cục bộ** nếu có file đã chọn và thông
tin version/source/license. Preflight chỉ kiểm file/metadata rồi lưu lựa chọn;
không chứng minh engine chạy được, không tự tải/cài component. Xem
[component cục bộ](LOCAL_COMPONENTS.md). Bộ Whisper đã chọn dùng budget768MiB;
chọn rõ budget và kiểm/lưu lại, không lấy lịch sử lựa chọn làm preflight mới.
Chọn original VIDEO → **Chép lời Video bằng engine/model đã chọn**. Library
đóng để nhả lock; cửa sổ tác vụ cho chọn ngôn ngữ/budget khớp selection rồi
**Tạo PCM và chép lời máy**. Mỗi lần chạy tạo file/run mới; PCM thành công vẫn
giữ nếu chép lời lỗi. **Hủy tác vụ** hoặc đóng cửa sổ chờ worker thoát, giữ RAW
và lịch sử đã tạo. **Mở lại Library** xem từng task, không một completed toàn Asset.
Task chép lời có trong build CI79; nó không tự tạo Evidence hoặc ACCEPT.
Chọn task **AUDIO_TRANSCRIPTION / SUCCEEDED** → **Chọn đoạn từ task chép lời máy**.
Library nhả lock trước khi mở cửa sổ lựa chọn. Bấm **Đọc lại RAW**, chọn rõ một
đoạn để xem toàn bộ text máy, rồi **Đối chiếu âm thanh gốc** và bấm nghe đoạn.
**Tạo Evidence MODEL chờ duyệt** tạo bản mới neo ORIGINAL VIDEO/range audio,
giữ RAW và text máy; chưa tạo review. **Mở lại Library** để mở Evidence Audio,
sửa thành phiên bản HUMAN khi cần và review riêng. Nếu hủy/crash hoặc kết quả
chưa xác nhận, kiểm lịch sử trước khi tạo lại; không suy rằng giao dịch đã rollback.
Luồng chọn RAW này là source checkpoint, chưa có trong build CI79; xem
[RAW → Evidence](TRANSCRIPT_EVIDENCE.md) và [pipeline](VIDEO_TRANSCRIPTION_PIPELINE.md).
Chất lượng tiếng Việt, long input và tài nguyên trên máy8GB còn chờ nghiệm thu.

## Sao lưu, cập nhật và khôi phục

Đóng Library bằng **Đóng Library để sao lưu / cập nhật / khôi phục**. Làm theo
[maintenance](MAINTENANCE_UI.md) cho backup, staging, nâng schema, activation và
health. Giữ ZIP cùng manifest đúng bản; không chọn candidate/journal theo ngày
mới nhất. Giữ bản cài cũ và backup cho đến khi có bằng chứng health/khôi phục.

**Tạo backup DB hiện tại** công bố snapshot SQLite đã mở/validate, có ID và thời
điểm; nó không chứa media. Backup hiện được giữ vô thời hạn; dự trù dung lượng
và sao lưu media/volume riêng. Không copy riêng DB đang WAL hoạt động, không
chép backup đè DB đang mở. Xem [backup](UPDATE_BACKUP.md).

Khi restore, chọn rõ backup ID/candidate, đọc thời điểm/schema/media issues,
xác nhận mất thay đổi sau backup và acknowledgement media riêng khi cần. Media
lỗi vẫn chặn health; xác nhận không sửa bytes. DB hỏng hoặc completed-copy đã
bị đổi dùng luồng riêng trong [damaged restore](DAMAGED_RESTORE_UI.md) và
[fresh restore](FRESH_COMPLETED_RESTORE.md): proof/consent mới, bảo toàn bytes,
copy vẫn giữ guard cho đến health riêng thành công. Giữ journal/guard/log khi
lỗi; không tự xóa guard hoặc mở app không tương thích để vượt recovery.

Release notes và manifest của release thật phải công bố compatibility,
checksum và cách khôi phục. Checkpoint local trong HANDOFF chưa phải release
từ `main` sau required CI/tag gate.
