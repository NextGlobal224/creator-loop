# Creator Loop — Repository, Installation & User Data Layout V1

**Cập nhật:** 29/09/2026. Đường dẫn dưới đây là **vai trò logic**, không ép ổ đĩa/thư mục cố định. Installer hoặc bản portable phải công bố vị trí data root, kiểm quyền ghi và backup trước update.

## 1. Ba vùng độc lập

```text
creator-loop-repo/                 # source, Git quản lý
├── app/
├── tests/
├── migrations/
├── packaging/
├── docs/
├── .github/workflows/
└── .gitignore

installation/creator-loop/<version>/   # replaceable; read-only khi chạy nếu có thể
├── app/
├── launcher/
├── migration-code/
└── release-manifest.json

user-data/creator-loop/             # persistent; không nằm trong checkout hay thư mục version
├── creator_loop.sqlite3
├── storage/originals/
├── storage/derived/
├── models/
├── components/
├── manifests/
├── backups/
├── logs/
└── runtime/                      # temp/ownership/lock, có chính sách dọn
```

Trên Windows có thể mặc định data root trong thư mục dữ liệu ứng dụng của người dùng và cho chọn vị trí chứa media lớn; không hardcode `D:\...` trong DB. `storage_key` là khóa tương đối/opaque trong data root hoặc volume đã đăng ký. Nếu media ở ổ khác, lưu volume identity/registered root và xử lý trường hợp ổ rời mất kết nối; không tự diễn giải `missing` là đã bị xóa.

## 2. Quyền sở hữu và xóa

- Git chỉ quản lý source, tests, migrations, workflow, docs và packaging. `.gitignore` loại DB, media, model, runtime, logs, secrets và build outputs; CI kiểm không vô tình commit file dữ liệu.
- Installation có thể thay nguyên thư mục version. Không lưu user DB/media ở đây. Không tự xóa bản installation trước cho đến khi health check release mới qua hoặc chính sách giữ bản cũ áp dụng.
- User data giữ xuyên release. Update app không xóa hoặc di chuyển original media. Component manager chỉ xóa thành phần do chính nó cài và ghi trong ownership manifest, sau khi xác minh không còn được version hiện hành dùng. Model/engine người dùng tự cài ở nơi khác không thuộc quyền dọn.
- Runtime ownership records được kiểm chống PID reuse; log có vòng đời và không chứa nội dung media/transcript hoặc token theo mặc định.

## 3. Manifest

- Release manifest (trong installation): version app, git commit, schema compatibility, artifact digest, runtime prerequisites, component compatibility.
- User data manifest: data root ID, schema version quan sát, storage roots đã đăng ký, component ownership/install manifest, last successful update và backup ID. Manifest chỉ là điều phối; SQLite vẫn là nguồn chuẩn quan hệ domain.
- Downloaded component manifest: name/version/source/license/digest, owned install path, thời điểm cài, trạng thái kiểm tra. Nếu version + digest đúng, không tải lại. Tải vào staging, kiểm digest trước khi activate.

## 4. Backup/restore

Backup DB qua SQLite Backup API. Nếu cần backup toàn bộ, đóng/đóng băng thao tác ghi Asset và chụp DB + storage cùng manifest ở một mốc xác định, kiểm digest/references. Restore kiểm compatibility và báo rõ dữ liệu tạo sau backup có thể mất. Không copy riêng file `.sqlite3` đang mở ở WAL mode để tuyên bố có backup nhất quán.

## 5. Test chấp nhận layout

- Xóa/cài lại installation không làm mất user data.
- Đổi vị trí installation không đổi Asset ID hoặc `storage_key`.
- Đường dẫn có khoảng trắng/Unicode hoạt động trên Windows; không cần quyền Administrator nếu chọn cài theo user.
- Rút ổ media rời → app báo unavailable và giữ bản ghi; gắn lại → resolve được.
- Component không thuộc ownership không bị updater/uninstaller xóa.
