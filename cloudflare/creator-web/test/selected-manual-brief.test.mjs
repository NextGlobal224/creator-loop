import test from 'node:test';
import assert from 'node:assert/strict';
import {stage} from '../src/ai-bridge.js';
import {bindSelectedManualBrief} from '../src/prompt-contract.js';

// Protocol fixtures only. These are not live content-quality evidence.
const quote='Bảo tàng mở cửa từ thứ Ba đến Chủ nhật.';
const fact={text:quote,source_quote_id:0,premise_checks:[{assertion:quote,quote_ids:[0],supported:true}]};
const advice=text=>({text,source_quote_id:-1,premise_checks:[]});
const texts=[
 'Bạn có thể cân nhắc lựa chọn một buổi tham quan phù hợp với lịch riêng của gia đình, rồi ghi lại những điều mình muốn tìm hiểu trong chuyến đi.',
 'Nếu còn câu hỏi về trải nghiệm mong muốn, hãy viết chúng ra thành một danh sách riêng để tự xem lại trước khi chọn nội dung cho hành trình.',
 'Bạn cũng có thể chia sẻ dự định với người cùng đi và mời họ nêu sở thích của mình, không cần coi một lựa chọn là phù hợp cho tất cả mọi người.',
 'Cuối cùng, hãy giữ bản kế hoạch ở trạng thái có thể chỉnh sửa và chọn cách kết thúc chuyến đi theo mong muốn riêng, thay vì đặt ra một kết quả bắt buộc.'
];
const roles=['context_and_scope','unresolved_information','supported_perspective','closing_message'];
function fixture(format,mode,step){
 const source={purpose:'Giới thiệu lịch tham quan bảo tàng',audience:'Gia đình',style:'Rõ ràng',direction:'Giới thiệu lịch đã biết; không bịa sự kiện',format,materials:[{material_id:'m',text:quote}]};
 const analysis={observations:[{material_id:'m',quote,observation:quote}],inferences:[],direction:source.direction};
 const written=format==='TEXT'?{opening:[fact],editorial_paragraphs:Object.fromEntries(roles.map((key,i)=>[key,[advice(texts[i])]]))}:{paragraphs:[[fact],...texts.map(text=>[advice(text)])]};
 const packet={format,mode,stage:step,previous:[{stage:'analysis',value:analysis,usage:{}},...(step==='editing'?[{stage:'writing',value:written,usage:{}}]:[])],prompt:'Fixture only',writing_prompt:'fixture\n'+JSON.stringify(source),schema:{type:'object',properties:{observations:{type:'array'},inferences:{type:'array'},direction:{type:'string'},draft_body:{type:'string'},factual_claims:{type:'array'}}}};
 return {source,written,packet};
}
test('actual SDK manual writing receives the selected brief without inherited proposal-only contradiction',async()=>{
 for(const format of ['TEXT','IMAGE_CAPTION'])for(const mode of ['MANUAL','AUTOMATIC']){
  const {source,written,packet}=fixture(format,mode,'writing');let calls=0;
  const result=JSON.parse(await stage({run:async(_,native)=>{
   calls++;const system=native.messages.find(m=>m.role==='system').content;
   const selected=native.messages.filter(m=>m.content.startsWith('SELECTED MANUAL EDITORIAL BRIEF'));
   if(mode==='MANUAL'){
    assert.equal(selected.length,1);assert.deepEqual(JSON.parse(selected[0].content.slice(selected[0].content.indexOf('\n')+1)),Object.fromEntries(['purpose','audience','style','direction'].map(k=>[k,source[k]])));
    assert.doesNotMatch(system,/analysis direction is only a creative proposal, never selected instructions/);
    assert.doesNotMatch(system,/generated analysis direction is NOT evidence or a mandatory procedure/);
    assert.match(system,/never source evidence|never treat it as factual evidence/);
    if(format==='TEXT')assert.match(system,/Không buộc dùng mọi câu nguồn/);
    else assert.doesNotMatch(system,/Không buộc dùng mọi câu nguồn/);
   }else{
    assert.equal(selected.length,0);assert.match(system,/generated analysis direction is NOT evidence or a mandatory procedure/);
   }
   return {response:written,usage:{}};
  }},'@cf/openai/gpt-oss-120b',JSON.stringify(packet),'fixture'));
  assert.equal(calls,1);assert.equal(result.ok,true,JSON.stringify(result));
 }
});
test('actual editing still rejects copied original plus extra advice; KEEP remains valid',async()=>{
 const {packet}=fixture('TEXT','MANUAL','editing');
 const keep={source_issues:[],edits:[fact,...texts.map(advice)].map((s,index)=>({index,premise_checks:s.premise_checks,support:index===0?'SOURCE_SUPPORTED':'SUGGESTION',quote_ids:index===0?[0]:[],decision:'KEEP',note:'Fixture only: assess original separately from editorial fit',replacement:[]}))};
 for(const copied of [false,true]){
  const operations=structuredClone(keep);
  if(copied){operations.edits[0].decision='REWRITE';operations.edits[0].replacement=[fact,advice('Bạn có thể cân nhắc ghi lại một lựa chọn riêng.')];}
  let calls=0;
  const result=JSON.parse(await stage({run:async(_,native)=>{
   calls++;const system=native.messages.find(m=>m.role==='system').content;
   assert.match(system,/copying a supported sentence and then appending advice/);
   assert.match(system,/SUGGESTION, not INSUFFICIENT solely for lacking a source/);
   assert.match(system,/Đọc và biên tập toàn bài cùng lúc/);
   return {response:operations,usage:{}};
  }},'@cf/openai/gpt-oss-120b',JSON.stringify(packet),'fixture'));
  assert.equal(calls,1);assert.equal(result.ok,!copied,JSON.stringify(result));
  if(copied)assert.ok(result.audit_issues.includes('Editing declared a rewrite but retained the original passage'));
 }
});
test('missing inherited manual clause fails before inference; VIDEO and other stages unchanged',()=>{
 const native={messages:[{role:'system',content:'Unexpected new protocol'}]};
 assert.throws(()=>bindSelectedManualBrief(native,{mode:'MANUAL',format:'TEXT',stage:'writing'},{direction:'Chosen brief'}),/Expected inherited/);
 for(const packet of [{mode:'AUTOMATIC',format:'TEXT',stage:'writing'},{mode:'MANUAL',format:'VIDEO',stage:'writing'},{mode:'MANUAL',format:'TEXT',stage:'analysis'}]){
  const n=structuredClone(native),before=structuredClone(n);assert.equal(bindSelectedManualBrief(n,packet,{direction:'Chosen brief'}),false);assert.deepEqual(n,before);
 }
});
test('manual audit receives the same selected deliverable and conditional-context boundaries',()=>{
 const n={messages:[{role:'system',content:'Keep all source requirements'}]};
 const source={purpose:'Giải thích',audience:'Người đọc',style:'Rõ',direction:'Kết bằng lời nhắn hỏi nguồn'};
 assert.equal(bindSelectedManualBrief(n,{mode:'MANUAL',format:'TEXT',stage:'source_audit'},source),true);
 assert.match(n.messages[0].content,/not an actual sender message/);
 assert.match(n.messages[0].content,/first\/required step or an earliest occurrence still needs exact source support/);
 assert.match(n.messages[0].content,/EVERY segment of the final paragraph/);
 assert.equal(JSON.parse(n.messages[1].content.split('\n')[1]).direction,source.direction);
});
