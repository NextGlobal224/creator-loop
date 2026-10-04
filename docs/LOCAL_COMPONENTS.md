# Component cục bộ — preflight artifact

Creator Loop V1 dùng engine/model người dùng đã chọn và được phép dùng cục bộ.
`--check-components` kiểm các file đã khai báo, giữ handle chỉ đọc trong lượt kiểm
và xuất JSON metadata. Lệnh chạy trước khi khởi tạo user data hoặc mở SQLite.
Không tải, chạy, cài, activate hay xóa file component; `owned` phải là `false`.

## Khai báo lựa chọn

Lưu JSON riêng ngoài Git/build. Manifest tối đa 64 KiB, 1–16 component; mọi field
bên dưới bắt buộc, không nhận field dư hoặc key trùng. Ví dụ là mẫu cần điền,
không phải engine/model đã được chọn hay license đã nghiệm thu:

```json
{
  "manifest_version": 1,
  "components": [
    {
      "component_id": "selected-engine",
      "kind": "ENGINE",
      "path": "D:\\MyComponents\\selected-engine.exe",
      "version": "REPLACE_WITH_SELECTED_VERSION",
      "sha256": "REPLACE_WITH_64_LOWERCASE_HEX_DIGITS",
      "byte_size": 123456,
      "source_url": "https://example.invalid/replace-with-original-source",
      "license": "REPLACE_WITH_LICENSE_NAME_AND_PERMISSION_BASIS",
      "license_url": "https://example.invalid/replace-with-license-source",
      "local_use_allowed": true,
      "owned": false,
      "worker_memory_bytes": 536870912
    }
  ]
}
```

`kind` là `ENGINE` hoặc `MODEL`. Mỗi file cần ID riêng và path tuyệt đối canonical;
không nhận link, junction/reparse hoặc hardlink. Không tìm trên PATH, không chọn
file thay thế khi thiếu. Source/license URL phải là HTTPS công khai, không chứa
credentials, query hoặc fragment. Ghi nguồn/license thực của đúng artifact và
chỉ xác nhận `local_use_allowed` khi đã có căn cứ cho quyền dùng cục bộ.

Version, source và license là khai báo của lựa chọn. Hash/size đối chiếu file vật
lý; đối chiếu digest với nguồn đã chọn khi nguồn công bố digest. Hash tự tính
không xác nhận nhà phát hành hoặc tính đúng của khai báo version/license. Lượt
kiểm này không thực thi lệnh version, không kiểm format/model compatibility và
không thay nghiệm thu engine/model thật.

`worker_memory_bytes` là commit budget dự kiến cho **một worker tuần tự**, trong
khoảng 64 MiB–4 GiB của native Job. CLI hiện từ chối khai báo vượt cap mặc định
512 MiB. Service có tham số cap cho caller tương ứng với Job của tác vụ; không
cộng ngân sách các worker tuần tự, không suy RAM từ kích thước model. Preflight
chưa tạo worker engine hay đo RAM; lựa chọn cap thật và kiểm máy 8 GB còn thiếu.

## Chạy kiểm tra

Với bản build đã có entry point này:

```powershell
CreatorLoop.exe --check-components 'D:\MyComponents\selected-components.json'
```

Từ checkout, dùng runner timeout hiện có để cả lượt kiểm có giới hạn tiến trình
và stdout/stderr được lưu thành log:

```powershell
$env:PYTHONPATH = 'app'
& .\scripts\run_test_with_timeout.ps1 -Executable .\.venv\Scripts\python.exe `
  -TimeoutSeconds 180 -CommandArgs @('-m','creator_loop','--check-components',
  'D:\MyComponents\selected-components.json')
```

Exit 0 xuất `LOCAL_ARTIFACTS_VERIFIED`, `runtime_compatibility_verified: false`
và ID/kind/version/digest/size/ownership. Exit 1 là file/metadata/budget không
đạt; sửa đúng lựa chọn rồi kiểm lại. Không kết hợp với thao tác DB/update/smoke.
Lỗi không làm mất component hoặc sửa registry/user DB. Log chỉ chứa metadata,
không chứa nội dung model, transcript hoặc credentials.

Service `hold_verified_components` hash từng file theo chunk tối đa 1 MiB,
đối chiếu identity/size/digest và giữ handle qua context sử dụng. Windows từ chối
write/delete/replace trong thời gian giữ lease; exit/error/cancel nhả các handle
của lượt đó. Không dùng kết quả CLI cũ để activate một path sau khi lease đã nhả;
caller xử lý phải kiểm lại và giữ lease xuyên tác vụ. Trên CI portable, fingerprint
được kiểm tại thời điểm hash, không có bảo đảm Windows sharing sau khi yield.

Deadline/cancel trong service là cooperative giữa các lần đọc. Chạy service ngoài
GUI và trong worker có process timeout khi nối UI/pipeline, vì filesystem I/O có
thể không trả về. CLI nguồn/probe CI được bọc timeout; UI selection/config, registry
điều phối và engine pipeline còn ở mốc tiếp theo.

## Bằng chứng và phần còn thiếu

`tests/test_local_components.py` dùng file **fake** để kiểm bytes/identity/bounds,
license/ownership khai báo, alias, cancellation/deadline và Windows read-sharing.
`scripts/check_local_components.py` kiểm chính candidate EXE với fake component,
digest sai, ownership bị từ chối và user data giữ nguyên; nối trong Windows CI.
Các bằng chứng này không nghiệm thu engine/model thật, ownership cài đặt,
activation/cleanup, version probe, xử lý transcript/vision hoặc release 8 GB.
Trạng thái checkpoint và kết quả chưa xác minh nằm ở [HANDOFF](HANDOFF.md),
phạm vi bắt buộc giữ nguyên ở [PRODUCT_ACCEPTANCE](PRODUCT_ACCEPTANCE.md).
