// Actual SDK envelopes with synthetic outputs; no live AI/semantic acceptance.
import test from 'node:test';
import assert from 'node:assert/strict';
import {stage} from '../src/ai-bridge.js';
import {analysisFields,analysisHash} from '../src/analysis-audit.js';
import {compileSourcedArticle} from '../../creator-ai/src/quality.js';
import {checkedWriting,plannedWriting} from '../../creator-ai/src/grounding.js';
import {composeFormat} from '../src/format-stages.js';

test('actual SDK fourth-call prompt/native schemas agree across all three Draft formats',async()=>{
 const quote='Bảo tàng mở cửa từ thứ Ba đến Chủ nhật.';
 const materials=[{material_id:'m',text:quote}];
 const fact={text:quote,source_quote_id:0,premise_checks:[{assertion:quote,quote_ids:[0],supported:true}]};
 const texts=[
  'Bạn có thể cân nhắc lựa chọn một buổi tham quan phù hợp với lịch riêng của gia đình, rồi ghi lại những điều mình muốn tìm hiểu trong chuyến đi.',
  'Nếu còn câu hỏi về trải nghiệm mong muốn, hãy viết chúng ra thành một danh sách riêng để tự xem lại trước khi chọn nội dung cho hành trình.',
  'Bạn cũng có thể chia sẻ dự định với người cùng đi và mời họ nêu sở thích của mình, không cần coi một lựa chọn là phù hợp cho tất cả mọi người.',
  'Cuối cùng, hãy giữ bản kế hoạch ở trạng thái có thể chỉnh sửa và chọn cách kết thúc chuyến đi theo mong muốn riêng, thay vì đặt ra một kết quả bắt buộc.',
 ];
 const advice=text=>({text,source_quote_id:-1,premise_checks:[]});
 const analysis={observations:[{material_id:'m',quote,observation:quote}],inferences:[],direction:'Đề xuất nội dung giới thiệu lịch đã có, kết bằng lời mời tự chọn.'};
 for(const format of ['TEXT','IMAGE_CAPTION','VIDEO']){
  const source={purpose:'Giới thiệu lịch tham quan bảo tàng',audience:'Gia đình',style:'Rõ ràng',direction:'Giới thiệu lịch đã biết',format,materials};
  const written=format==='TEXT'?{opening:[fact],editorial_paragraphs:Object.fromEntries(['context_and_scope','unresolved_information','supported_perspective','closing_message'].map((key,i)=>[key,[advice(texts[i])]]))}:{paragraphs:[[fact],...texts.map(text=>[advice(text)])]};
  const compiled=format==='TEXT'?compileSourcedArticle(checkedWriting(plannedWriting(written,{fictionalOpening:false}),[{material_id:'m',source_quote:quote}]),[{material_id:'m',source_quote:quote}]):composeFormat(written,[{material_id:'m',source_quote:quote}]);
  const edits={source_issues:[],edits:compiled.segments.map((s,i)=>({index:i,premise_checks:i===0?fact.premise_checks:[],quote_ids:i===0?[0]:[],support:i===0?'SOURCE_SUPPORTED':'SUGGESTION',decision:'KEEP',note:'Synthetic protocol fixture only',replacement:[]}))};
  const requirements=format==='TEXT'?['PROJECT_NO_UNSOURCED_RULE','PROJECT_CLOSING_MESSAGE','PROJECT_NONREPETITIVE_DEVELOPMENT']:['PROJECT_NO_UNSOURCED_RULE','PROJECT_NONREPETITIVE_DEVELOPMENT','PROJECT_REQUEST_AND_FORMAT'];
  const packet={stage:'source_audit',previous:[{stage:'analysis',value:analysis,usage:{}},{stage:'writing',value:written,usage:{}},{stage:'editing',value:edits,task_requirements:requirements,usage:{}}],format,prompt:'Synthetic format protocol only',writing_prompt:'fixture\n'+JSON.stringify(source),schema:{type:'object',properties:{observations:{type:'array',items:{type:'object'}},inferences:{type:'array',items:{type:'string'}},direction:{type:'string'},draft_body:{type:'string'},factual_claims:{type:'array',items:{type:'object'}}}}};
  let input,calls=0;
  const result=JSON.parse(await stage({run:async(_,native)=>{
   calls++;input=native;
   const schema=native.response_format.json_schema;
   const hash=schema.properties.article_sha256.const;
   return {response:{article_sha256:hash,finding_checks:requirements.map((_,index)=>({index,verdict:'ADDRESSED',segment_indexes:compiled.segments.map(s=>s.index),reason:'Synthetic protocol fixture, no quality acceptance'})),segments:compiled.segments.map((s,index)=>({index,kind:s.citation?'SOURCE_FACT':'ADVICE',issues:[]})),analysis_audit:{analysis_sha256:await analysisHash(analysis),checks:analysisFields(analysis).map(f=>({field:f.field,verdict:f.field==='direction'?'EDITORIAL_ONLY':'SUPPORTED',premise_checks:f.field==='direction'?[]:fact.premise_checks,reason:'Fixture only',issues:[]}))}},usage:{}};
  }},'@cf/openai/gpt-oss-120b',JSON.stringify(packet),'fixture-version'));
  assert.equal(calls,1,format);assert.equal(result.ok,true,JSON.stringify(result));
  assert.equal(input.max_tokens,6000,format);
  const marker='\nReturn only JSON matching this schema:\n';
  const user=input.messages.find(m=>m.role==='user'&&m.content.includes(marker));
  const schema=JSON.parse(user.content.slice(user.content.lastIndexOf(marker)+marker.length));
  assert.deepEqual(schema,input.response_format.json_schema,format);
  assert.ok(schema.required.includes('analysis_audit'),format);
  assert.match(user.content,/Bảo tàng mở cửa/);
 }
});
