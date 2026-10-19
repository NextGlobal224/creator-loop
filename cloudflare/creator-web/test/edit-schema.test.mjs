import test from 'node:test';
import assert from 'node:assert/strict';
import {stage} from '../src/ai-bridge.js';
import {constrainRewrites} from '../src/edit-schema.js';
import {compileSourcedArticle} from '../../creator-ai/src/quality.js';
import {checkedWriting,plannedWriting,applyEditOperations} from '../../creator-ai/src/grounding.js';
import {composeFormat,editFormat} from '../src/format-stages.js';

const quote='Bảo tàng mở cửa từ thứ Ba đến Chủ nhật.';
const catalog=[{material_id:'m',source_quote:quote}];
const fact={text:quote,source_quote_id:0,premise_checks:[{assertion:quote,quote_ids:[0],supported:true}]};
const texts=['Bạn có thể cân nhắc lựa chọn một buổi tham quan phù hợp với lịch riêng của gia đình, rồi ghi lại những điều mình muốn tìm hiểu trong chuyến đi.','Nếu còn câu hỏi về trải nghiệm mong muốn, hãy viết chúng ra thành một danh sách riêng để tự xem lại trước khi chọn nội dung cho hành trình.','Bạn cũng có thể chia sẻ dự định với người cùng đi và mời họ nêu sở thích của mình, không cần coi một lựa chọn là phù hợp cho tất cả mọi người.','Cuối cùng, hãy giữ bản kế hoạch ở trạng thái có thể chỉnh sửa và chọn cách kết thúc chuyến đi theo mong muốn riêng, thay vì đặt ra một kết quả bắt buộc.'];
const advice=text=>({text,source_quote_id:-1,premise_checks:[]});

test('actual SDK editing constraints preserve receipts and align textual/native schemas in each format',async()=>{
 const exports=[];
 for(const format of ['TEXT','IMAGE_CAPTION','VIDEO']){
  const source={purpose:'Giới thiệu lịch tham quan bảo tàng',audience:'Gia đình',style:'Rõ ràng',direction:'Giới thiệu lịch đã biết',format,materials:[{material_id:'m',text:quote}]};
  const written=format==='TEXT'?{opening:[fact],editorial_paragraphs:Object.fromEntries(['context_and_scope','unresolved_information','supported_perspective','closing_message'].map((key,i)=>[key,[advice(texts[i])]]))}:{paragraphs:[[fact],...texts.map(text=>[advice(text)])]};
  const initial=format==='TEXT'?compileSourcedArticle(checkedWriting(plannedWriting(written,{fictionalOpening:false}),catalog),catalog):composeFormat(written,catalog);
  const analysis={observations:[{material_id:'m',quote,observation:quote}],inferences:[],direction:'Đề xuất nội dung giới thiệu lịch đã biết.'};
  const operations={source_issues:[],edits:initial.segments.map((s,index)=>({index,premise_checks:index===0?fact.premise_checks:[],support:index===0?'SOURCE_SUPPORTED':'SUGGESTION',quote_ids:index===0?[0]:[],decision:'KEEP',note:'Protocol fixture only',replacement:[]}))};
  const packet={format,stage:'editing',previous:[{stage:'analysis',value:analysis,usage:{}},{stage:'writing',value:written,usage:{}}],prompt:'Fixture only',writing_prompt:'fixture\n'+JSON.stringify(source),schema:{type:'object',properties:{observations:{type:'array'},inferences:{type:'array'},direction:{type:'string'},draft_body:{type:'string'},factual_claims:{type:'array'}}}};
  let schema,calls=0;
  const result=JSON.parse(await stage({run:async(_,native)=>{
   calls++;schema=native.response_format.json_schema;
   const marker='\nReturn only JSON matching this schema:\n',user=native.messages.find(m=>m.role==='user'&&m.content.includes(marker));
   assert.deepEqual(JSON.parse(user.content.slice(user.content.lastIndexOf(marker)+marker.length)),schema);
   const branches=schema.properties.edits.items.oneOf.filter(b=>b.properties.decision.const==='REWRITE');
   assert.equal(branches.length,initial.segments.length);
   for(const b of branches){
    assert.deepEqual([...b.required].sort(),['decision','index','note','premise_checks','quote_ids','replacement','support']);
    for(const item of b.properties.replacement.items.oneOf)assert.deepEqual(item.properties.text.not,{const:initial.segments[b.properties.index.const].text});
   }
   return {response:operations,usage:{}};
  }},'@cf/openai/gpt-oss-120b',JSON.stringify(packet),'fixture'));
  assert.equal(calls,1);assert.equal(result.ok,true,JSON.stringify(result));
  const noOp=structuredClone(operations);noOp.edits[0].decision='REWRITE';noOp.edits[0].replacement=[fact];
  const realEdit=structuredClone(noOp);realEdit.edits[0].replacement=[{...fact,text:'Theo lịch đã ghi, bảo tàng mở cửa từ thứ Ba đến Chủ nhật.'}];
  const apply=v=>format==='TEXT'?applyEditOperations(v,initial,catalog):editFormat(v,initial,catalog);
  assert.throws(()=>apply(noOp),/change/);assert.doesNotThrow(()=>apply(realEdit));
  const space=structuredClone(noOp);space.edits[0].replacement[0].text='  '+quote.replaceAll(' ','  ')+' ';
  assert.throws(()=>apply(space));
  exports.push({format,schema,keep:operations,no_op:noOp,rewrite:realEdit});
 }
 // Optional fixture-only export for an independent full JSON Schema validator.
 if(process.env.B_WEB_SCHEMA_EXPORT){const fs=await import('node:fs');fs.writeFileSync(process.env.B_WEB_SCHEMA_EXPORT,JSON.stringify(exports));}
});
test('unexpected decoder shape or missing inherited edit receipts fails closed',()=>{
 assert.throws(()=>constrainRewrites({properties:{edits:{items:{oneOf:[{properties:{index:{const:0},decision:{const:'REWRITE'}}}]}}}},{segments:[{text:'Original'}]}));
});
