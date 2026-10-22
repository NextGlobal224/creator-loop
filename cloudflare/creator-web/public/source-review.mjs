// Source choice is separate from factual ACCEPT and publication Approval.
export function codePointRange(text,start,end){
 if(!Number.isInteger(start)||!Number.isInteger(end)||start<0||end<=start||end>text.length)throw Error('Bôi chọn một đoạn nguyên văn trong ô nguồn.');
 for(const offset of [start,end])if(offset>0&&offset<text.length&&/[\uD800-\uDBFF]/.test(text[offset-1])&&/[\uDC00-\uDFFF]/.test(text[offset]))throw Error('Không cắt giữa một ký tự Unicode.');
 return {start:Array.from(text.slice(0,start)).length,end:Array.from(text.slice(0,end)).length};
}
export function currentEvidence(versions){
 const latest=new Map();for(const v of versions)if((latest.get(v.evidence_id)?.version_no??0)<v.version_no)latest.set(v.evidence_id,v);
 return [...latest.values()];
}
export function mountSourceReview({api,getProject,afterChange,say,root=document}){
 const $=s=>root.querySelector(s);let selected=null,seq=0;
 const live=s=>s&&selected===s&&getProject()===s.project;
 function clear(){seq++;selected=null;$('#source-review-panel').hidden=true;$('#source-text').value='';$('#source-evidence').replaceChildren();$('#source-confirm').checked=false;}
 function render(data){
  const s=selected;s.data=data;$('#source-review-panel').hidden=false;$('#source-confirm').checked=false;
  $('#source-text').value=data.text??'';$('#source-text').hidden=data.text===null;$('#source-range').disabled=data.text===null;
  $('#source-edit-version').replaceChildren(new Option('Tạo đoạn mới',''));
  $('#source-evidence').replaceChildren();const chosen=new Set(data.source_use?.evidence_version_ids??[]);
  for(const v of currentEvidence(data.evidence_versions)){
   const label=document.createElement('label'),box=document.createElement('input'),text=document.createElement('span');box.type='checkbox';box.value=v.evidence_version_id;box.checked=chosen.has(v.evidence_version_id);box.name='source-evidence';
   text.textContent=`v${v.version_no} · ${v.review_state} · ${v.locator_type} ${v.locator_data}\n${v.content}`;label.append(box,text);$('#source-evidence').append(label);
   if(v.locator_type==='TEXT_RANGE'){
    const o=new Option(`Sửa phạm vi v${v.version_no} · ${v.content.slice(0,60)}`,v.evidence_version_id);$('#source-edit-version').append(o);
    const open=document.createElement('button');open.type='button';open.className='secondary';open.textContent='Đối chiếu đoạn nguyên văn';open.onclick=()=>{if(!live(s))return;const loc=JSON.parse(v.locator_data),chars=Array.from(data.text);const start=chars.slice(0,loc.start).join('').length,end=chars.slice(0,loc.end).join('').length;$('#source-text').focus();$('#source-text').setSelectionRange(start,end);};$('#source-evidence').append(open);
   }
  }
  $('#source-use-status').textContent=data.source_use?`Lựa chọn nguồn v${data.source_use.version_no}: ${data.source_use.state}. Đây không phải duyệt factual hoặc duyệt đăng.`:'Chưa xác nhận đoạn được dùng cho Draft. Bản gốc và các phiên bản luôn được giữ.';
  if(data.text===null)$('#source-use-status').textContent+=' Tư liệu chưa có text gốc để chọn đoạn; trích xuất media vẫn đang hoàn thiện, không tự suy nội dung từ tên tệp.';
 }
 async function open(file){
  const project=getProject(),ticket=++seq;$('#source-review-panel').hidden=true;selected={project,file,data:null};const s=selected;
  try{const data=await api('/api/source-review?project_id='+encodeURIComponent(project)+'&file_id='+encodeURIComponent(file));if(ticket!==seq||!live(s))return;render(data);$('#source-review-panel').scrollIntoView({block:'nearest'});}catch(e){if(live(s))say(e.message);}
 }
 $('#source-range').onclick=async()=>{
  const s=selected;if(!live(s)||!s.data||s.data.text===null)return;
  const button=$('#source-range');button.disabled=true;
  try{const range=codePointRange(s.data.text,$('#source-text').selectionStart,$('#source-text').selectionEnd),edit=$('#source-edit-version').value;
   await api('/api/source-ranges',{project_id:s.project,file_id:s.file,...range,...(edit?{evidence_version_id:edit,reason:$('#source-reason').value.trim()||'Sửa phạm vi nguyên văn'}:{})});
   if(!live(s))return;await afterChange(s.project);if(getProject()===s.project){await open(s.file);say('Đã lưu phiên bản đoạn nguồn; chưa xác nhận dùng cho Draft.');}
  }catch(e){if(live(s))say(e.message);}finally{if(live(s))button.disabled=false;}
 };
 for(const [selector,state] of [['#source-allow','SELECTED'],['#source-exclude','EXCLUDED'],['#source-reopen','REOPENED']])$(selector).onclick=async()=>{
  const s=selected;if(!live(s)||!s.data)return;
  if(!$('#source-confirm').checked){say('Đánh dấu xác nhận lựa chọn nguồn trước khi lưu; chưa gửi thao tác.');return;}
  const ids=state==='SELECTED'?[...$('#source-evidence').querySelectorAll('input:checked')].map(e=>e.value):[];
  if(state==='SELECTED'&&!ids.length){say('Tạo/chọn ít nhất một đoạn nguồn trước khi xác nhận dùng cho Draft.');return;}
  const button=$(selector);button.disabled=true;
  try{await api('/api/source-use',{project_id:s.project,file_id:s.file,state,evidence_version_ids:ids,expected_parent:s.data.source_use?.use_version_id??null,explicit_source_use_choice:true,reason:$('#source-reason').value});
   if(!live(s))return;await afterChange(s.project);if(getProject()===s.project){await open(s.file);say('Đã lưu lựa chọn nguồn. Không tạo Evidence ACCEPT hoặc phê duyệt đăng.');}
  }catch(e){if(live(s))say(e.message);}finally{button.disabled=false;}
 };
 return {open,clear};
}
