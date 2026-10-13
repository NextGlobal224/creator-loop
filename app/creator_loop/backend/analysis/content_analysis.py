"""Small local editorial analysis, with explicit scope and exact citations.

No model or network is loaded here. This backend describes processed text;
caption text does not stand in for unprocessed pixels, audio or a whole video.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from creator_loop.shared.workflow import ResearchMaterial

BACKEND_NAME = "LOCAL_STRUCTURED_RULES"
BACKEND_VERSION = "1.0.0"


@dataclass(frozen=True)
class ContentAnalysis:
    material_id: str
    scope: str
    hook: str
    structure: str
    message: str
    data_cues: tuple[str, ...]
    evidence_version_ids: tuple[str, ...]
    limitation: str


def analyze_material(material: ResearchMaterial) -> ContentAnalysis:
    text = material.text.strip()
    sentences = [
        p.strip() for p in re.split(r"(?<!\d\.)(?<=[.!?])\s+|\n+", text) if p.strip()
    ]
    hook = sentences[0][:220] if sentences else "Chưa rõ — chưa có text đã xử lý"
    counts = [f"{len(sentences)} đoạn/câu trong phần được xử lý"]
    if re.search(r"(?m)^\s*(?:[-•]|\d+[.)])", text):
        counts.append("có liệt kê")
    if "?" in text:
        counts.append("có câu hỏi")
    if re.search(r"\b(?:hãy|chia sẻ|bình luận|bạn nghĩ)\b", text, re.IGNORECASE):
        counts.append("có lời mời hành động trong text")
    data_cues = tuple(
        p[:220]
        for p in sentences
        if re.search(r"\d|nghiên cứu|số liệu|theo báo cáo", p, re.IGNORECASE)
    )[:5]
    limits = {
        "URL_ONLY": "Chỉ có Source/URL: chưa phân tích nội dung; không tạo Evidence từ URL.",
        "CAPTION_ONLY": "Chỉ phân tích caption đã lưu; chưa xử lý toàn ảnh/video hoặc nguyên nhân tạo tương tác.",
        "PROCESSED_IMAGE_EVIDENCE": "Chỉ các vùng ảnh/OCR có Evidence đã xử lý và neo đúng file; không mô tả toàn ảnh.",
        "PROCESSED_VIDEO_EVIDENCE": "Chỉ các đoạn transcript/hình/âm thanh có Evidence và mốc locator đã xử lý; không phân tích toàn video.",
    }
    limitation = limits.get(
        material.scope,
        "Chỉ phần text đã lưu/định vị; dữ kiện và suy luận chưa được người dùng kiểm chứng.",
    )
    return ContentAnalysis(
        material.material_id,
        material.scope,
        hook,
        "; ".join(counts),
        "Quan sát: nội dung gồm các câu/đoạn đã trích dẫn. Suy luận editorial cần review; không xác nhận dữ kiện trong nguồn là đúng.",
        data_cues,
        material.evidence_version_ids,
        limitation,
    )


def recommend_direction(purpose: str, audience: str, style: str) -> str:
    return f"Giúp {audience or 'người đọc'} kiểm chứng {purpose}: tách quan sát, nhận định và điều chưa rõ; phong cách {style or 'rõ ràng, thận trọng'}."


def draft_body(
    *,
    direction: str,
    purpose: str,
    audience: str,
    style: str,
    output_format: str,
    citations: tuple[str, ...],
) -> str:
    """Original editorial scaffolding; no copied caption or factual invention."""
    lead = "Điều ta tin và điều ta đã kiểm chứng có thể khác nhau."
    narration = "Trước một thông tin, hãy hỏi ba câu: Ta thực sự quan sát được gì? Nhận định dựa trên nguồn nào? Điều gì vẫn chưa rõ? Tách dữ kiện khỏi suy luận giúp cuộc trao đổi cẩn trọng hơn."
    context = f"Mục đích: {purpose}\nKhán giả: {audience or 'Chưa rõ'}\nPhong cách: {style or 'rõ ràng, thận trọng'}\nHướng: {direction}"
    refs = "Nguồn nghiên cứu — exact Claim Versions (chưa duyệt): " + (
        ", ".join(citations) or "Chưa rõ"
    )
    if output_format == "VIDEO":
        body = f"KỊCH BẢN VIDEO NHÁP\n{context}\n\nLỜI ĐỌC\n{lead}\n{narration}\n\nKẾ HOẠCH CẢNH\n1. Tiêu đề: Điều ta tin / điều đã kiểm chứng — 4 giây\n2. Chữ: Ta quan sát được gì? — 4 giây\n3. Chữ: Nguồn nào? Điều gì chưa rõ? — 4 giây\n4. Chữ: Tách dữ kiện khỏi suy luận — 4 giây"
    elif output_format == "IMAGE_CAPTION":
        body = f"CAPTION NHÁP\n{lead}\n{narration}\n\nBRIEF HÌNH\nBa ô chữ tiếng Việt: Quan sát / Nguồn / Chưa rõ. Tạo hình minh họa riêng; chưa cấp quyền sử dụng media nguồn.\n\n{context}"
    else:
        body = f"{lead}\n\n{narration}\n\nBạn sẽ kiểm tra nguồn nào trước khi chia sẻ?\n\n{context}"
    return (
        body
        + "\n\n"
        + refs
        + "\nBản nháp editorial theo mẫu local, chờ Creator review; không cam kết viral."
    )
