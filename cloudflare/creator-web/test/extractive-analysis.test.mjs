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

test('actual Web bridge passes MANUAL selection into the profile without changing the Windows body protocol',async()=>{
 for(const format of ['TEXT','IMAGE_CAPTION']){
  const p={...packet(format),mode:'MANUAL'},selected=JSON.parse(p.writing_prompt.slice(p.writing_prompt.indexOf('\n{')+1)).direction;
  let calls=0;
  const result=JSON.parse(await stage({run:async(_,native)=>{
   calls++;assert.equal(native.response_format.json_schema.properties.direction.const,selected);
   return {choices:[{finish_reason:'stop',message:{content:JSON.stringify({...value,direction:selected})}}],usage:{}};
  }},'@cf/openai/gpt-oss-120b',JSON.stringify(p),'fixture'));
  assert.equal(calls,1);assert.equal(result.ok,true,JSON.stringify(result));assert.equal(result.stage_result.value.direction,selected);
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
test('duplicate source lines have one decoding branch while each source and caveat remains selectable',async()=>{
 const sources=[{material_id:'a',text:'Sách dành cho sinh viên báo chí.\nSách dành cho sinh viên báo chí.\nChưa đọc toàn bộ nội dung sách.'},{material_id:'b',text:'Bộ câu hỏi xuất phát từ nghiên cứu quan sát.\nChưa có dữ liệu đánh giá hiệu quả trong ghi chú.'}];
 const p=packet();p.writing_prompt='fixture\n'+JSON.stringify({purpose:'Giới thiệu tư liệu',format:'TEXT',materials:sources});
 const profile=await analysisProfile(p);let options;
 const observations=sources.flatMap(s=>[...new Set(s.text.split('\n'))].map(quote=>({material_id:s.material_id,quote,observation:quote})));
 await profile.run({run:async(_,native)=>{options=native.response_format.json_schema.properties.observations.items.oneOf;return {response:{observations,inferences:[],direction:'Đề xuất giới thiệu hai tư liệu và nêu rõ giới hạn đã đọc.'}};}},'model',{messages:[{role:'system',content:''}],response_format:{type:'json_schema',json_schema:{properties:{inferences:{},direction:{}},required:[]}}});
 assert.equal(options.length,4);assert.deepEqual(options.map(o=>o.properties.quote.const),observations.map(o=>o.quote));
});
test('native and runtime observation bounds match the existing Python forty-item contract',async()=>{
 const quotes=Array.from({length:41},(_,i)=>`Dữ kiện đã ghi số ${i+1}.`);
 const p=packet();p.writing_prompt='fixture\n'+JSON.stringify({purpose:'Giới thiệu tư liệu',format:'TEXT',materials:[{material_id:'m',text:quotes.join('\n')}]});
 const profile=await analysisProfile(p);let max;
 const observations=quotes.map(quote=>({material_id:'m',quote,observation:quote}));
 const invoke=count=>profile.run({run:async(_,native)=>{max=native.response_format.json_schema.properties.observations.maxItems;return {response:{observations:observations.slice(0,count),inferences:[],direction:'Đề xuất nội dung dựa trên tư liệu đã chọn.'}};}},'model',{messages:[{role:'system',content:''}],response_format:{type:'json_schema',json_schema:{properties:{inferences:{},direction:{}},required:[]}}});
 await invoke(40);assert.equal(max,40);
 await assert.rejects(invoke(41),/domain contract/);
});
