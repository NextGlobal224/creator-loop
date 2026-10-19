import test from 'node:test';
import assert from 'node:assert/strict';
import {composeFormat,editFormat} from '../src/format-stages.js';
import {stage} from '../src/ai-bridge.js';
import worker from '../../creator-ai/src/index.js';
import {preparePending,reconcilePending,savedSelection,changedDraftText} from '../public/pending.mjs';

const catalog=[{material_id:'m',source_quote:'Bảo tàng mở cửa từ thứ Ba đến Chủ nhật.'}];
const fact={text:catalog[0].source_quote,source_quote_id:0,premise_checks:[{assertion:catalog[0].source_quote,quote_ids:[0],supported:true}]};
const advice={text:'Bạn có thể ghi lại câu hỏi riêng trước khi chọn trải nghiệm phù hợp với gia đình.',source_quote_id:-1,premise_checks:[]};
const initial=()=>composeFormat({paragraphs:[[fact],[advice]]},catalog);
const operations=()=>({source_issues:[],edits:initial().segments.map(s=>({index:s.index,premise_checks:s.citation?fact.premise_checks:[],quote_ids:s.citation?[0]:[],support:s.citation?'SOURCE_SUPPORTED':'SUGGESTION',decision:'KEEP',replacement:[],note:'Protocol fixture only'}))});
test('lost ACK keeps payload/identity and is reconciled only by job lookup',()=>{
 const data={purpose:'Original'},p=preparePending(data,null,()=> 'original-id');
 assert.deepEqual(preparePending(data,p,()=>{throw Error('must not allocate new id');}),p);
 assert.throws(()=>preparePending({purpose:'Changed'},p,()=> 'new-id'));
 assert.deepEqual(reconcilePending(200,{id:p.key,status:'RUNNING'}),{clear:true,job:{id:p.key,status:'RUNNING'}});
 assert.deepEqual(reconcilePending(404,{}),{clear:false,absent:true});
 assert.throws(()=>reconcilePending(503,{}));
 const fresh=preparePending({purpose:'After explicit discard'},null,()=> 'new-id');assert.equal(fresh.key,'new-id');
});
test('reload opens latest manual exact version, preserves old selection and no-op does not restore AI proposal',()=>{
 const old={draft_version_id:'v1',version_no:1,body_text:'Old AI proposal',direction:'old',image_file_id:'old-image'};
 const edited={draft_version_id:'v2',version_no:2,body_text:'Actual manual edit',direction:'chosen',image_file_id:'new-image',image_provenance:'source',image_usage_note:'rights'};
 const workflow={versions:[old,edited],llm_proposal:{draft_body:old.body_text},direction_text:'old',output_format:'IMAGE_CAPTION'};
 assert.deepEqual(savedSelection(workflow),edited);assert.deepEqual(savedSelection(workflow,'v1'),old);assert.equal(savedSelection(workflow,'proposal'),null);
 assert.equal(changedDraftText(workflow,edited,{...edited}),false);
 assert.equal(changedDraftText(workflow,edited,{...edited,body_text:old.body_text}),true);
 assert.equal(changedDraftText(workflow,edited,{...edited,image_file_id:'different'}),true);
});
test('non-article format preserves exact cited bytes and immutable segment identities',()=>{
 const before=initial(),after=editFormat(operations(),before,catalog);
 assert.equal(after.final.body,before.body);assert.deepEqual(after.final.claims,before.claims);
 assert.deepEqual(after.source_checks.map(c=>c.final_segment_indexes),[[0],[1]]);
});
test('unsupported original cannot be retained or relabelled as advice',()=>{
 for(const support of ['SOURCE_SUPPORTED','SUGGESTION','INSUFFICIENT']){
  const ops=operations();ops.edits[0].premise_checks[0].supported=false;ops.edits[0].support=support;
  assert.throws(()=>editFormat(ops,initial(),catalog));
  ops.edits[0].premise_checks[0].supported=true;
 }
});
test('replacement cannot introduce unsupported premises or orphan required paragraph',()=>{
 const ops=operations();ops.edits[1].decision='REWRITE';ops.edits[1].replacement=[{...advice,premise_checks:[{assertion:'Bịa kết quả',quote_ids:[],supported:false}]}];
 assert.throws(()=>editFormat(ops,initial(),catalog));
 const removed=operations();removed.edits[1].decision='REMOVE';assert.throws(()=>editFormat(removed,initial(),catalog));
});
test('caption and VIDEO cannot retain acknowledged unsupported premises in a softened rewrite',()=>{
 for(const format of ['IMAGE_CAPTION','VIDEO']){
  const unsupported='Phương pháp A luôn tốt nhất';
  const before=composeFormat({paragraphs:[[fact],[{...advice,text:`Nếu muốn tìm hiểu, ${unsupported}. Hãy ghi lại nhận định.`}]]},catalog);
  const ops={source_issues:[],edits:operations().edits};
  Object.assign(ops.edits[1],{support:'INSUFFICIENT',decision:'REWRITE',premise_checks:[{assertion:unsupported,quote_ids:[],supported:false}],replacement:[{...advice,text:'Có lẽ PHƯƠNG PHÁP A—LUÔN TỐT NHẤT, bạn có thể cân nhắc.'}]});
  assert.throws(()=>editFormat(ops,before,catalog),/Insufficient original assertion/,format);
 }
});
test('Web TEXT retains Windows stage protocol with explicit analysis profile',async()=>{
 const source={purpose:'Giới thiệu lịch tham quan bảo tàng',audience:'Gia đình',style:'Rõ ràng',direction:'Giới thiệu lịch đã biết',format:'TEXT',materials:[{material_id:'m',text:catalog[0].source_quote,scope:'TEXT_PASSAGE',source_url:'https://example.org/fixture'}]};
 const packet={prompt:'fixture',writing_prompt:'fixture\n'+JSON.stringify(source),schema:{properties:{observations:{type:'array'},inferences:{type:'array'},direction:{type:'string'}}},stage:'analysis',previous:[]};
 const analysis={observations:[{material_id:'m',quote:catalog[0].source_quote,observation:catalog[0].source_quote}],inferences:[],direction:source.direction};
 const captured=[];const AI={run:async(_,input)=>{captured.push(input);return {response:analysis,usage:{prompt_tokens:1,completion_tokens:1}};}};
 const native=await worker.fetch(new Request('https://fixture.test/b/generate',{method:'POST',headers:{authorization:'Bearer fixture','content-type':'application/json'},body:JSON.stringify(packet)}),{AI,CREATOR_MODEL:'@cf/openai/gpt-oss-120b',CREATOR_KEY:'fixture',CREATOR_VERSION:{id:'fixture'}});
 const ffi=JSON.parse(await stage(AI,'@cf/openai/gpt-oss-120b',JSON.stringify({...packet,format:'TEXT'}),'fixture'));
 assert.equal(native.status,200);assert.equal(ffi.ok,true);assert.equal(ffi.model_calls,1);assert.notDeepEqual(captured[0],captured[1]);assert.equal(captured[1].response_format.json_schema.properties.inferences.items.pattern,'^Giả thuyết chưa xác nhận: .+$');assert.match(captured[1].response_format.json_schema.properties.direction.description,/KẾ HOẠCH NỘI DUNG/);assert.match(captured[1].response_format.json_schema.properties.inferences.description,/tiền đề factual/);assert.deepEqual((await native.json()).stage_result,ffi.stage_result);
 for(const format of ['TEXT','IMAGE_CAPTION','VIDEO']){
  const packetWithFormat={...packet,format,writing_prompt:'fixture\n'+JSON.stringify({...source,format})};
  const choiceAI={run:async()=>({choices:[{index:0,finish_reason:'stop',message:{role:'assistant',content:JSON.stringify(analysis),reasoning_content:'PRIVATE_REASONING_FIXTURE'}}],usage:{prompt_tokens:1,completion_tokens:1}})};
  const choicesFFI=JSON.parse(await stage(choiceAI,'@cf/openai/gpt-oss-120b',JSON.stringify(packetWithFormat),'fixture'));
  assert.equal(choicesFFI.ok,true,format);assert.deepEqual(choicesFFI.stage_result.value,analysis);assert.equal(choicesFFI.profile_diagnostic.response_channel,'choices');assert.doesNotMatch(JSON.stringify(choicesFFI),/PRIVATE_REASONING_FIXTURE/);
 }
});
