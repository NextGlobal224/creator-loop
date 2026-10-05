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
khoảng 64 MiB–4 GiB của native Job. CLI mặc định512 MiB; cap khác phải được chọn
tường minh bằng `--component-worker-memory-mib` khi check/select. Service có tham số cap cho caller tương ứng với Job của tác vụ; không
cộng ngân sách các worker tuần tự, không suy RAM từ kích thước model. Preflight
chưa tạo worker engine hay đo RAM; lựa chọn cap thật và kiểm máy 8 GB còn thiếu.

## Kiểm phiên bản engine đã lưu trên Windows

Trong cửa sổ Engine/model, dùng **Kiểm phiên bản engine đã lưu** sau khi lưu lựa
chọn. Có thể chạy CLI riêng khi app sử dụng data root đó đã đóng:

```powershell
$env:CREATOR_LOOP_DATA_ROOT = 'D:\MyCreatorLoopData'
CreatorLoop.exe --check-whisper-runtime
```

Lệnh không khởi tạo/migrate DB, không đổi lựa chọn hoặc nghiệm thu. Nó cần DB
hiện có/không có restore guard, kiểm lại size/hash/lease cả6file của profile CPU
whisper.cpp1.8.7/base multilingual đã chọn; kiểm header AMD64 PE32+ từ held file,
rồi chạy đúng EXE `--version` dưới native Job/cap đã lưu. PATH/cwd chỉ System32;
không tải, tìm executable thay thế, activate hoặc dọn component unowned. Header
theo [Microsoft PE format](https://learn.microsoft.com/en-us/windows/win32/debug/pe-format);
static header check không chứng minh mọi DLL đã được loader sử dụng.

Worker GUI có timeout180s/cap256MiB cho I/O; child engine version có deadline15s,
cap riêng theo lựa chọn (vẫn chịu giới hạn Job cha). Stdout/stderr engine đi qua
private pipe tối đa32KiB, chỉ nhận đúng một dòng version; không lưu transcript
hoặc raw engine output vào default logs. Timeout/cancel chỉ dừng native cây do
worker sở hữu. Outer CLI logs chỉ ghi JSON metadata/error category; ownership
metadata được giữ. Cleanup pipe chưa xác nhận thì giữ buffer/lease và từ chối.

Exit0 `ENGINE_CLI_VERSION_CONFIRMED` xác nhận CLI báo đúng1.8.7 trên máy đang
chạy. `model_executed`, `model_compatibility_verified` và
`runtime_compatibility_verified` đều `false`; không tự nâng saved selection.
Exit3 root đang bận; exit1 thiếu lựa chọn/file/prerequisite, guard, hash/PE/version,
output hoặc deadline không đạt. Version-only không chạy model và không chứng
minh delay-loaded dependencies, chất lượng, resource8GB hoặc clean-target runtime.
Build41c8d43 trước source mới chưa có entry point này; không gán proof cho build cũ.

Runtime đã chọn có imports MSVCP140/VCRUNTIME140/VCRUNTIME140_1/VCOMP140 và
Windows UCRT, ngoài năm file engine. Theo
[Microsoft Visual C++ runtime](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist),
Redistributable phải đúng kiến trúc x64 và không cũ hơn toolchain dùng để build
binary. Minimum runtime/toolchain version cùng clean-target test của bộ này
chưa được nghiệm thu; version-only PASS trên máy đang có DLL không thay kiểm đó.
Creator Loop không chép System32 DLL vào engine/build hoặc tự cài Redistributable.
License MIT của engine/model không cấp quyền phân phối Microsoft runtime.

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
thể không trả về. UI dưới đây dùng worker có Job riêng và process deadline;
CLI nguồn/probe CI được bọc timeout. Engine pipeline còn thiếu.

## Bằng chứng và phần còn thiếu

### Lưu lựa chọn đã kiểm

Với DB đã được tạo bằng app hiện tại, đóng Library rồi chạy:

```powershell
CreatorLoop.exe --select-components 'D:\MyComponents\selected-components.json'
```

Từ source, dùng cùng runner timeout bên trên và thay `--check-components` bằng
`--select-components`. Lệnh kiểm lại bytes và giữ lease qua lượt lưu. App lock
và SQLite writer reservation điều phối với registry/updater; không migration,
không thay dữ liệu domain, không cài hay nhận ownership component. Exit 3 khi
app đang dùng data root; exit 1 khi file/metadata/schema không đạt. Root/DB thiếu
hoặc hỏng không được tự tạo, sửa hay migrate bởi lệnh này.

Lựa chọn được lưu tại `manifests/storage-roots.json`, field `component_selection`,
cùng timestamp kiểm và `runtime_compatibility_verified: false`. Root ID,
registrations/default/offline roots, component installations, backup/update IDs
và metadata khác được giữ. Với manifest chưa có, tạo root ID một lần từ data root
có DB hợp lệ. Đây là điều phối, không thêm bảng SQLite. Metadata cũ malformed hoặc
schema quan sát không khớp bị từ chối để giữ chẩn đoán; không tự sửa lịch sử.

`load_component_selection` chỉ đọc khai báo và mốc kiểm cũ, kể cả khi component
đã đổi hoặc offline. Nó không cấp phép chạy; thao tác processing phải kiểm lại và
giữ lease riêng. Atomic publication lỗi/hủy trước publish giữ lựa chọn trước;
sau publish thành công, lựa chọn mới có thể tồn tại dù caller chưa nhận stdout.
Đọc lại trạng thái trước khi quyết định thử lại; không lấy việc thiếu stdout làm
căn cứ xóa/khôi phục file component. Engine pipeline còn thiếu.

### Chọn và lưu qua cửa sổ Component

Trong Library, chọn **Đóng Library để chọn engine/model cục bộ**, hoặc chạy
`CreatorLoop.exe --components`. Từ checkout: đặt `PYTHONPATH=app`, rồi chạy
`python -m creator_loop --components`. Dùng đúng data root của Library; nếu DB
chưa có, mở Library để tạo DB trước. Cửa sổ Component không tạo hay migrate DB.

1. Chọn JSON khai báo ở trên và bấm **Kiểm file đã chọn**.
2. Đối chiếu ID, loại, phiên bản khai báo, path, nguồn/license, digest và size trong
   bảng. Di chuột để xem giá trị đầy đủ và license URL; cuộn ngang khi cần.
3. Sau khi đối chiếu quyền dùng cục bộ, tự đánh dấu ô xác nhận rồi bấm
   **Kiểm lại và lưu lựa chọn**. Ô xác nhận mặc định bỏ chọn.

Chọn budget worker512MiB (mặc định) hoặc768MiB cho Whisper base CPU đã chốt.
Đổi budget xóa review/consent cũ; kiểm và đối chiếu lại trước lưu. Đây là giới
hạn được cấu hình, chưa chứng minh engine chạy được hoặc đủ RAM trên máy đích.
CLI check/select nhận `--component-worker-memory-mib N` (64–4096), không truyền
thì vẫn512; không kết hợp option này với tác vụ khác. Task Video hiện hỗ trợ
512/768 và yêu cầu budget khớp selection đã lưu, không tự tăng khi xử lý lỗi.

Worker kiểm lại bytes ngay trước lưu. Kết quả review gắn với toàn bộ khai báo;
thay path hoặc sửa khai báo sau review yêu cầu kiểm lại. Đổi nội dung file dù giữ
nguyên khai báo cũng bị kiểm digest từ chối. CLI có thể dùng
`--select-components MANIFEST --reviewed-components FINGERPRINT`, với fingerprint
trong kết quả `--check-components`, để giữ cùng ràng buộc review. Fingerprint là
đối chiếu khai báo, không phải chứng nhận license hay khả năng chạy engine/model.

**Xem lựa chọn đã lưu** hoặc `CreatorLoop.exe --inspect-components` chỉ đọc lịch sử
và mốc kiểm. File đã offline/thay đổi vẫn có thể hiện trong lịch sử; kết quả ghi
`freshly_verified: false`, không bật quyền lưu hay chạy từ bằng chứng cũ.

Kiểm/lưu chạy ngoài GUI với Job 256 MiB, deadline mặc định 180 giây và output tối đa
128 KiB; đây là budget tác vụ kiểm, không phải budget engine đã nghiệm thu. **Hủy
tác vụ** chỉ dừng cây worker thuộc lượt này. Đóng cửa sổ khi đang kiểm chờ cây đó
thoát; nếu cleanup chưa xác minh, cửa sổ và ownership/log được giữ. Log nằm dưới
`logs/component-check-<id>/` trong data root. Hủy/timeout sau atomic publication
có thể đã lưu thành công: mở lại lịch sử trước khi thử lại. Không xóa component
hoặc registry để xử lý lỗi. Sau khi đóng Component, mở lại Library bình thường.

`tests/test_local_components.py` dùng file **fake** để kiểm bytes/identity/bounds,
license/ownership khai báo, alias, cancellation/deadline và Windows read-sharing.
`scripts/check_local_components.py` kiểm chính candidate EXE với fake component,
digest sai, ownership bị từ chối và user data giữ nguyên; nối trong Windows CI.
Các bằng chứng này không nghiệm thu engine/model thật, ownership cài đặt,
activation/cleanup, version probe, xử lý transcript/vision hoặc release 8 GB.
`tests/test_component_selection.py` kiểm app/DB lock thực, preserve root/registry/
domain, CLI subprocess, stale history, corrupt/missing DB, alias, budget và lỗi
atomic publication bằng fake component; không nghiệm thu actual engine/model.
`tests/test_component_ui.py` kiểm consent/review đổi, lịch sử offline, app lock,
CLI thật, native cancel/deadline/sentinel và Library nhả lock trước mở Component.
Candidate probe có check, save-review, inspect, UI smoke và stale-review refusal;
source probe không thay bằng chứng exact frozen candidate/required CI.
Trạng thái checkpoint và kết quả chưa xác minh nằm ở [HANDOFF](HANDOFF.md),
phạm vi bắt buộc giữ nguyên ở [PRODUCT_ACCEPTANCE](PRODUCT_ACCEPTANCE.md).
