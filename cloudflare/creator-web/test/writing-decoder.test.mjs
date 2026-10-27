// Controlled native responses only, not evidence of provider/semantic quality.
import test from 'node:test';
import assert from 'node:assert/strict';
import {writingDecodingSchema} from '../src/writing-decoder.js';
import {plannedWritingSchema,checkedWriting,plannedWriting} from '../../creator-ai/src/grounding.js';
import {stage} from '../src/ai-bridge.js';

const quotes=['Bảo tàng mở cửa vào thứ Ba.','Vé được bán tại quầy.'];
const catalog=quotes.map(source_quote=>({material_id:'m',source_quote}));
const keys=['context_and_scope','unresolved_information','supported_perspective','closing_message'];
const fact=(id=0)=>({source_quote_id:id,text:quotes[id],premise_checks:[{assertion:quotes[id],quote_ids:[id],supported:true}]});
const advice={source_quote_id:-1,text:'Bạn có thể chọn buổi tham quan phù hợp.',premise_checks:[]};
const paragraphs=[
 'Bạn có thể cân nhắc lựa chọn một buổi tham quan phù hợp với lịch riêng của gia đình, rồi ghi lại những điều mình muốn tìm hiểu trong chuyến đi.',
 'Nếu còn câu hỏi về trải nghiệm mong muốn, hãy viết chúng ra thành một danh sách riêng để tự xem lại trước khi chọn nội dung cho hành trình.',
 'Bạn cũng có thể chia sẻ dự định với người cùng đi và mời họ nêu sở thích của mình, không cần coi một lựa chọn là phù hợp cho tất cả mọi người.',
 'Cuối cùng, hãy giữ bản kế hoạch ở trạng thái có thể chỉnh sửa và chọn cách kết thúc chuyến đi theo mong muốn riêng, thay vì đặt ra một kết quả bắt buộc.',
];
const article={opening:[fact()],editorial_paragraphs:Object.fromEntries(keys.map((k,i)=>[k,[{...advice,text:paragraphs[i]}]]))};
const source={purpose:'Giới thiệu lịch bảo tàng',audience:'Người đọc Việt',style:'Rõ ràng',direction:'Giới thiệu lịch và lời mời tự chọn',format:'TEXT',materials:[{material_id:'m',text:quotes.join('\n')} ]};
const packet={format:'TEXT',mode:'MANUAL',stage:'writing',prompt:'Controlled protocol test',writing_prompt:'fixture\n'+JSON.stringify(source),previous:[{stage:'analysis',value:{observations:[{material_id:'m',quote:quotes[0],observation:quotes[0]}],inferences:[],direction:source.direction},usage:{}}],schema:{type:'object',properties:{observations:{type:'array'},inferences:{type:'array'},direction:{type:'string'},draft_body:{type:'string'},factual_claims:{type:'array'}}}};

test('native writing grammar is smaller; original schema, roles and bounds remain immutable',()=>{
 const schema=plannedWritingSchema(catalog,{fictionalOpening:false}),before=JSON.stringify(schema);
 const native=writingDecodingSchema(schema,catalog);
 assert.equal(JSON.stringify(schema),before);
 assert.ok(JSON.stringify(native).length<before.length);
 assert.equal(native.properties.opening.minItems,1);assert.equal(native.properties.opening.maxItems,12);
 assert.deepEqual(Object.keys(native.properties.editorial_paragraphs.properties),keys);
 assert.equal(native.properties.opening.items.oneOf,undefined);
 assert.deepEqual(native.properties.opening.items.properties.source_quote_id.enum,[-1,0,1]);
 assert.equal(native.properties.opening.items.properties.premise_checks.maxItems,8);
 assert.equal(native.properties.opening.items.properties.premise_checks.items.properties.quote_ids.maxItems,1);
 assert.throws(()=>writingDecodingSchema({...schema,properties:{...schema.properties,opening:{...schema.properties.opening,items:{type:'object'}}}},catalog));
});

test('full domain checks still reject uncited factual receipts, wrong quote IDs and unsupported premises',()=>{
 for(const bad of [
  {...fact(),source_quote_id:-1},
  {...fact(),premise_checks:[]},
  {...fact(),premise_checks:[{...fact().premise_checks[0],quote_ids:[1]}]},
  {...fact(),premise_checks:[{...fact().premise_checks[0],supported:false}]},
 ])assert.throws(()=>checkedWriting(plannedWriting({...article,opening:[bad]},{fictionalOpening:false}),catalog));
});

test('actual SDK path accepts complete checked JSON and rejects cross-field mismatch/truncated native output without retry',async()=>{
 for(const value of [article,{...article,opening:[{...fact(),premise_checks:[{...fact().premise_checks[0],quote_ids:[1]}]}]},null]){
  let calls=0;
  const result=JSON.parse(await stage({run:async(model,native)=>{
   calls++;assert.equal(model,'@cf/openai/gpt-oss-120b');assert.equal(native.max_tokens,4500);
   assert.equal(native.response_format.json_schema.properties.opening.items.oneOf,undefined);
   return {choices:[{finish_reason:value?'stop':'length',message:{content:value?JSON.stringify(value):'{"opening":[{"source_quote_id":0,"text":"dở","premise_checks":[{"quote_ids":[0 '+ ' '.repeat(200)}}],usage:{neurons:1}};
  }},'@cf/openai/gpt-oss-120b',JSON.stringify(packet),'fixture'));
  assert.equal(calls,1);assert.equal(result.ok,value===article,JSON.stringify(result));
  if(value===null){assert.equal(result.profile_diagnostic.finish_reason,'length');assert.equal(result.profile_diagnostic.final_json_status,'INVALID');}
 }
});
