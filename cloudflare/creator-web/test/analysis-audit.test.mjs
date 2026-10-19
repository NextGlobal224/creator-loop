import test from 'node:test';
import assert from 'node:assert/strict';
import {analysisFields,analysisHash,validateAnalysis,validateCoverage,analysisProfile,coverageSchema} from '../src/analysis-audit.js';
const materials=[{material_id:'m',text:'COR giới thiệu ba câu hỏi để xem xét nội dung trực tuyến.'}];
const catalog=[{material_id:'m',source_quote:materials[0].text}];
const analysis={observations:[{material_id:'m',quote:materials[0].text,observation:materials[0].text}],inferences:['Giả thuyết chưa xác nhận: Bạn có thể cân nhắc hỏi lại người gửi.'],direction:'Đề xuất tự viết: hỏi đường dẫn gốc, không dự đoán kết quả.'};
function receipt(hash,fields){return {analysis_sha256:hash,checks:fields.map((f,i)=>({field:f.field,verdict:i===0?'SUPPORTED':'EDITORIAL_ONLY',premise_checks:i===0?[{assertion:f.text,quote_ids:[0],supported:true}]:[],reason:'Fixture only, no quality acceptance',issues:[]}))};}
test('immutable analysis audit requires every field/hash/order and no unsupported premise',async()=>{
 const fields=validateAnalysis(analysis,materials),hash=await analysisHash(analysis);assert.equal(fields.length,3);assert.equal(validateCoverage(receipt(hash,fields),hash,fields,catalog).checks.length,3);
 for(const mutate of [r=>r.checks.pop(),r=>r.checks.reverse(),r=>r.analysis_sha256='0'.repeat(64),r=>r.checks[1].issues.push('Unsupported efficacy'),r=>r.checks[2].verdict='UNRESOLVED',r=>r.checks[0].premise_checks[0].supported=false,r=>r.checks[1].premise_checks.push({assertion:'Tools recommended by UNESCO',quote_ids:[],supported:false})]){
  const r=receipt(hash,fields);mutate(r);assert.throws(()=>validateCoverage(r,hash,fields,catalog));
 }
 assert.throws(()=>validateAnalysis({...analysis,inferences:['Giả thuyết: hiệu quả chưa biết']},materials));
 for(const suffix of ['', ' ', '\t'])assert.throws(()=>validateAnalysis({...analysis,inferences:['Giả thuyết chưa xác nhận:'+suffix]},materials));
 assert.doesNotThrow(()=>validateAnalysis({...analysis,inferences:[]},materials));
 assert.throws(()=>validateAnalysis({...analysis,observations:[{...analysis.observations[0],quote:'not in original'}]},materials));
});
test('live-failure boundaries are explicit in each Web stage without repairing model JSON',async()=>{
 for(const stage of ['analysis','writing','editing']){
  const packet={stage,previous:stage==='analysis'?[]:[{value:analysis}],writing_prompt:'fixture\n'+JSON.stringify({materials})},profile=await analysisProfile(packet);let captured;
  let calls=0;const result={response:analysis};assert.equal(await profile.run({run:async(_,input)=>{calls++;captured=input;return result;}},'model',{reasoning_effort:'low',max_tokens:2600,messages:[{role:'system',content:'Original constraints'}],response_format:{type:'json_schema',json_schema:{properties:{inferences:{}},required:[]}}}),result);
  assert.equal(calls,1);assert.equal(captured.max_tokens,2600);assert.equal(captured.reasoning_effort,stage==='analysis'?'medium':'low');
  if(stage==='analysis'){
   const rule=captured.response_format.json_schema.properties.inferences.items;
   assert.equal(new RegExp(rule.pattern).test('Giả thuyết chưa xác nhận:'),false);assert.equal(new RegExp(rule.pattern).test(analysis.inferences[0]),true);assert.match(captured.messages[0].content,/inferences:\[\]/);
  }else if(stage==='writing')assert.match(captured.messages[0].content,/direction is NOT evidence/);
  else assert.match(captured.messages[0].content,/never emit an identical REWRITE/);
 }
});
test('analysis correction preserves factual no-fiction Projects and explicit manual direction',async()=>{
 for(const project of [{purpose:'Giới thiệu lịch mở cửa bảo tàng',direction:'Chỉ dùng dữ kiện đã có, không tạo tình huống giả định.'},{purpose:'Giúp người đọc nhận thức đúng về sự thật trước khi chia sẻ ảnh chụp màn hình',direction:''}]){
  const profile=await analysisProfile({stage:'analysis',previous:[],writing_prompt:'fixture\n'+JSON.stringify({...project,materials})});let captured;
  await profile.run({run:async(_,input)=>{captured=input;return {response:analysis};}},'model',{messages:[{role:'system',content:'Respect explicit Project'}],response_format:{type:'json_schema',json_schema:{properties:{inferences:{}},required:[]}}});
  assert.match(captured.messages[0].content,/including factual\/no-fiction briefs/);
  assert.equal(captured.messages[0].content.includes('For this specifically commissioned screenshot-sharing Project'),!project.direction);
 }
});
test('actual failed direction and unsupported observation are audited as immutable fields without rewriting',async()=>{
 const bad={...analysis,direction:'Sử dụng công cụ kiểm tra dữ kiện được UNESCO đề xuất để tìm bằng chứng hỗ trợ hoặc phản bác.'};
 const fields=analysisFields(bad),hash=await analysisHash(bad),r=receipt(hash,fields);r.checks[2]={field:'direction',verdict:'UNRESOLVED',premise_checks:[{assertion:bad.direction,quote_ids:[],supported:false}],reason:'Note does not provide this institutional recommendation',issues:['Unsupported attribution']};assert.throws(()=>validateCoverage(r,hash,fields,catalog));assert.equal(fields[2].text,bad.direction);
 const wrong={...analysis,observations:[{...analysis.observations[0],observation:'COR bảo đảm câu hỏi xác định sự thật.'}]};const wf=analysisFields(wrong),wh=await analysisHash(wrong),wr=receipt(wh,wf);wr.checks[0].issues=['Exact quote does not support expanded observation'];assert.throws(()=>validateCoverage(wr,wh,wf,catalog));
});
test('fourth call binds separate receipt and preserves existing article JSON',async()=>{
 const packet={stage:'source_audit',previous:[{value:analysis}],writing_prompt:'fixture\n'+JSON.stringify({materials})};const profile=await analysisProfile(packet);const fields=analysisFields(analysis),hash=await analysisHash(analysis),article={article_sha256:'fixture',segments:[]};let input;
 const result=await profile.run({run:async(_,value)=>{input=value;return {response:{...article,analysis_audit:receipt(hash,fields)},usage:{neurons:1}};}},'model',{messages:[{role:'system',content:'Original article audit'}],response_format:{type:'json_schema',json_schema:{type:'object',properties:{},required:[]}}});assert.deepEqual(result.response,article);assert.equal(profile.receipt().analysis_sha256,hash);assert.equal(input.response_format.json_schema.properties.analysis_audit.properties.checks.minItems,3);
});
test('native choices channel audit preserves metadata and strips only the explicit receipt',async()=>{
 const packet={stage:'source_audit',previous:[{value:analysis}],writing_prompt:'fixture\n'+JSON.stringify({materials})};
 const fields=analysisFields(analysis),hash=await analysisHash(analysis),article={article_sha256:'fixture',segments:[]};
 const profile=await analysisProfile(packet);
 const original={id:'native-fixture',usage:{neurons:1},choices:[{index:0,finish_reason:'stop',message:{role:'assistant',reasoning_content:'PRIVATE REASONING FIXTURE',content:JSON.stringify({...article,analysis_audit:receipt(hash,fields)})}}]};
 const result=await profile.run({run:async()=>original},'model',{messages:[{role:'system',content:'Audit'}],response_format:{type:'json_schema',json_schema:{type:'object',properties:{},required:[]}}});
 assert.deepEqual(JSON.parse(result.choices[0].message.content),article);assert.equal(result.choices[0].message.reasoning_content,original.choices[0].message.reasoning_content);assert.deepEqual(result.usage,original.usage);assert.equal(result.response,undefined);assert.ok(JSON.parse(original.choices[0].message.content).analysis_audit);assert.equal(profile.receipt().analysis_sha256,hash);
});
test('native analysis choices never salvage malformed or incomplete output or weaken grounding',async()=>{
 const packet={stage:'analysis',previous:[],writing_prompt:'fixture\n'+JSON.stringify({materials})};
 const input={messages:[{role:'system',content:'Analysis'}],response_format:{type:'json_schema',json_schema:{type:'object',properties:{inferences:{}},required:[]}}};
 const native=(value,finish_reason='stop')=>({choices:[{finish_reason,message:{content:value}}]});
 const valid=native(JSON.stringify(analysis)),profile=await analysisProfile(packet);
 assert.equal(await profile.run({run:async()=>valid},'model',input),valid);
 assert.deepEqual(profile.diagnostic(),{response_received:true,response_channel:'choices',validation:'PASS',finish_reason:'stop',final_content_utf8_bytes:new TextEncoder().encode(JSON.stringify(analysis)).length,final_json_status:'VALID'});assert.equal(profile.rejectedJSON(),null);
 for(const result of [native('```json\n'+JSON.stringify(analysis)+'\n```'),native(JSON.stringify(analysis),'length'),native(JSON.stringify({...analysis,inferences:['Giả thuyết: chưa đủ nhãn']})),native(JSON.stringify({...analysis,observations:[{...analysis.observations[0],quote:'invented'}]})),{...valid,choices:[...valid.choices,...valid.choices]}])await assert.rejects(()=>profile.run({run:async()=>result},'model',input));
 await assert.rejects(()=>profile.run({run:async()=>({...native({text:JSON.stringify(analysis)}),response:analysis})},'model',input),/Unsupported native choice/);
});
test('rejected profile retains only parsed final JSON, never native reasoning or headers',async()=>{
 const packet={stage:'analysis',previous:[],writing_prompt:'fixture\n'+JSON.stringify({materials})},profile=await analysisProfile(packet);
 const invalid={...analysis,inferences:['Giả thuyết: missing explicit label']};
 const result={headers:{authorization:'PRIVATE_HEADER_FIXTURE'},choices:[{finish_reason:'stop',message:{reasoning_content:'PRIVATE_REASONING_FIXTURE',content:JSON.stringify(invalid)}}]};
 await assert.rejects(()=>profile.run({run:async()=>result},'model',{messages:[{role:'system',content:'Analysis'}],response_format:{type:'json_schema',json_schema:{properties:{inferences:{}}}}}));
 assert.deepEqual(profile.rejectedJSON(),invalid);assert.deepEqual(profile.diagnostic(),{response_received:true,response_channel:'choices',validation:'REJECTED',finish_reason:'stop',final_content_utf8_bytes:new TextEncoder().encode(JSON.stringify(invalid)).length,final_json_status:'VALID'});assert.doesNotMatch(JSON.stringify([profile.diagnostic(),profile.rejectedJSON()]),/PRIVATE_/);
});

