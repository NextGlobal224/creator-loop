// Source choice is separate from factual ACCEPT and publication Approval.
export function codePointRange(text,start,end){
 if(!Number.isInteger(start)||!Number.isInteger(end)||start<0||end<=start||end>text.length)throw Error('Bôi chọn một đoạn nguyên văn trong ô nguồn.');
 for(const offset of [start,end])if(offset>0&&offset<text.length&&/[\uD800-\uDBFF]/.test(text[offset-1])&&/[\uDC00-\uDFFF]/.test(text[offset]))throw Error('Không cắt giữa một ký tự Unicode.');
 return {start:Array.from(text.slice(0,start)).length,end:Array.from(text.slice(0,end)).length};
}
export function currentEvidence(versions){
 const latest=new Map();for(const v of versions)if((latest.get(v.evidence_id)?.version_no??0)<v.version_no)latest.set(v.evidence_id,v);
 return [...latest.values()].sort((a,b)=>{
  if(a.locator_type!==b.locator_type||!['TIME_RANGE','TEXT_RANGE'].includes(a.locator_type))return 0;
  try{const x=JSON.parse(a.locator_data),y=JSON.parse(b.locator_data),start=a.locator_type==='TIME_RANGE'?'start_ms':'start';return Number.isFinite(x[start])&&Number.isFinite(y[start])?x[start]-y[start]:0;}catch{return 0;}
 });
}
export function mountSourceReview({api,getProject,afterChange,say,startSourceJob,root=document}){
 const $=s=>root.querySelector(s);let selected=null,seq=0;
 const live=s=>s&&selected===s&&getProject()===s.project;
 function clear(){seq++;selected=null;$('#source-audio').pause();$('#source-audio').removeAttribute('src');$('#source-original-image').removeAttribute('src');$('#source-review-panel').hidden=true;for(const selector of ['#source-text','#source-model-text','#source-transcript','#source-start-ms','#source-end-ms','#source-image-model','#source-image-observation'])$(selector).value='';$('#source-transcript-warnings').textContent='';$('#source-image-warnings').textContent='';$('#source-evidence').replaceChildren();$('#source-confirm').checked=false;}
 function imageRegion(){if(['x','y','width','height'].some(k=>!$('#source-region-'+k).value.trim()))throw Error('Nhập đủ bốn giá trị của vùng ảnh.');const region=Object.fromEntries(['x','y','width','height'].map(k=>[k,Number($('#source-region-'+k).value)]));if(Object.values(region).some(v=>!Number.isFinite(v))||region.x<0||region.y<0||region.width<=0||region.height<=0||region.x+region.width>1||region.y+region.height>1)throw Error('Vùng ảnh cần số 0–1, rộng/cao dương và không vượt ảnh.');return region;}
 function showRegion(region){for(const [k,v] of Object.entries(region)){$('#source-region-'+k).value=v;$('#source-region-rect').setAttribute(k,String(v*1000));}}
 function render(data){
  const s=selected;s.data=data;$('#source-review-panel').hidden=false;$('#source-confirm').checked=false;
  $('#source-text').value=data.text??'';$('#source-text').hidden=data.text===null;$('#source-range').disabled=data.text===null;
  $('#source-timed').hidden=data.media_type!=='AUDIO';$('#source-audio').pause();
  $('#source-image-panel').hidden=data.media_type!=='IMAGE';$('#source-original-image').removeAttribute('src');$('#source-image-save').disabled=true;$('#source-image-observation').value='';$('#source-read-image').disabled=!startSourceJob||!!data.source_extraction;showRegion({x:0,y:0,width:1,height:1});
  if(data.media_type==='IMAGE')$('#source-original-image').src='/media/'+encodeURIComponent(s.file);
  $('#source-transcribe').disabled=!startSourceJob||!!data.source_extraction;
  $('#source-audio').removeAttribute('src');$('#source-audio-save').disabled=true;
  if(data.media_type==='AUDIO')$('#source-audio').src='/media/'+encodeURIComponent(s.file);
  $('#source-start-ms').value='';$('#source-end-ms').value='';$('#source-transcript').value='';
  $('#source-timed-status').textContent='Hiện nghe/sửa trên Web hỗ trợ WAV PCM hoặc float32 tối đa 2 phút/8 MiB. Trích xuất cần quyền operator đúng model và quota/dự phòng trong gói. Chọn đoạn transcript để đối chiếu; upload không thay đọc nguồn.';
  const parsed=data.source_extraction?.parsed;
  $('#source-image-model').value=data.media_type==='IMAGE'?(parsed?.text??''):'';
  $('#source-image-warnings').textContent=data.media_type==='IMAGE'?(parsed?['Kết quả máy chưa được duyệt. Đối chiếu ảnh/chữ gốc trước khi chọn dùng.',...(parsed.uncertain??[]).map(v=>'Chưa chắc: '+v),...(parsed.unread??[]).map(v=>'Chưa đọc: '+v)].join('\n'):'Chưa có kết quả đọc ảnh. Upload/hiển thị ảnh không thay phân tích nội dung.'):'';
  $('#source-model-text').value=parsed?.text??'';
  const notes={OVERLAPPING_SEGMENT:'Đoạn chồng thời gian',FULL_TEXT_SEGMENT_MISMATCH:'Có nội dung chưa gắn khớp với đoạn thời gian',UNTRANSCRIBED_RANGES:'Có khoảng chưa trích xuất được',MISSING_AVG_LOGPROB:'Thiếu độ tin cậy lời nói',MISSING_NO_SPEECH_PROB:'Chưa có độ tin cậy về lời nói/im lặng',MISSING_COMPRESSION_RATIO:'Chưa có chỉ số kiểm tra lặp',SUSPECT_AVG_LOGPROB:'Lời nói có độ tin cậy thấp',SUSPECT_NO_SPEECH_PROB:'Có thể là khoảng không có lời nói',SUSPECT_COMPRESSION_RATIO:'Nội dung có thể lặp hoặc nhận sai'};
  $('#source-transcript-warnings').textContent=parsed?['Kết quả máy chưa được duyệt; nghe đối chiếu từng đoạn.',...(parsed.warnings??[]).map(w=>notes[w]??'Cần nghe đối chiếu thêm'),...(parsed.untranscribed_ranges??[]).map(([a,b])=>`Chưa đọc được ${a}–${b} ms`),...(parsed.segments??[]).filter(v=>v.warnings?.length).map(v=>`${v.start_ms}–${v.end_ms} ms: ${v.warnings.map(w=>notes[w]??'Cần nghe đối chiếu thêm').join('; ')}`)].join('\n'):'Chưa có kết quả trích lời nói để đối chiếu.';
  $('#source-edit-version').replaceChildren(new Option('Tạo đoạn mới',''));
  $('#source-evidence').replaceChildren();const chosen=new Set(data.source_use?.evidence_version_ids??[]);
  for(const v of currentEvidence(data.evidence_versions)){
   const label=document.createElement('label'),box=document.createElement('input'),text=document.createElement('span');box.type='checkbox';box.value=v.evidence_version_id;box.checked=chosen.has(v.evidence_version_id);box.name='source-evidence';
   text.textContent=`v${v.version_no} · ${v.review_state} · ${v.locator_type} ${v.locator_data}\n${v.content}`;label.append(box,text);$('#source-evidence').append(label);
   if(v.locator_type==='TEXT_RANGE'){
    const o=new Option(`Sửa phạm vi v${v.version_no} · ${v.content.slice(0,60)}`,v.evidence_version_id);$('#source-edit-version').append(o);
    const open=document.createElement('button');open.type='button';open.className='secondary';open.textContent='Đối chiếu đoạn nguyên văn';open.onclick=()=>{if(!live(s))return;const loc=JSON.parse(v.locator_data),chars=Array.from(data.text);const start=chars.slice(0,loc.start).join('').length,end=chars.slice(0,loc.end).join('').length;$('#source-text').focus();$('#source-text').setSelectionRange(start,end);};$('#source-evidence').append(open);
   }
   if(data.media_type==='AUDIO'&&v.locator_type==='TIME_RANGE'){
    const loc=JSON.parse(v.locator_data);
    if(loc.track==='audio'){
     const open=document.createElement('button');open.type='button';open.className='secondary';open.textContent='Nghe/sửa đoạn '+loc.start_ms+'–'+loc.end_ms+' ms';
     open.onclick=()=>{if(!live(s))return;s.audioVersion=v.evidence_version_id;$('#source-start-ms').value=loc.start_ms;$('#source-end-ms').value=loc.end_ms;$('#source-transcript').value=v.content;$('#source-audio').src='/media/'+encodeURIComponent(s.file)+'#t='+loc.start_ms/1000+','+loc.end_ms/1000;$('#source-audio-save').disabled=false;$('#source-timed-status').textContent='Đang sửa v'+v.version_no+'; bản cũ được giữ. Nghe đúng đoạn rồi ghi lý do sửa.';};$('#source-evidence').append(open);
    }
   }
   if(data.media_type==='IMAGE'&&v.locator_type==='IMAGE_REGION'){
    const open=document.createElement('button');open.type='button';open.className='secondary';open.textContent='Xem/sửa vùng ảnh v'+v.version_no;
    open.onclick=()=>{if(!live(s))return;s.imageVersion=v.evidence_version_id;showRegion(JSON.parse(v.locator_data));$('#source-image-observation').value=v.content;$('#source-image-save').disabled=false;$('#source-original-image').scrollIntoView({block:'nearest'});};$('#source-evidence').append(open);
   }
  }
  $('#source-use-status').textContent=data.source_use?`Lựa chọn nguồn v${data.source_use.version_no}: ${data.source_use.state}. Đây không phải duyệt factual hoặc duyệt đăng.`:'Chưa xác nhận đoạn được dùng cho Draft. Bản gốc và các phiên bản luôn được giữ.';
  if(data.text===null)$('#source-use-status').textContent+=data.source_extraction?' Đối chiếu bản gốc và các đoạn máy trích xuất trước khi chọn; nội dung sửa là phiên bản riêng.':' Tư liệu chưa có text gốc/đoạn máy trích xuất để chọn. Dùng bộ đọc đúng loại nếu có; không suy nội dung từ tên tệp.';
 }
 async function open(file){
  const project=getProject(),ticket=++seq;$('#source-audio').pause();$('#source-audio').removeAttribute('src');$('#source-original-image').removeAttribute('src');$('#source-review-panel').hidden=true;selected={project,file,data:null};const s=selected;
  try{const data=await api('/api/source-review?project_id='+encodeURIComponent(project)+'&file_id='+encodeURIComponent(file));if(ticket!==seq||!live(s))return;render(data);$('#source-review-panel').scrollIntoView({block:'nearest'});}catch(e){if(live(s))say(e.message);}
 }
 $('#source-range').onclick=async()=>{
  const s=selected;if(!live(s)||!s.data||s.data.text===null)return;
  const button=$('#source-range');button.disabled=true;
  try{const range=codePointRange(s.data.text,$('#source-text').selectionStart,$('#source-text').selectionEnd),edit=$('#source-edit-version').value;
   await api('/api/source-ranges',{project_id:s.project,file_id:s.file,...range,...(edit?{evidence_version_id:edit,reason:$('#source-reason').value.trim()||'Sửa phạm vi nguyên văn'}:{})});
   if(!live(s))return;const ticket=seq;await afterChange(s.project);if(live(s)||(seq===ticket+1&&selected===null&&getProject()===s.project)){await open(s.file);say('Đã lưu phiên bản đoạn nguồn; chưa xác nhận dùng cho Draft.');}
  }catch(e){if(live(s))say(e.message);}finally{if(live(s))button.disabled=false;}
 };
 $('#source-transcribe').onclick=async()=>{
  const s=selected;if(!live(s)||!startSourceJob)return;
  const button=$('#source-transcribe');button.disabled=true;
  try{await startSourceJob(s.project,s.file,()=>live(s));}catch(e){if(live(s))say(e.message);}finally{if(live(s))button.disabled=false;}
 };
 $('#source-read-image').onclick=async()=>{
  const s=selected;if(!live(s)||!startSourceJob||s.data?.media_type!=='IMAGE')return;
  const button=$('#source-read-image');button.disabled=true;
  try{await startSourceJob(s.project,s.file,()=>live(s),'IMAGE');}catch(e){if(live(s))say(e.message);}finally{if(live(s))button.disabled=!!s.data?.source_extraction;}
 };
 for(const k of ['x','y','width','height'])$('#source-region-'+k).oninput=()=>{try{showRegion(imageRegion());}catch{}};
 let drag=null;
 const imageView=$('#source-image-view');
 function point(event){const rect=$('#source-original-image').getBoundingClientRect();return {x:Math.min(1,Math.max(0,(event.clientX-rect.left)/rect.width)),y:Math.min(1,Math.max(0,(event.clientY-rect.top)/rect.height))};}
 imageView.onpointerdown=event=>{const s=selected,image=$('#source-original-image');if(!live(s)||s.data?.media_type!=='IMAGE'||!image.complete||!image.naturalWidth)return;event.preventDefault();drag={s,start:point(event),id:event.pointerId};imageView.setPointerCapture(event.pointerId);};
 imageView.onpointermove=event=>{if(!drag||event.pointerId!==drag.id||!live(drag.s))return;const p=point(event),x=Math.min(p.x,drag.start.x),y=Math.min(p.y,drag.start.y),width=Math.abs(p.x-drag.start.x),height=Math.abs(p.y-drag.start.y);if(width&&height)showRegion({x,y,width,height});};
 imageView.onpointerup=imageView.onpointercancel=()=>{drag=null;};
 $('#source-image-save').onclick=async()=>{
  const s=selected;if(!live(s)||!s.imageVersion)return;const button=$('#source-image-save');button.disabled=true;
  try{const region=imageRegion(),content=$('#source-image-observation').value,reason=$('#source-reason').value;if(!content.trim()||!reason.trim())throw Error('Nhập nội dung đã đối chiếu và lý do sửa vùng ảnh.');
   await api('/api/source-image-regions',{project_id:s.project,file_id:s.file,evidence_version_id:s.imageVersion,region,content,reason});
   if(!live(s))return;const ticket=seq;await afterChange(s.project);if(live(s)||(seq===ticket+1&&selected===null&&getProject()===s.project)){await open(s.file);say('Đã lưu phiên bản vùng ảnh; cần chọn/xác nhận lại nguồn. Chưa duyệt factual hoặc đăng.');}
  }catch(e){if(live(s))say(e.message);}finally{if(live(s))button.disabled=false;}
 };
 $('#source-audio-save').onclick=async()=>{
  const s=selected;if(!live(s)||!s.audioVersion)return;
  const button=$('#source-audio-save');button.disabled=true;
  try{
   const start=$('#source-start-ms').value,end=$('#source-end-ms').value;
   if(!/^\d+$/.test(start)||!/^\d+$/.test(end)||!Number.isSafeInteger(Number(start))||!Number.isSafeInteger(Number(end))||Number(end)<=Number(start))throw Error('Nhập khoảng thời gian mili giây nguyên, kết thúc sau bắt đầu.');
   if(!$('#source-transcript').value.trim()||!$('#source-reason').value.trim())throw Error('Nhập transcript đã đối chiếu và lý do sửa.');
   await api('/api/source-audio-ranges',{project_id:s.project,file_id:s.file,evidence_version_id:s.audioVersion,start_ms:Number(start),end_ms:Number(end),content:$('#source-transcript').value,reason:$('#source-reason').value});
   if(!live(s))return;const ticket=seq;await afterChange(s.project);if(live(s)||(seq===ticket+1&&selected===null&&getProject()===s.project)){await open(s.file);say('Đã lưu phiên bản transcript; cần chọn/xác nhận lại nguồn, chưa duyệt factual hoặc đăng.');}
  }catch(e){if(live(s))say(e.message);}finally{if(live(s))button.disabled=false;}
 };
 for(const [selector,state] of [['#source-allow','SELECTED'],['#source-exclude','EXCLUDED'],['#source-reopen','REOPENED']])$(selector).onclick=async()=>{
  const s=selected;if(!live(s)||!s.data)return;
  if(!$('#source-confirm').checked){say('Đánh dấu xác nhận lựa chọn nguồn trước khi lưu; chưa gửi thao tác.');return;}
  const ids=state==='SELECTED'?[...$('#source-evidence').querySelectorAll('input:checked')].map(e=>e.value):[];
  if(state==='SELECTED'&&!ids.length){say('Tạo/chọn ít nhất một đoạn nguồn trước khi xác nhận dùng cho Draft.');return;}
  const button=$(selector);button.disabled=true;
  try{await api('/api/source-use',{project_id:s.project,file_id:s.file,state,evidence_version_ids:ids,expected_parent:s.data.source_use?.use_version_id??null,explicit_source_use_choice:true,reason:$('#source-reason').value});
   if(!live(s))return;const ticket=seq;await afterChange(s.project);if(live(s)||(seq===ticket+1&&selected===null&&getProject()===s.project)){await open(s.file);say('Đã lưu lựa chọn nguồn. Không tạo Evidence ACCEPT hoặc phê duyệt đăng.');}
  }catch(e){if(live(s))say(e.message);}finally{button.disabled=false;}
 };
 return {open,clear,refreshIfCurrent:async(project,file)=>{if(selected?.project===project&&selected?.file===file&&getProject()===project)await open(file);}};
}
