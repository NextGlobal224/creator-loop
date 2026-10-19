# Creator Loop — CI/CD & Release Contract V1

## Additive B Web beta channel — user authority 18 October 2026

The [B Web deployment baseline](B_WEB_ARCHITECTURE.md) is an additional target,
not a Windows ZIP relabelled as a website. Required current GitHub checks and
independent technical review still gate integration. New Web tags use
`b-web-v<web version>-beta.<sequence>` and point to reviewed `main`. Such a tag
does not replace any `v0.1.0-beta.2` asset or imply a new desktop binary version.
The Web release contains the exact verified Worker bundle/static assets,
source/dependency/checksum/deployment manifests and a Vietnamese guide.
No live secret, DB, private media, QA session or private log enters public assets.

Before publication require fail-closed evidence for authenticated deployed
UI/API/media, durable SQLite/media and coordinated backup/confirmed restore,
idempotency/progress/cancel/unknown-outcome safety, both modes × all three Draft
formats, actual source-grounded output quality/save/reopen/version/review, and
independent access without Windows/localhost/Tunnel. Bind source, bundle hashes,
required CI run/review and exact Cloudflare version/model/live outputs. Local
fixtures/health/401 do not substitute for these gates. Preserve human publication
approval, all stable/full-beta gates, Windows beta.2 and A/C states. Rollback uses
a known compatible Worker version; restore of older DB state requires explicit
confirmation and preservation of the previous checkpoint/media. No paid fallback.

**Cập nhật:** 29/09/2026. **Phụ thuộc:** `Creator_Loop_Data_Architecture_V1_1.md` là nguồn chuẩn luật dữ liệu. Đây là contract cho ứng dụng Windows local-first, không phải quy trình deploy server. Chưa tạo Bootstrap Gate 1.

## 1. Nguyên tắc phát hành

- Feature branch → PR → **required checks** → `main` → tag từ commit đã được kiểm tra → build Windows → test **chính artifact sẽ phát hành** → manifest/provenance → release → người dùng chủ động cập nhật.
- Không silent auto-update V1. Không Docker Hub/SSH production. Không đẩy dữ liệu người dùng vào GitHub Actions.
- Branch protection/ruleset buộc CI thành điều kiện merge; workflow xanh mà không required không phải gate. Quyền GitHub token theo job tối thiểu; release job có quyền ghi, PR không có secrets phát hành. Pin third-party actions theo full commit SHA hoặc theo chính sách repository; lock dependencies, kiểm license/vulnerability có ngưỡng và quy trình xử lý rõ.
- Tag/release tạo từ commit trên `main`; artifact phải mang commit SHA, app version, schema version, build time, target OS/arch, dependencies/components compatibility. Hash file công bố cùng manifest; cân nhắc code signing và artifact attestation. Hash đơn lẻ không chứng minh nguồn build.

## 2. Required CI trên PR

| Job | Nơi chạy | Điều kiện bắt buộc |
|---|---|---|
| Fast | Ubuntu | Formatting/lint/type check, schema/locator contract, domain/backend/frontend tests phù hợp mã thực tế; không đặt job giả cho phần chưa có. |
| Data & migration | Ubuntu + Windows cho đường dẫn/SQLite | DB mới, nâng cấp từ mọi schema version còn hỗ trợ, kiểm `PRAGMA foreign_keys=ON`, `integrity_check`, `foreign_key_check`, `user_version`/schema version; migration thất bại không để DB mở cho app mới. |
| Architecture invariants | Ubuntu | Project reference đúng một target; review event đúng một target; anchor file cùng Asset; Claim→Evidence Version; Package/Draft cùng Project; Post/platform khớp Package; NULL khác 0; version và observation append-only. |
| Approval | Ubuntu | Public repository tạo Package building + items + seal nguyên tử; direct SQL building rỗng không seal/duyệt được; sau seal bất biến; caption đổi một ký tự hoặc media bytes đổi → fingerprint đổi → approval cũ không hợp lệ; REVOKED ngăn publish; kiểm race giữa duyệt và tạo lệnh đăng. |
| Windows lifecycle | Windows hosted | Path có khoảng trắng/Unicode, launcher, SQLite, subprocess, cancel/timeout/crash; chỉ dừng process thuộc Creator Loop. Fake engine dùng trong PR để test kiểm soát tiến trình, không tải model 488 MB mỗi lần. |
| Security | Ubuntu/Windows tùy tool | Dependency audit, secret scan, permissions workflow, package manifest và các nguồn tải component. Không làm job báo xanh vô điều kiện. |

