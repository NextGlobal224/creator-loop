# Mở app từ active installation

Sau [activation](UPDATE_ACTIVATION.md) thành công, dùng launcher có bộ schema
tương thích với DB hiện tại và trỏ nó tới installation root đã công bố:

```powershell
$env:CREATOR_LOOP_DATA_ROOT = 'C:\Users\YourName\AppData\Local\CreatorLoop'
& .\CreatorLoop.exe --launch-managed --installation-root 'C:\Users\YourName\Apps\CreatorLoop'
```

Không có server nền hoặc update im lặng. Launcher kiểm DB/history/integrity/FK và
pointer ACTIVE, revalidate toàn candidate inventory/digest/schema readable range
dưới app/SQLite writer lock. Nó giữ mọi version và user data, không tự chọn bản
cũ nếu pointer không có/HEALTH_FAILED, không restore hoặc migrate DB.

Parent nhả lock trước khi child lấy AppDataLock của chính nó. Child luôn dùng
`--compatible-only`: validate schema dưới lock và **không initialize/migrate**.
Nếu updater thắng khoảng giữa hai lần lấy lock, child trả contention hoặc schema
refusal, không âm thầm nâng schema để chạy. Direct `--compatible-only --smoke`
cũng không tạo DB/data root khi thiếu. Schema ngoài bộ hỗ trợ của launcher cần
launcher phù hợp; manifest không tự bổ sung code migration cho bản cũ.

Native Job owns desktop child và descendants. Session UI chạy đến khi người dùng
đóng, parent poll bằng retained process handle; không áp deadline của health cho
cả phiên desktop. Normal exit/cancel/parent crash cleanup Job riêng. Smoke/UI
smoke có deadline60s và CI outer runner120s. Không kill PID bên ngoài.

`logs/managed-launch-<id>/` giữ ownership trước resume và result metadata/exit code.
stdout/stderr UI được chuyển vào NUL, tránh ghi nội dung media/transcript hoặc
Qt decoder metadata vào log mặc định. Native tests kiểm riêng policy này và poll
live/normal exit cleanup child+grandchild. Health vẫn giữ stdout/stderr riêng để
parse JSON bounded; đây là hai mode khác nhau.

Exit3 là lock contention trước launch, exit4 là validation refusal, hoặc exit code
của child nếu nó đã chạy. UI smoke/normal exit không chứng minh engine/model thật
hay resource budget máy8GB. CI chạy cả schema smoke và desktop smoke từ exact ZIP
qua pointer đã activation/health; updater metadata/recovery/restore UI còn tiếp.
