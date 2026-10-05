# Gate trước khi publish V1

Theo [CI/CD contract](Creator_Loop_CICD_Release_Contract_V1.md#4-release-gate),
required CI, tag từ `main`, nghiệm thu máy đích và đúng tested bytes là điều kiện
phát hành. Guard `scripts/check_release_gate.py` chạy sau download/checksum và
trước `gh release create`; PR không được cấp quyền phát hành.

Guard từ chối manifest development/unresolved, mismatch tag/version/commit/run,
thiếu guide, ZIP/member digest hoặc inventory khác manifest. Provenance phải
thuộc chính Windows CI run đang publish. `component_compatibility.status` phải
là `VERIFIED_V1`, với engine/model name, version, SHA256, source, license và
`local_use_allowed=true` lấy từ kết quả đã xác minh; không nhập giá trị mẫu để
vượt gate. V1 dùng engine/model cục bộ, không đưa bytes hoặc đường dẫn cá nhân
của chúng vào release/Git.

Các dòng1–23 trong ma trận Gate của [PRODUCT_ACCEPTANCE](PRODUCT_ACCEPTANCE.md)
phải có trạng thái `ĐÃ NGHIỆM THU`, evidence và cột còn thiếu trống/`—`. Chỉ cập
nhật trạng thái sau khi xem bằng chứng đúng commit/artifact/phạm vi, gồm máy8GB
và engine/model thật. Mục24 chứa hành động publish nên chưa cần đánh dấu đã
nghiệm thu trước chính hành động đó; artifact phải có đầy đủ21offline guides,
launcher và build-info đúng commit/target. Sau publish, cập nhật evidence của
mục24 bằng tag/release/CI/tested bytes thực tế.

Đây là kiểm tra điều kiện và binding, **không thực hiện hoặc chứng minh thay**
real-machine tests, walkthrough, license verification hay independent review.
JSON/checkbox/PASS của guard không thay nghiệm thu hành vi. Vẫn phải giữ required
checks và các điều kiện release của contract; signatures/attestation chưa bật.

Hiện `write_release_manifest.py` chỉ phát sinh development checkpoint với
engine/model unresolved và release gate chưa hoàn tất; tag publish sẽ bị chặn
có chủ đích. Không đổi metadata thành final chỉ để qua CI. Luồng tạo final
manifest/compatibility từ bằng chứng real-machine còn phải hoàn thiện sau khi
có đầu vào engine/model và nghiệm thu tương ứng. Bản chạy development và các
proof hiện hành tra [HANDOFF](HANDOFF.md); không chọn/tải model tự động.