test('unconfirmed hypothesis is distinct from a verified fact and cannot exempt direction or unsupported background',async()=>{
 const value={...analysis,inferences:['Giả thuyết chưa xác nhận: Cách kể bằng một cuộc trò chuyện có thể phù hợp với độc giả này.']};
 const fields=analysisFields(value),hash=await analysisHash(value),r=receipt(hash,fields);
 r.checks[1]={field:'inferences.0',verdict:'HYPOTHESIS',truth_status:'UNCONFIRMED',premise_checks:[{assertion:materials[0].text,quote_ids:[0],supported:true}],reason:'Source background only; editorial fit remains untested, no demonstrated effect.',issues:[]};
 assert.equal(validateCoverage(r,hash,fields,catalog).checks[1].truth_status,'UNCONFIRMED');
 for(const mutate of [x=>x.checks[1].truth_status='VERIFIED',x=>delete x.checks[1].truth_status,x=>x.checks[1].premise_checks=[],x=>x.checks[1].premise_checks[0].supported=false,x=>x.checks[1].premise_checks[0].quote_ids=[],x=>x.checks[1].issues.push('Invented tool availability'),x=>Object.assign(x.checks[2],{verdict:'HYPOTHESIS',truth_status:'UNCONFIRMED'}),x=>Object.assign(x.checks[0],{verdict:'HYPOTHESIS',truth_status:'UNCONFIRMED'})]){
  const changed=structuredClone(r);mutate(changed);assert.throws(()=>validateCoverage(changed,hash,fields,catalog));
 }
 const metadataCatalog=[{material_id:'m',source_quote:'Hướng biên tập: công cụ này luôn tìm được tác giả.'}];
 assert.throws(()=>validateCoverage(r,hash,fields,metadataCatalog),/factual source excerpts/);
 const schema=coverageSchema(hash,fields,catalog);
 const hypotheses=schema.properties.checks.items.oneOf.filter(x=>x.properties.verdict.const==='HYPOTHESIS');
 assert.equal(hypotheses.length,1);assert.equal(hypotheses[0].properties.field.const,'inferences.0');assert.equal(hypotheses[0].properties.truth_status.const,'UNCONFIRMED');
});

