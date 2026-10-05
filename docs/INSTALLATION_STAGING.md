# Kiểm tra và stage bản cài mới

Tạo thư mục installation riêng với data root và source checkout. Giữ ZIP cùng `release-manifest.json` từ nguồn phân phối đã chọn. Chạy thủ công bằng bản CreatorLoop đang có chức năng staging:

```powershell
$env:CREATOR_LOOP_DATA_ROOT = 'C:\Users\YourName\AppData\Local\CreatorLoop'
New-Item -ItemType Directory -Force 'C:\Users\YourName\Apps\CreatorLoop'
& .\CreatorLoop.exe --stage-update 'C:\Downloads\CreatorLoop.zip' `
  --release-manifest 'C:\Downloads\release-manifest.json' `
  --installation-root 'C:\Users\YourName\Apps\CreatorLoop'
```

Source dùng `PYTHONPATH=app` và `python -m creator_loop` với cùng tham số. Exit0 trả tên thư mục candidate `<app_version>-<commit_prefix>-<id>`; exit4 nghĩa là staging thất bại. Candidate chứa `CreatorLoop/` và manifest. Các thư mục version trước vẫn được giữ, không tự xóa. Không có update im lặng.

Staging kiểm digest ZIP và từng file, inventory khớp chính xác, size/budget, đường dẫn Windows an toàn, collision/case aliases, duplicate và linked/special members. Manifest phải có commit/version/target, schema readable range, migration IDs, runtime/dependencies, component compatibility và recovery/provenance notes. Không giải nén file ngoài staging. Lỗi chỉ dọn staging được tạo bởi lượt đó; process crash có thể để lại thư mục ẩn `.id.staging` chưa được công bố là candidate.

Trên Windows, I/O giải nén, kiểm inventory và đọc migration dùng dạng extended path cho các file nội bộ có đường dẫn vượt260 ký tự. Tên candidate, binding và kiểm separation/ownership vẫn dùng đường dẫn canonical ban đầu; không đổi registry hoặc cần quyền Administrator để ghi các file này. Đây không phải cam kết mọi engine, Explorer hoặc công cụ ngoài hỗ trợ đường dẫn dài. Căn cứ API: [Microsoft — giới hạn đường dẫn](https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation).

Frozen runtime hook dùng I/O alias của cùng bundle; nếu volume đã có tên8.3 ngắn
hơn và samefile, Qt/import/DLL resources dùng alias đó. Không tạo junction,
mapping, bật short-name hay sửa registry. Qt có thể canonicalize extended path
về tên dài và không tải được DLL/plugin. Khi volume không cung cấp alias phù hợp,
đường dẫn sâu có thể bị từ chối ngay ở bootstrap/health; updater giữ candidate,
backup và version cũ theo [activation](UPDATE_ACTIVATION.md), không coi stage
thành công là app đã chạy được. Chọn installation root ngắn hơn cho lần chuẩn bị
mới nếu gặp giới hạn này; không tự di chuyển hoặc xóa dữ liệu/installation cũ.

**Stage chưa activate hoặc migrate DB.** Service không chạy candidate, không sửa DB/media và không thay installation đang dùng. Snapshot DB phải được tạo và kiểm trước phần update làm thay đổi DB. Dùng [preparation](UPDATE_PREPARATION.md) cho backup→stage→migration/media-reference check, rồi [activation/health](UPDATE_ACTIVATION.md); nếu bị gián đoạn, dùng [recovery](UPDATE_RECOVERY.md) và quy trình restore có xác nhận theo hướng dẫn. Không tự chạy candidate trên DB cũ chỉ để thử vì launcher có thể thực hiện migration; health trước update phải dùng fixture/copy, còn health sau migration phải được updater kiểm soát. Các service này chưa thay nghiệm thu updater/release toàn V1.

CI build và test exact ZIP, tạo manifest từ file đã giải nén/test, rồi stage ZIP đó với process timeout120s; launcher/schema smoke riêng trên fixture dùng60s. Log được đính kèm Windows artifact. Đây là kiểm tra staging/launcher; chưa thay nghiệm thu toàn luồng, engine/model, máy8GB hay release gate.

Digest xác minh bytes so với manifest đã chọn, không tự chứng minh ai phát hành manifest. Manifest ghi GitHub Actions run/commit hoặc local checkpoint method khi được tạo cục bộ; signing/attestation chưa bật. Component compatibility vẫn ghi engine/model chưa chốt, nên artifact là checkpoint phát triển, chưa phải bản phát hành V1.