Mọi test dùng fixture giả, không chứa dữ liệu riêng của người dùng. Windows runner kiểm tra tương thích OS; **không chứng minh** hiệu năng trên máy người dùng 8 GB.

## 3. Ownership và lifecycle

Runtime ownership record gồm `run_id`, PID, canonical executable path, thời điểm/identity tạo process, parent/job identity khi có và component version. Trước terminate phải đối chiếu với tiến trình hiện tại để chống PID reuse; không kill process ngoài quyền sở hữu. Trên Windows ưu tiên quản lý process tree bằng primitive phù hợp (ví dụ Job Object) nếu triển khai bảo đảm được. Cancel, normal exit và crash recovery có test riêng. Mục tiêu sau khi đóng app: không còn worker **do app sở hữu**; worker của ứng dụng khác không bị ảnh hưởng.

Facebook có thể kết nối qua Playwright/CDP loopback tới Chrome đang mở sau khi user bật/cho phép DevTools. Chỉ điều khiển đúng tab Facebook đã chọn; không đọc/xuất cookie/token, tạo profile/context mới hoặc thao tác tab ngoài Facebook. Chrome/context/tab của user không thuộc Job/ownership của app: normal exit/cancel/timeout/crash chỉ ngắt kết nối và cleanup worker/driver do app sở hữu, giữ Chrome/phiên user mở. Private Chromium do app launch vẫn theo ownership cũ. Required tests phải chứng minh cả hai lifecycle; source/mock không thay native nghiệm thu artifact.

## 4. Release gate

1. Build từ tag/commit xác định trên Windows sạch; đóng gói runtime cần thiết cho app hoặc nêu rõ prerequisite và kiểm tra ở preflight. Tách engine/model lớn qua component manager với version, digest, nguồn tải và quyền sở hữu.
2. Giải nén **artifact cuối** vào đường dẫn có khoảng trắng và tiếng Việt. Kiểm cấu trúc package, launcher, tạo DB mới, mở bản sao DB cũ sau migration, basic asset/evidence flow, đóng app sạch; so digest artifact trước/sau test.
3. Test thực trên máy Windows mục tiêu 8 GB trước release: engine/model thật, video/ảnh/text mẫu, thiếu RAM, thiếu dung lượng, cancel, crash recovery và thời gian khởi động. Ghi kết quả, không suy từ runner hosted.
4. Manifest ghi `app_version`, `git_commit`, `schema_from/schema_to`, `artifact_sha256`, file manifest/digest, minimum OS/runtime, component compatibility, migration IDs, release notes và hướng khôi phục. Chữ ký/attestation nếu bật phải kiểm được bằng quy trình công bố.
5. Release thủ công hoặc gated; chỉ upload đúng artifact đã test. Không rebuild khác bytes sau gate rồi dùng kết quả test cũ.

## 4.1. Gate beta riêng; V1 ổn định giữ nguyên

Gate này chỉ có hiệu lực cho một quyết định phát hành **beta/pre-release** được người có quyền duyệt. Không đổi luật domain/schema, ownership, budget, quyền media, Package seal, Approval/REVOKED, backup nhất quán hoặc bất cứ tiêu chí V1 ổn định nào ở mục4. Beta không phải xác nhận product/V1 hoàn tất. Bản PR/installation dùng cá nhân không tự trở thành release beta.

Điều kiện beta bắt buộc:

1. Review độc lập hợp lệ trên GitHub và required checks được thực thi trên exact head/base của từng PR phụ thuộc; finding còn mở phải xử lý. Nguồn cuối phải có trên `main`; tag phải trỏ đúng commit đã kiểm. Retarget/merge làm đổi merge tree thì chạy required CI tương ứng, không chuyển bằng chứng synthetic merge cũ sang nguồn mới.
2. Build Windows từ nguồn/tag xác định, kiểm **chính ZIP cuối**: inventory/manifest/provenance, đường dẫn khoảng trắng/Unicode, launcher, DB mới và mọi migration còn hỗ trợ, Library/Evidence/basic product flow, consistent DB backup/restore, containment/ownership/normal exit/cancel/crash recovery. Giữ scope thực đo; code hoặc artifact đổi bytes thì bổ sung kiểm ảnh hưởng. Hosted CI không thay đo hardware thật.
3. Khai báo chính xác engine/model ngoài package: name/version/digest/source/license/local-use, preflight và native runtime đã kiểm; giữ budget đã chốt. Trạng thái compatibility beta chỉ được ghi đúng scope; không gán `VERIFIED_V1` cho phép đo có giới hạn hoặc quality chưa ACCEPT. Không bundle hoặc tải lại component chỉ để phát hành.
4. Manifest, checksum, release notes, readable schema range, minimum OS/runtime, component compatibility và hướng cập nhật/khôi phục đầy đủ, cùng binding commit/run/ZIP. Không sửa metadata của artifact development cũ để biến thành release; pipeline phải tạo metadata trung thực cho artifact mới. Không rebuild khác bytes sau gate.
5. Publish có quyền, qua checker beta riêng fail-closed và GitHub **pre-release=true**; stable channel vẫn dùng toàn bộ gate V1. Không bỏ/bypass checker hiện hành, approval hoặc CI. Version/tag phải tuân parser/manifest; nếu binary vẫn `0.1.0`, không báo binary `0.1.0-beta.1`. Nhãn pre-release trên GitHub là thuộc tính kênh phát hành, không đổi version trong executable.

Chỉ các phạm vi dưới đây được hoãn nghiệm thu sang V1 ổn định; phải hiện rõ trong release notes/ledger, không đổi FAIL/SKIP/chưa kiểm thành PASS:

| Phạm vi hoãn | Beta phải công bố | V1 ổn định vẫn bắt buộc |
|---|---|---|
| Chứng nhận Windows mục tiêu8GB và thiếu RAM/dung lượng **thực** | Môi trường/số đo đã kiểm; chưa chứng nhận physical shortage hoặc hỗ trợ rộng. Giữ các negative/ownership/backup guards và budget. | Toàn bộ mục4.3 và PRODUCT_ACCEPTANCE01/20/23. |
| Chất lượng ASR tiếng Việt đại diện | Quality chưa nghiệm thu; RAW/MODEL có thể sai, phải nghe/đọc, sửa HUMAN và review riêng trước khi dùng. Không hứa accuracy từ mẫu corpus giới hạn. | Mẫu đại diện/reference/quyền dùng và quyết định chất lượng của người có trách nhiệm;20/23. |
| PUBLISHED–Observation vận hành ngoài ứng dụng | Chưa nghiệm thu thao tác ngoài ứng dụng; không tự đăng, giả metadata hoặc tạo human approval. Guard/Post/Observation nội bộ vẫn phải kiểm. | Quyền và bằng chứng vận hành thật theo16/17/18. |

`check_release_gate.py`/workflow chỉ hỗ trợ stable tại checkpoint chuẩn bị09/10/2026. Duyệt gate tài liệu chưa làm checker hỗ trợ beta: phải triển khai channel selection có ràng buộc, beta-specific evidence/manifest checks và tests từ chối bypass trước khi public release. Không dùng `--generate-notes` làm bằng chứng đã hoàn thành beta hoặc V1.

## 5. Update contract trên máy người dùng

`STOP OWNED WORKERS → exclusive app/DB lock → preflight disk/write permissions → consistent DB backup → validate backup opens + integrity/FK/schema → stage new installation → migration → integrity_check → foreign_key_check → schema_version check → media/storage reference check → activate new app → health check`.

- **Backup SQLite:** dùng SQLite Backup API (hoặc cơ chế snapshot SQLite được kiểm chứng), không copy riêng `.sqlite3` khi WAL còn hoạt động. Backup có ID, timestamp, app/schema version, size/digest; mở backup và kiểm tra nó trước khi migration.
- **Media consistency:** V1 update không sửa/xóa originals/derived user data. Trước và sau migration kiểm các `storage_key` bắt buộc; nếu chính migration thay quan hệ media, phải thiết kế backup/restore media nhất quán và kiểm thử riêng. Backup DB đơn lẻ không phải backup toàn bộ dữ liệu.
- **Package sealing:** tạo Package building, toàn bộ items, fingerprint và `sealed_at` trong một transaction; chỉ sealed snapshot mới được Approval tham chiếu. Test direct SQL building thiếu item, sai Project/Draft và UPDATE sau seal.
- **Migration:** chạy theo version tuần tự, có idempotency/guard rõ; mỗi migration bọc transaction khi SQLite operation cho phép. Không cho hai updater/app cùng mở DB để ghi. Ghi migration history và schema version sau khi thành công. `integrity_check` không thay `foreign_key_check`.
- **Failure:** không mở DB bằng app không tương thích; không cố rollback chỉ bằng executable. Giữ backup, bản installation trước và log lỗi không chứa nội dung riêng nhạy cảm. Restore DB chỉ theo quy trình có xác nhận rằng các thay đổi sau mốc backup sẽ mất; nếu migration chưa commit và transaction rollback được, ưu tiên giữ DB hiện tại.
- **Health:** mở connection, đọc schema version, truy vấn tối thiểu, resolve một storage reference mẫu, kiểm launcher/worker; có timeout. Không chạy model nặng chỉ để health check.