test('a hypothesis correction never converts actual unsupported procedure directions into accepted analysis',async()=>{
 for(const text of ['Sử dụng công cụ kiểm tra dữ kiện được UNESCO đề xuất để tìm bằng chứng hỗ trợ hoặc phản bác.','Kiểm tra bằng chứng kèm theo và so sánh với công cụ kiểm chứng dữ kiện của UNESCO.']){
  const value={...analysis,direction:text},fields=analysisFields(value),hash=await analysisHash(value),r=receipt(hash,fields);
  r.checks[2]={field:'direction',verdict:'UNRESOLVED',premise_checks:[{assertion:text,quote_ids:[],supported:false}],reason:'Selected notes do not establish this tool applicability.',issues:['Unsupported institutional procedure']};
  assert.throws(()=>validateCoverage(r,hash,fields,catalog));assert.equal(value.direction,text);
 }
 const profile=await analysisProfile({stage:'source_audit',previous:[{value:analysis}],writing_prompt:'fixture\n'+JSON.stringify({materials})});
 const fields=analysisFields(analysis),hash=await analysisHash(analysis);let captured;
 await profile.run({run:async(_,input)=>{captured=input;return {response:{article_sha256:'unchanged',analysis_audit:receipt(hash,fields)}};}},'model',{messages:[{role:'system',content:'Strict original article audit'}],response_format:{type:'json_schema',json_schema:{properties:{},required:[]}}});
 assert.match(captured.messages[0].content,/NOT verified/);assert.match(captured.messages[0].content,/capability\/benefit assertion hidden/);assert.match(captured.messages[0].content,/NOT_APPLICABLE needs a concrete comparison/);
 assert.match(captured.messages[0].content,/later question explicitly referring back/);assert.match(captured.messages[0].content,/do not blanket-exempt later paragraphs/);
});
