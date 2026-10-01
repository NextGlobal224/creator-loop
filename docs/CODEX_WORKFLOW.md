# Cách dùng Codex trong Creator Loop

Hướng dẫn này được hợp nhất từ bộ `Codex-Kit-v2` cho repo Creator Loop. [AGENTS.md](../AGENTS.md) quy định cách làm; [HANDOFF.md](HANDOFF.md) giữ trạng thái đang làm. Ba file này không thay thế contract, source, Git hoặc kết quả CI. Việc tạo đủ file chưa phải nghiệm thu hành vi thiết lập.

## 1. Tôi chọn cách làm nào?

| Bạn muốn làm gì | Cách giao việc |
|---|---|
| Bắt đầu một dự án mới | Gọi `$start-project` trong thư mục dự án mới và đưa ý tưởng, người dùng, chức năng, ràng buộc |
| Làm một task mới trong sản phẩm đã có | Dùng mẫu “Task mới” dưới đây |
| Tiếp tục công việc dở dang hoặc đổi chat/tài khoản | Dùng mẫu “Tiếp tục công việc” |
| Dừng phiên để lần khác làm tiếp | Dùng mẫu “Bàn giao” |

“Task mới” là một việc trong dự án hiện tại; không đồng nghĩa “dự án mới”. Skill `start-project` chỉ dùng cho trường hợp thứ nhất.

Với Creator Loop, giữ thứ tự luật đã chốt: [Data Architecture V1.1](Creator_Loop_Data_Architecture_V1_1.md) → [CI/CD & Release Contract V1](Creator_Loop_CICD_Release_Contract_V1.md) → [Layout Contract V1](Creator_Loop_Layout_Contract_V1.md) → [Master Prompt V2](Codex_Master_Prompt_V2.md). Nếu xung đột, dừng phần phụ thuộc và xử lý theo Master Prompt; không tự đổi baseline.

## 2. Ba mẫu giao việc hằng ngày

### Task mới

```text
Mục tiêu: [Kết quả cần đạt].
Phạm vi: [Phần được phép sửa].
Hoàn thành khi: [Hành vi hoặc đầu ra kiểm tra được].
Dữ liệu tái hiện/file liên quan: [Nếu đã biết].

Hãy thực hiện và kiểm tra phù hợp đến khi đạt mục tiêu.
Đọc hướng dẫn và source cần thiết; mở rộng phạm vi khi cần
đạt mục tiêu. Báo thay đổi, bằng chứng và hạn chế còn lại.
```

### Tiếp tục công việc

```text
Tiếp tục từ docs/HANDOFF.md trong repo đang mở.
Đối chiếu branch, commit, staged/unstaged/untracked và file thực tế.
Xác minh dữ kiện đã cũ khi cần, giữ các thay đổi có sẵn.
Thực hiện bước tiếp theo trong phạm vi đã được phép đến mốc hoàn thành,
kiểm tra phù hợp và cập nhật bàn giao. Nếu bước tiếp theo còn chờ
duyệt theo yêu cầu hiện tại, chuẩn bị kết quả cụ thể để tôi duyệt.
```

### Bàn giao

```text
Cập nhật docs/HANDOFF.md với trạng thái code hiện tại, việc đã
xác minh, quyết định còn hiệu lực, vấn đề mở và hành động tiếp theo.
Ghi nguồn kiểm tra và trạng thái code khi kiểm tra; giữ bàn giao ngắn.
Không đưa toàn bộ log hoặc lịch sử chat vào file.
```

Nếu repo dùng đường dẫn bàn giao khác, thay đường dẫn trong ba mẫu. Mẫu tiếp tục cho phép thực hiện công việc đã được giao; không tự cấp quyền publish hoặc vượt điều kiện phê duyệt của dự án.

## 3. Nguồn nào dùng cho việc gì?

| Nguồn | Vai trò |
|---|---|
| Yêu cầu/đặc tả được chốt | Kết quả phải đạt |
| AGENTS và contract áp dụng | Quy tắc và ràng buộc khi làm |
| Source/config/Git hiện tại | Trạng thái đang tồn tại |
| Test và kết quả chạy | Bằng chứng cho những trường hợp đã kiểm tra |
| HANDOFF | Ghi trạng thái và giúp tìm đúng nguồn |
| Skill | Quy trình/kiến thức tái sử dụng phù hợp với nhiệm vụ |