## 6. Rollback và tương thích

Mỗi release công bố schema range app có thể đọc. Có thể quay lại app cũ **chỉ khi** app cũ hỗ trợ schema hiện tại. Nếu không, khôi phục backup DB trước update cùng installation tương ứng, kèm đánh giá media thay đổi từ sau backup. Migration có thể chỉ đi tới; không hứa downgrade tự động. Bản backup và thời hạn giữ phải được nêu trong UI update.

## 7. Gates trước commit đầu tiên

Repository skeleton, `.gitignore`, docs baseline, workflow có kiểm tra thực, fixture DB, migration 0001, launcher Windows tối thiểu và một smoke test artifact. Không cần triển khai toàn bộ domain trước commit đầu, nhưng CI phải chạy và chặn vi phạm đang có. Mọi thay đổi luật dữ liệu đi qua PR cập nhật **baseline + migration + test** trong cùng thay đổi.

### Beta publication sequencing

### B-only beta profile — explicit user authority 18/10/2026

The user separately authorizes publishing B without reopening A discovery or paid C.
This is additive `release_channel=beta, release_scope=B`, validated by
`b_release_evidence.py`; absence of that explicit scope retains the full-beta checker.
Stable/full-beta gates and their 24 requirements are unchanged. B receipt requires
all modes/formats, real image with unchanged caption, source/version/choice/reopen,
manual publication guards, exact package config→Worker/model→compatible live run,
independent source review, required CI, exact native B UI/lifecycle/upgrade/backup/
restore/privacy tests and packaged original output bytes. B native artifact criteria
cannot be substituted by source tests or reused receipts. Reuse must bind actual
implementation/source/config and retain original live origins; no fabricated new run.
The full project remains partial; excluded A-new-scan, C-render/voice-quality,
full-beta and stable-V1 plus the three stable deferrals must remain explicit.
The same reviewed-main/tagged-build/identical-ZIP publication and redownload sequence
below applies. No AI APPROVE, waived protection or publication to Facebook.

### Tagged beta publication (all profiles)

### B Web manual beta — explicit user amendment 19/10/2026

`release_scope=B_WEB_MANUAL_BETA` qualifies only MANUAL TEXT and MANUAL
IMAGE_CAPTION. Both require real source-grounded Vietnamese output, actual image
bytes with provenance/rights, saved/reopened original and edited versions/review,
authenticated durable cloud storage, progress/cancel/idempotency, independent
HTTPS browser proof and exact reviewed source/required CI/deployment/tag/artifact/
checksum/publication/redownload. No local or storage-only PASS substitutes quality.
The full `B_WEB` six-flow profile, desktop B scope, stable and full-beta gates
remain unchanged. Automatic stays in the backlog and existing features remain.
VIDEO inheritance awaits C contract acceptance and verified B integration; no
new VIDEO run or VIDEO evidence is required for this manual-beta release.
Canonical Windows Access-denied backup still blocks principal data transfer;
isolated development/QA is allowed and cannot certify the principal backup.
Keep Windows beta.2 immutable, A/C status, grants/reservations/failures, $0 and
manual approval before publication. Two qualified flows do not complete all B.

Beta tags use `v<actual numeric app version>-beta.<positive sequence>`; this does not change the binary's numeric app_version. Tag from reviewed main triggers the unchanged required CI build; stable auto-publication skips beta tags. Agent downloads that immutable tagged ZIP, completes native eligibility and writes a sanitized structured receipt binding commit/build-run/original manifest/ZIP/EXE. Only then dispatch `publish_beta.yml`: validate exact tag/main/build jobs, finalize metadata without rebuilding ZIP, check every file and all beta scopes, publish identical bytes with `--prerelease`. Missing/stale/source-only/mock QA is refused. Default manifest writer remains development. Stable checker/24 criteria are retained; exactly three user-approved stable deferrals are enumerated, never entire requirement-row exclusions. No new scope or review/branch-protection bypass is authorized by the receipt.
