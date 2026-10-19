// Lost acknowledgments retain the original payload. Only reads reconcile them;
// starting another inference always requires a new explicit submit.
export const storageKey='creator-pending-request-v2';
export function preparePending(data,existing,newId){
  if(existing){
    if(JSON.stringify(existing.data)!==JSON.stringify(data))throw Error('Còn yêu cầu chưa rõ kết quả. Mở trạng thái hoặc bỏ yêu cầu chờ trước khi đổi nội dung.');
    return existing;
  }
  return {key:newId(),data};
}
export function reconcilePending(status,job){
  if(status===200&&job?.id)return {clear:true,job};
  if(status===404)return {clear:false,absent:true};
  throw Error('Chưa xác minh được yêu cầu đang chờ; không tự gửi lại.');
}
export function savedSelection(workflow,versionId){
  const saved=workflow.versions||[];
  const latest=saved.reduce((best,v)=>!best||v.version_no>best.version_no?v:best,null);
  if(versionId==='proposal')return null;
  return saved.find(v=>v.draft_version_id===versionId)||latest;
}
export function changedDraftText(workflow,selected,editor){
  const original=selected||{body_text:workflow.llm_proposal?.draft_body||'',direction:workflow.direction_text,image_file_id:null,image_provenance:'',image_usage_note:''};
  return editor.body_text!==original.body_text||editor.direction!==original.direction||
    workflow.output_format==='IMAGE_CAPTION'&&['image_file_id','image_provenance','image_usage_note'].some(k=>(editor[k]||'')!==(original[k]||''));
}

export function formProblem(data){
 if(!data.project_id)return {field:'project',message:'Bước 1: tạo hoặc chọn một Project.'};
 if(!data.material_ids?.length)return {field:'materials',message:'Bước 2: tích ô bên cạnh tư liệu cần dùng. Nếu chưa có, mở Nhập hoặc tải tư liệu; chọn ảnh caption ở mục Ảnh của bản nháp.'};
 if(data.material_ids.length>8)return {field:'materials',message:'Chỉ chọn từ 1 đến 8 tư liệu cho một lượt.'};
 if(!data.purpose?.trim())return {field:'purpose',message:'Bước 3: nhập mục đích của bài, ví dụ giải thích điều gì cho người đọc.'};
 if(data.mode==='MANUAL'&&!data.direction?.trim())return {field:'direction',message:'Bước 3: nhập hướng nội dung bạn chọn ở chế độ Thủ công.'};
 if(data.output_format==='IMAGE_CAPTION'&&(!data.draft_choices?.image_file_id?.trim()||!data.draft_choices.image_provenance?.trim()||!data.draft_choices.image_usage_note?.trim()))return {field:'draft-image',message:'Chọn ảnh đã tải, ghi nguồn ảnh và căn cứ quyền sử dụng trong mục Ảnh của bản nháp. URL ảnh không thay cho tệp ảnh.'};
 return null;
}
