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
