import test from 'node:test';
import assert from 'node:assert/strict';
import {analysisFields,analysisHash,validateAnalysis,validateCoverage,analysisProfile} from '../src/analysis-audit.js';
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
 assert.throws(()=>validateAnalysis({...analysis,observations:[{...analysis.observations[0],quote:'not in original'}]},materials));
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