Khi có mâu thuẫn, Codex nêu khác biệt và đọc nguồn cần thiết để giải quyết. Một file ghi CURRENT hoặc một nhận xét PASS không tự chứng minh đúng.

Việc nhỏ không phải đọc toàn bộ kế hoạch hoặc mọi contract. Khi **tiếp tục** công việc, đọc handoff rồi kiểm Git/source; với task mới, đọc hướng dẫn và phần contract liên quan. Tìm ký hiệu/phạm vi bằng `rg` có thể rộng; chỉ nạp nội dung cần cho quyết định. Giữ nguyên sự khác nhau giữa tìm kiếm toàn repo và đọc mọi file.

## 4. Kiểm tra của dự án

Repo yêu cầu Python `>=3.12,<3.13` theo `pyproject.toml`; README yêu cầu Python 3.12 x64 cho source. `requirements-release.txt` ghim PySide6/PyInstaller; CI ghim Ruff `0.16.9`, mypy `2.3.1`. Tách lệnh **xác nhận từ cấu hình** với kết quả thực thi. Bảng dưới ghi theo mốc code `99ff244` và [PR #5](https://github.com/NextGlobal224/creator-loop/pull/5) lúc thiết lập A2 ngày 01/10/2026; kiểm lại khi code đổi.

| Mục | Lệnh/cách kiểm tra đã xác nhận | Nguồn | Tình trạng |
|---|---|---|---|
| Setup source/UI | Python 3.12 x64; `python -m pip install PySide6==6.10.2` khi cần UI. | `README.md`, `pyproject.toml` | Đã xác nhận cấu hình; **chưa chạy trong A2**. |
| Format/lint | `ruff check app tests scripts`; `ruff format --check app tests scripts` | `.github/workflows/ci.yml` | CI `fast-schema-domain` **SUCCESS** trên PR head `99ff244`; chưa chạy local trong A2. |
| Kiểu/biên dịch | `mypy app/creator_loop`; `python -m compileall -q app tests` | `.github/workflows/ci.yml` | CI fast **SUCCESS** trên cùng head; chưa chạy local trong A2. |
| Test hành vi | `python -m unittest discover -s tests -v`. Với local PowerShell, dùng `$env:PYTHONPATH = 'app'` trước lệnh nếu cần import từ source. | `.github/workflows/ci.yml`, `README.md`, handoff cũ | CI fast/Windows **SUCCESS** trên PR head `99ff244`. Local full test cũ áp dụng cho `25ea4e4`, không tự xác minh mốc code `99ff244`; chưa chạy local trong A2. |
| Source smoke | Đặt `PYTHONPATH=app` và `CREATOR_LOOP_DATA_ROOT` ngoài repo rồi chạy `python -m creator_loop --smoke`; PowerShell mẫu trong README. | `README.md`, `.github/workflows/ci.yml` | CI fast **SUCCESS** trên cùng head; chưa chạy local trong A2. |
| Security | `python scripts/check_workflow_policy.py`; CI còn chạy `pip-audit -r requirements-release.txt` và Gitleaks trên toàn Git history. | `.github/workflows/ci.yml` | Job `security-dependencies-workflow` **SUCCESS** trên cùng head; chưa chạy local trong A2. |
| Windows artifact | CI cài `requirements-release.txt`, chạy `python -m PyInstaller --noconfirm --clean --onedir --name CreatorLoop --paths app --add-data "migrations/0001_initial.sql;migrations" packaging/entrypoint.py`, nén ZIP, giải nén vào đường dẫn Unicode, smoke file EXE và so SHA-256 ZIP. | `.github/workflows/ci.yml`, `packaging/entrypoint.py` | Job `windows-artifact` **SUCCESS** trên cùng head; không phải test máy đích 8 GB. |
| Release/required CI | Tag `v*` từ commit trên `main` mới vào release job; branch protection hoặc ruleset phải bắt buộc các check PR. | `.github/workflows/ci.yml`, CI/CD contract, README; GitHub branch protection API ngày 01/10/2026 | `main` yêu cầu ba check PR với `strict=true`; PR #5 head `99ff244` có ba job SUCCESS, `publish-release` SKIPPED đúng điều kiện. Chưa chạy release gate. |

Ba job PR có bằng chứng ở [run 36884109471](https://github.com/NextGlobal224/creator-loop/actions/runs/36884109471), đối chiếu bằng `gh pr view 5 --repo NextGlobal224/creator-loop --json headRefOid,statusCheckRollup`. Kết quả CI thuộc commit đó; không tự áp dụng cho code sửa sau này. Kết quả local cũ và ca symlink bị skip được ghi trong [HANDOFF.md](HANDOFF.md).

- PASS: đã chạy và đạt trong phạm vi nêu rõ.
- FAIL: đã chạy và có lỗi; ghi lỗi liên quan và bước xử lý.
- SKIP: không thực hiện một ca; ghi lý do và ảnh hưởng.
- Chưa chạy: chưa có bằng chứng thực thi. Có thể đồng thời đã xác nhận lệnh từ cấu hình.

Kiểm tra phù hợp với thay đổi và yêu cầu repo. Mở rộng khi ảnh hưởng chưa rõ hoặc có lỗi. Sửa tài liệu thuần túy thường cần đọc diff và đường dẫn; vẫn làm kiểm tra bổ sung nếu repo yêu cầu hoặc tài liệu tác động đến công cụ.

## 5. Giữ phiên gọn

Gõ `/` trong ô chat để kiểm tra lệnh có trong phiên bản/IDE đang dùng. Lệnh dùng ở ô chat; không dán `/compact` vào PowerShell.

| Môi trường | Trạng thái ghi nhận |
|---|---|
| Phiên bản extension/CLI thực tế | `codex --version` tại A2: `codex-cli 0.155.0-alpha.16.3`. Phiên bản VS Code Codex extension **chưa xác nhận**; `code --list-extensions --show-versions` lỗi quyền truy cập profile, thư mục `codex-audio` không chứng minh phiên bản Codex extension. |
| `/status`, `/compact`, `/fork` trong menu IDE | Chưa quan sát trong các phiên ghi nhận; người dùng kiểm tra menu `/` |

| Tình huống | Cách làm |
|---|---|
| Phiên còn tập trung và đủ context | Tiếp tục |
| Phiên dài nhưng cùng mục tiêu | Cập nhật handoff nếu cần, dùng `/compact` nếu menu hỗ trợ |
| Khám phá hướng khác và cần lịch sử kế thừa | Dùng `/fork` nếu hỗ trợ; Git worktree riêng nếu sẽ sửa code song song |
| Mục tiêu ít liên quan | Chat mới, chỉ đưa nguồn liên quan |
| Xem context và giới hạn hiển thị | `/status` nếu hỗ trợ; ghi đúng loại chỉ số |

Prompt tóm tắt tạo nội dung bàn giao; nó không tự thực hiện thao tác compact. Fork kế thừa lịch sử, không phải cơ chế tự làm nhẹ context. Không compact theo lịch cố định chỉ vì đã đến giờ hoặc đủ một số tin nhắn.

## 6. Đổi tài khoản hoặc máy

### Cùng máy và môi trường

1. Gọi mẫu “Bàn giao” trước khi dừng nếu trạng thái chưa được cập nhật.
2. Đảm bảo file đã lưu. Git là mốc phiên bản; commit có ý nghĩa khi bạn muốn giữ một mốc, không phải điều kiện để Codex đọc file local.
3. Đăng xuất/đăng nhập bằng chức năng được môi trường Codex hỗ trợ. Không chuyển token, file xác thực hoặc lấy chat ở tài khoản khác làm nguồn duy nhất.
4. Mở đúng repo và chat mới, dùng mẫu “Tiếp tục công việc”. Cho Codex kiểm tra Git trước khi sửa.
5. Chỉ để một phiên sửa cùng worktree tại một thời điểm; nếu làm song song phải có worktree và phối hợp rõ ràng.

Skill cá nhân đọc từ file trong HOME. Theo cơ chế này, khi chỉ đổi tài khoản đăng nhập nhưng vẫn dùng cùng Windows profile và cùng môi trường Codex, file skill được giữ. Windows, WSL và môi trường remote có thể dùng HOME khác nhau: phải kiểm tra vị trí thực tế.

### Đổi máy hoặc môi trường

Code và tài liệu cần được mang sang bằng repo hoặc bản sao có kiểm soát. Skill user-scope ngoài repo phải được chuyển/cài và kiểm tra riêng. Không giả định chat, connector, memories, cấu hình global hay credential tự đi cùng repo.

Skill cá nhân trên máy bạn từng được xác nhận tại `C:/Users/Admin/.agents/skills/start-project/SKILL.md`; đây là mốc quan sát, cần kiểm tra lại nếu đổi môi trường.

## 7. Chọn model và công cụ

Giữ một cấu hình đủ tốt cho công việc thường ngày. Tăng khả năng suy luận khi lỗi hoặc dependency cần điều tra sâu; giảm mức tốn kém chỉ khi kết quả vẫn đạt. Ghi model/reasoning/chế độ tốc độ trong bảng đo để so sánh.

Không đặt tên model cố định làm yêu cầu cho mọi repo. Lựa chọn và usage phụ thuộc tài khoản/catalog đang có. Nếu thử chế độ tốc độ hoặc model khác, xem mô tả/usage hiện hành trước khi dùng thường xuyên.

Giữ công cụ cần cho task. Chỉ đọc log liên quan; lưu log dài vào vị trí phù hợp của dự án và trỏ đến phần lỗi. Connector chỉ có ích khi truy cập được nguồn thật của nhiệm vụ.

## 8. Bảng nghiệm thu thiết lập

Đây là đánh giá của bộ hướng dẫn; tách khỏi việc nghiệm thu sản phẩm/Gate/CI. Codex cập nhật từng dòng bằng bằng chứng, không đánh dấu cả bảng PASS chỉ vì file tồn tại.

| Tiêu chí | Trạng thái | Bằng chứng hoặc phần còn thiếu |
|---|---|---|
| Đúng repo và hướng dẫn áp dụng | Đạt | Đã đối chiếu repo, branch, mốc code `99ff244`, worktree, `AGENTS.md` và thứ tự bốn contract; lần lưu mốc này kiểm lại Git trước khi commit. |
| AGENTS phù hợp, không có xung đột chưa xử lý | Đạt | Thứ tự contract đúng; task nhỏ đọc và kiểm theo phạm vi, chưa thấy xung đột áp dụng. |
| HANDOFF khớp Git/file, bằng chứng đúng phạm vi | Đạt | Ghi rõ `99ff244` là mốc code trước commit tài liệu; hash commit tài liệu lấy từ Git. Tách CI trên PR head khỏi local test cũ, ruleset và release gate. |
| WORKFLOW có mẫu giao việc và lệnh đúng repo | Đạt | Có mẫu task mới/tiếp tục/bàn giao; lệnh đối chiếu với `README.md`, `pyproject.toml`, requirements và `.github/workflows/ci.yml`. |
| Chat mới nhận hướng dẫn và đọc bàn giao | Đạt | Chat mới ngày 01/10/2026 đọc `AGENTS.md`, `HANDOFF.md`, phần contract liên quan và workflow; đối chiếu branch/HEAD/worktree, CI và PR #5 trước khi sửa. |
| Một việc nhỏ thật được xử lý trực tiếp | Đạt | Sửa hai câu hiện trạng cũ trong `README.md` theo PR #5 head `99ff244` và run 36884109471; đã đọc diff, `git diff --check` PASS cho tracked files, quét riêng khoảng trắng/link của workflow untracked. Không sửa code sản phẩm. |
| Một lần tiếp tục task thật đúng trạng thái | Đạt | Giữ thay đổi A2 và kit untracked; phát hiện ghi chú README cũ, chỉ sửa tài liệu liên quan và giữ mốc code/test đúng phạm vi. |
| Có dữ liệu đánh giá hiệu quả ban đầu | Đạt mốc ghi nhận đầu tiên | Người dùng quan sát giao diện hiển thị “Worked for 4m 10s” cho task README; usage chưa có. Một task chưa đủ để suy ra tỷ lệ tiết kiệm. |
| Lệnh IDE `/status`, `/compact`, `/fork` và phiên bản extension | Chưa thử/xác nhận | Chưa quan sát menu lệnh trong IDE; phiên bản VS Code extension chưa xác nhận. Đây là kiểm tra riêng, không chặn nghiệm thu bộ hướng dẫn. |

**Kết luận:** Bộ hướng dẫn đạt nghiệm thu thiết lập và các hành vi đã thử ở trên. Required checks/ruleset và release gate thuộc nghiệm thu sản phẩm riêng; Gate 2 chưa hoàn tất. Skill dự án mới và tích hợp ngoài có nghiệm thu riêng. Các mục IDE chưa thử giữ nguyên trạng thái chưa thử.

## 9. Đo hiệu quả trên công việc thật

Bản ghi đầu tiên là mốc bắt đầu quan sát. Chỉ so sánh công việc có độ phức tạp tương đương, giữ các điều kiện tương đối ổn định. Không yêu cầu chạy lại hàng loạt task để lấy số.

| Ngày/task | Mục tiêu và mức độ khó | Model/reasoning/tốc độ | Thời gian thực tế | Lượt sửa do hiểu sai/bỏ sót | Kết quả và bằng chứng | Usage và nguồn, nếu có | Công duy trì tài liệu |
|---|---|---|---|---|---|---|---|
| 01/10/2026 — kiểm tra ghi chú runner trong README | Task tài liệu nhỏ: đối chiếu CI và PR #5, sửa hai câu hiện trạng cũ, cập nhật bàn giao/nghiệm thu | Model/reasoning/tốc độ trong IDE: thiếu quan sát | 4m 10s — giao diện hiển thị “Worked for 4m 10s”, do người dùng quan sát và cung cấp | 0 lượt sửa do người dùng báo lỗi trong task này tại lúc ghi | PR #5 head `99ff244`; ba job PR `SUCCESS`, release `SKIPPED`; README, HANDOFF và bảng này được cập nhật; `git diff --check` PASS, quét link/khoảng trắng không thấy lỗi | Usage theo task/phiên: chưa có | 3 file tài liệu liên quan được cập nhật; thiếu số đo thời gian duy trì |

Lượt sửa do yêu cầu người dùng đổi không tính như lỗi bỏ sót yêu cầu cũ. Nếu không có usage theo task, ghi chỉ số phiên và hạn chế của nó; không tự phân bổ quota cho từng task. Context còn lại không phải quota còn lại.

Sau vài công việc tương đương, xem: kết quả đúng hơn không, ít làm lại hơn không, thời gian và công duy trì thế nào. Chỉ công bố tỷ lệ tiết kiệm khi dữ liệu thực tế đủ để tính và nêu điều kiện so sánh. Bỏ quy tắc làm chậm mà không cải thiện chất lượng hoặc khả năng tiếp tục.

## 10. Tái sử dụng và cải tiến

- Repo giữ phiên bản của code và tài liệu. Handoff là trạng thái hiện hành; history truy xuất khi cần.
- Skill cá nhân ngoài repo cần bản lưu riêng nếu muốn chuyển máy/rollback. Không tạo nhiều bản skill cùng tên trong vùng được khám phá.
- Chỉ tạo candidate khi có phát hiện dùng lại được và bằng chứng. Kiểm tra hành vi mới, hành vi cũ cần giữ và phạm vi kích hoạt trước khi thay bản hoạt động.
- Thêm DECISIONS khi lý do quyết định cần giữ riêng; thêm INDEX khi nhiều tài liệu khiến việc tìm nguồn khó. Không bắt mọi task đi qua INDEX.
- Multi-skill router không phải bước cài bắt buộc. Chỉ thêm khi chọn/ghép workflow thành vấn đề thực tế; dùng số skill tối thiểu đủ làm việc.

Hướng dẫn chi tiết cho kiểm tra/cập nhật skill và tích hợp dịch vụ nằm trong bộ `Codex-Kit-v2`. Chỉ mở phần tương ứng khi có nhu cầu.

## Liên kết sản phẩm do bộ Codex-Kit-v2 cung cấp

- [Hướng dẫn AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [Skill và vị trí đọc skill local](https://learn.chatgpt.com/docs/build-skills)
- [Lệnh trong Codex IDE](https://learn.chatgpt.com/docs/developer-commands?surface=ide)
- [Usage và các lựa chọn hiện hành](https://learn.chatgpt.com/docs/pricing)

Ba file nền tảng, cách giao việc và bảng đo là lựa chọn thiết kế của bộ này; không phải yêu cầu bắt buộc hoặc cam kết tiết kiệm của OpenAI.
Các liên kết trên được chuyển từ bộ nguồn; nội dung trang và menu `/` chưa được kiểm lại trong phiên A2.
