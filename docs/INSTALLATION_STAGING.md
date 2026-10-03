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

**Stage chưa activate hoặc migrate DB.** Service không chạy candidate, không sửa DB/media và không thay installation đang dùng. Snapshot DB phải được tạo và kiểm trước phần update làm thay đổi DB. Orchestration backup→stage→migration/media-reference check→activate/health và restore có xác nhận vẫn đang triển khai. Không tự chạy candidate trên DB cũ chỉ để thử vì launcher có thể thực hiện migration; health trước update phải dùng fixture/copy, còn health sau migration phải được updater kiểm soát.

CI build và test exact ZIP, tạo manifest từ file đã giải nén/test, rồi stage ZIP đó và chạy launcher/schema smoke trên fixture bằng process runner60s. Log được đính kèm Windows artifact. Đây là kiểm tra staging/launcher; chưa thay nghiệm thu toàn luồng, engine/model, máy8GB hay release gate.

Digest xác minh bytes so với manifest đã chọn, không tự chứng minh ai phát hành manifest. Manifest hiện ghi GitHub Actions run/commit; signing/attestation chưa bật. Component compatibility vẫn ghi engine/model chưa chốt, nên artifact là checkpoint phát triển, chưa phải bản phát hành V1.
