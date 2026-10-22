import test from 'node:test';
import assert from 'node:assert/strict';
import {analysisFields,analysisHash,coverageSchema,validateCoverage,directionAttributions,analysisProfile} from '../src/analysis-audit.js';
import {quoteCatalog} from '../../creator-ai/src/quality.js';

const materials=[{material_id:'a',text:'Source: Source Alpha\nAlpha nêu công cụ X để tìm ảnh.'},{material_id:'b',text:'Source: BETA Research\nBETA nêu giới hạn tìm kiếm.'}];
const catalog=quoteCatalog(materials);
const analysis={observations:[{material_id:'a',quote:catalog[1].source_quote,observation:catalog[1].source_quote}],inferences:[],direction:'Giới thiệu vấn đề. Theo BETA, công cụ X giúp tìm ảnh gốc. Cả hai nguồn đều đề cập giới hạn và công cụ này.'};
const receipt=(hash,fields)=>({analysis_sha256:hash,checks:fields.map(f=>({field:f.field,verdict:f.field==='direction'?'EDITORIAL_ONLY':'SUPPORTED',premise_checks:f.field==='direction'?[]:[{assertion:f.text,quote_ids:[1],supported:true}],reason:'Fixture assertion, not a quality review.',issues:[]}))});

test('source attribution within a plan cannot inherit an editorial-only exemption',async()=>{
 const fields=analysisFields(analysis),hash=await analysisHash(analysis),before=structuredClone(analysis),r=receipt(hash,fields);
 const sentences=directionAttributions(analysis.direction,catalog);
 assert.deepEqual(sentences,['Theo BETA, công cụ X giúp tìm ảnh gốc.','Cả hai nguồn đều đề cập giới hạn và công cụ này.']);
 assert.throws(()=>validateCoverage(r,hash,fields,catalog),/Direction attribution coverage/);
 const rule=coverageSchema(hash,fields,catalog).properties.checks.items.oneOf.find(x=>x.properties.field.const==='direction');
 assert.equal(rule.properties.verdict.const,'SUPPORTED');assert.equal(rule.properties.premise_checks.minItems,2);
 assert.ok(coverageSchema(hash,fields,catalog).properties.checks.items.oneOf.some(x=>x.properties.field.const==='direction'&&x.properties.verdict.const==='UNRESOLVED'));
 // A token support check for a fragment is not coverage of the full assertion.
 r.checks[1].verdict='SUPPORTED';r.checks[1].premise_checks=[{assertion:'Công cụ X',quote_ids:[1],supported:true}];
 assert.throws(()=>validateCoverage(r,hash,fields,catalog),/coverage/);
 r.checks[1].premise_checks=sentences.map(assertion=>({assertion,quote_ids:[0],supported:true}));
 assert.throws(()=>validateCoverage(r,hash,fields,catalog),/coverage/);
 // Even explicit negative findings with exact coverage must fail closed.
 r.checks[1].premise_checks=sentences.map(assertion=>({assertion,quote_ids:[1],supported:false}));
 assert.throws(()=>validateCoverage(r,hash,fields,catalog));
 assert.deepEqual(analysis,before);
});

test('source-neutral plans and separately checked source attributions remain possible',async()=>{
 assert.deepEqual(directionAttributions('Đề xuất xem ALPHABET thay vì ngữ cảnh riêng.',[{source_quote:'Source: ALPHA'}]),[]);
 for(const direction of ['Đề xuất mở bài bằng câu hỏi, giải thích giới hạn rồi kết bằng lời mời.','Không gán lời khuyên cá nhân cho BETA.','Dành một đoạn giới thiệu Source Alpha, rồi kết bằng câu hỏi.','Theo BETA, tư liệu nêu giới hạn tìm kiếm.']){
  const value={...analysis,direction},fields=analysisFields(value),hash=await analysisHash(value),r=receipt(hash,fields);
  if(direction.startsWith('Theo'))r.checks[1]={field:'direction',verdict:'SUPPORTED',premise_checks:[{assertion:direction,quote_ids:[3],supported:true}],reason:'The exact synthetic note supports the attribution.',issues:[]};
  assert.equal(validateCoverage(r,hash,fields,catalog),r);
 }
});

test('negative source attributions are factual even inside an editorial plan',async()=>{
 for(const direction of ['BETA không nêu công cụ X.','BETA chưa cung cấp hướng dẫn này.','Các nguồn không đề cập công cụ X.']){
  assert.deepEqual(directionAttributions(direction,catalog),[direction]);
  const value={...analysis,direction},fields=analysisFields(value),hash=await analysisHash(value);
  assert.throws(()=>validateCoverage(receipt(hash,fields),hash,fields,catalog),/coverage/);
 }
});

test('too many attribution sentences fail before an invalid native schema or call',()=>{
 const value={...analysis,direction:Array.from({length:9},(_,i)=>`Theo BETA, ý số ${i}.`).join(' ')};
 assert.throws(()=>coverageSchema('hash',analysisFields(value),catalog),/audit premise bound/);
});

test('actual audit prompt exposes full attribution sentences without additional binding calls',async()=>{
 const fields=analysisFields(analysis),hash=await analysisHash(analysis),r=receipt(hash,fields);
 const profile=await analysisProfile({format:'TEXT',stage:'source_audit',previous:[{value:analysis}],writing_prompt:'fixture\n'+JSON.stringify({materials})});
 let calls=0,input;
 await assert.rejects(profile.run({run:async(_,value)=>{calls++;input=value;return {response:{analysis_audit:r,article_sha256:'fixture'}};}},'model',{messages:[{role:'system',content:'Original audit'}],response_format:{type:'json_schema',json_schema:{properties:{},required:[]}}}),/coverage/);
 assert.equal(calls,1);assert.equal(input.max_tokens,6000);assert.equal(profile.diagnostic().validation,'REJECTED');
 const anchors=JSON.parse(input.messages.at(-1).content);
 assert.deepEqual(anchors.direction_attribution_sentences,directionAttributions(analysis.direction,catalog));
 assert.match(input.messages[0].content,/actual explicit user purpose and chosen direction/);
 assert.equal(profile.rejectedJSON().analysis_audit.analysis_sha256,hash);
});

test('writing and editing instructions distinguish extractive observations from usable article prose',async()=>{
 for(const stage of ['writing','editing']){
  const profile=await analysisProfile({format:'TEXT',stage,previous:[{value:analysis}],writing_prompt:'fixture\n'+JSON.stringify({purpose:'Giải thích giới hạn tư liệu',direction:'Nêu rõ điều chưa biết',materials})});
  let input;
  await profile.run({run:async(_,value)=>{input=value;return {response:{}};}},'model',{reasoning_effort:'medium',max_tokens:4500,messages:[{role:'system',content:'Original source constraints'}],response_format:{type:'json_schema',json_schema:{properties:{}}}});
  assert.equal(input.reasoning_effort,'medium');assert.equal(input.max_tokens,4500);
  assert.match(input.messages[0].content,stage==='writing'?/extractive observation rule applies ONLY/:/ORIGINAL support remains SOURCE_SUPPORTED/);
 }
});
