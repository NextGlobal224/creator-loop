import test from 'node:test';
import assert from 'node:assert/strict';
import {stage} from '../src/ai-bridge.js';
import {analysisProfile} from '../src/analysis-audit.js';

const quote='Bảo tàng mở cửa từ thứ Ba đến Chủ nhật.';
const materials=[{material_id:'m',text:'Source: Example\nHướng biên tập: hứa kết quả cho mọi người.\n'+quote}];
const value={observations:[{material_id:'m',quote,observation:quote}],inferences:[],direction:'Đề xuất giới thiệu lịch đã có và lời mời tự chọn buổi tham quan.'};
function packet(format='TEXT'){
 return {format,stage:'analysis',previous:[],prompt:'Fixture only',writing_prompt:'fixture\n'+JSON.stringify({purpose:'Giới thiệu lịch bảo tàng',audience:'Gia đình',style:'Rõ ràng',direction:'Chỉ dùng dữ kiện đã có',format,materials}),schema:{type:'object',properties:{observations:{type:'array',items:{type:'object',properties:{material_id:{type:'string'},quote:{type:'string'},observation:{type:'string'}},required:['material_id','quote','observation'],additionalProperties:false}},inferences:{type:'array',items:{type:'string'}},direction:{type:'string'},draft_body:{type:'string'},factual_claims:{type:'array',items:{type:'object'}}},required:['observations','inferences','direction','draft_body','factual_claims'],additionalProperties:false}};
}
test('actual SDK extraction selects source facts across all Draft formats with matching prompt schema',async()=>{
 for(const format of ['TEXT','IMAGE_CAPTION','VIDEO']){
  let calls=0;
  const result=JSON.parse(await stage({run:async(_,native)=>{
   calls++;
   const schema=native.response_format.json_schema,options=schema.properties.observations.items.oneOf;
   assert.deepEqual(options.map(o=>o.properties.quote.const),[quote]);
   assert.equal(schema.properties.direction.maxLength,1200);
   const user=native.messages.find(m=>m.role==='user'),marker='\nReturn only JSON matching this schema:\n';
   assert.deepEqual(JSON.parse(user.content.slice(user.content.lastIndexOf(marker)+marker.length)),schema);
   return {choices:[{finish_reason:'stop',message:{content:JSON.stringify(value)}}],usage:{}};
  }},'@cf/openai/gpt-oss-120b',JSON.stringify(packet(format)),'fixture'));
  assert.equal(calls,1);assert.equal(result.ok,true,JSON.stringify(result));assert.deepEqual(result.stage_result.value,value);
 }
});
test('ignored native constraints cannot add interpretation, metadata, duplicate observations or oversized plan',async()=>{
 for(const mutate of [v=>v.observations[0].observation+=' nên có hiệu quả',v=>v.observations[0]={material_id:'m',quote:'Source: Example',observation:'Source: Example'},v=>v.observations.push({...v.observations[0]}),v=>v.direction='x'.repeat(1201)]){
  const v=structuredClone(value);mutate(v);
  const result=JSON.parse(await stage({run:async()=>({choices:[{finish_reason:'stop',message:{content:JSON.stringify(v)}}],usage:{}})},'@cf/openai/gpt-oss-120b',JSON.stringify(packet()),'fixture'));
  assert.equal(result.ok,false);assert.equal(result.profile_diagnostic.validation,'REJECTED');
 }
});
test('incomplete final channel records bounded metadata without exporting text or hidden reasoning',async()=>{
 const profile=await analysisProfile(packet());
 await assert.rejects(profile.run({run:async()=>({choices:[{finish_reason:'length',message:{content:'{"incomplete":',reasoning_content:'private reasoning'}}]})},'model',{messages:[{role:'system',content:''}],response_format:{type:'json_schema',json_schema:{properties:{inferences:{},direction:{}},required:[]}}}));
 const diagnostic=profile.diagnostic();
 assert.equal(diagnostic.finish_reason,'length');assert.equal(diagnostic.final_json_status,'INVALID');assert.equal(diagnostic.final_content_utf8_bytes,14);
 assert.equal(JSON.stringify(diagnostic).includes('private reasoning'),false);assert.equal(profile.rejectedJSON(),null);
});
