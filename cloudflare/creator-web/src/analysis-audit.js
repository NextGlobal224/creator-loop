// Web-only explicit coverage; never changes the Windows Worker protocol.
import {quoteCatalog} from '../../creator-ai/src/quality.js';
import {checkPremises,factualQuoteIds} from '../../creator-ai/src/grounding.js';
import {projectPolicy} from '../../creator-ai/src/project-policy.js';

const label='Giả thuyết chưa xác nhận:';
// Match workers-ai-provider@4.0.0 processText precedence. Inspect only the
// final JSON channel; never extract hidden reasoning, fences or partial JSON.
function nativeJSON(result){
 const content=result?.choices?.[0]?.message?.content;
 if(content!=null&&typeof content!=='string')throw Error('Unsupported native choice content');
 if(typeof content==='string'&&content.length){
  if(result.choices.length!==1||result.choices[0].finish_reason!=='stop'||result.choices[0].message.tool_calls?.length)throw Error('Incomplete native JSON choice');
  return {value:JSON.parse(content),replace:value=>({...result,choices:[{...result.choices[0],message:{...result.choices[0].message,content:JSON.stringify(value)}}]})};
 }
 const value=typeof result?.response==='string'?JSON.parse(result.response):result?.response;
 if(!value||typeof value!=='object'||Array.isArray(value))throw Error('Missing native JSON response');
 return {value,replace:value=>({...result,response:typeof result.response==='string'?JSON.stringify(value):value})};
}
export function analysisFields(analysis){
 if(!analysis||!Array.isArray(analysis.observations)||!analysis.observations.length||!Array.isArray(analysis.inferences)||typeof analysis.direction!=='string'||!analysis.direction.trim())throw Error('Invalid immutable analysis');
 return [...analysis.observations.map((o,i)=>({field:`observations.${i}`,text:o.observation,material_id:o.material_id,quote:o.quote})),...analysis.inferences.map((text,i)=>({field:`inferences.${i}`,text})),{field:'direction',text:analysis.direction}];
}
export function validateAnalysis(analysis,materials){
 const fields=analysisFields(analysis);
 if(fields.some(f=>typeof f.text!=='string'||!f.text.trim()))throw Error('Empty analysis field');
 if(analysis.observations.some(o=>!materials.some(m=>m.material_id===o.material_id&&typeof o.quote==='string'&&o.quote.trim()&&m.text.includes(o.quote))))throw Error('Analysis quote identity mismatch');
 if(analysis.inferences.some(s=>!s.startsWith(label)||!s.slice(label.length).trim()))throw Error('Substantive unconfirmed inference required');
 return fields;
}
export async function analysisHash(analysis){
 return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(analysis))))).map(v=>v.toString(16).padStart(2,'0')).join('');
}
export function coverageSchema(hash,fields,catalog){
 const items=fields.flatMap(f=>{
  const base={type:'object',properties:{field:{const:f.field},verdict:{type:'string',enum:['SUPPORTED','EDITORIAL_ONLY','UNRESOLVED']},premise_checks:{type:'array',maxItems:8,items:{type:'object',properties:{assertion:{type:'string',minLength:1},quote_ids:{type:'array',items:{type:'integer',minimum:0,maximum:catalog.length-1}},supported:{type:'boolean'}},required:['assertion','quote_ids','supported'],additionalProperties:false}},reason:{type:'string',minLength:1},issues:{type:'array',items:{type:'string'}}},required:['field','verdict','premise_checks','reason','issues'],additionalProperties:false};
  if(!f.field.startsWith('inferences.'))return [base];
  return [base,{...base,properties:{...base.properties,verdict:{const:'HYPOTHESIS'},truth_status:{const:'UNCONFIRMED'},premise_checks:{...base.properties.premise_checks,minItems:1}},required:[...base.required,'truth_status']}];
 });
 return {type:'object',properties:{analysis_sha256:{const:hash},checks:{type:'array',minItems:fields.length,maxItems:fields.length,items:{oneOf:items}}},required:['analysis_sha256','checks'],additionalProperties:false};
}
export function validateCoverage(receipt,hash,fields,catalog){
 if(!receipt||Object.keys(receipt).sort().join()!=='analysis_sha256,checks'||receipt.analysis_sha256!==hash||!Array.isArray(receipt.checks)||receipt.checks.length!==fields.length)throw Error('Analysis audit identity/coverage missing');
 for(const [i,check] of receipt.checks.entries()){
  const hypothesis=check?.verdict==='HYPOTHESIS';
  const keys=hypothesis?'field,issues,premise_checks,reason,truth_status,verdict':'field,issues,premise_checks,reason,verdict';
  if(!check||Object.keys(check).sort().join()!==keys||check.field!==fields[i].field||!['SUPPORTED','EDITORIAL_ONLY','HYPOTHESIS'].includes(check.verdict)||typeof check.reason!=='string'||!check.reason.trim()||!Array.isArray(check.issues)||check.issues.length)throw Error('Analysis audit unresolved');
  if(hypothesis&&(!fields[i].field.startsWith('inferences.')||!fields[i].text.startsWith(label)||!fields[i].text.slice(label.length).trim()||check.truth_status!=='UNCONFIRMED'))throw Error('Invalid unconfirmed hypothesis boundary');
  checkPremises(check.premise_checks,catalog);
  if(hypothesis&&check.premise_checks.some(p=>p.quote_ids.some(q=>!factualQuoteIds(catalog).includes(q))))throw Error('Hypothesis background must use factual source excerpts');
  if(check.premise_checks.some(p=>!p.supported)||['SUPPORTED','HYPOTHESIS'].includes(check.verdict)&&!check.premise_checks.length||check.verdict==='EDITORIAL_ONLY'&&check.premise_checks.length||fields[i].field.startsWith('observations.')&&check.verdict!=='SUPPORTED')throw Error('Unsupported analysis premise');
 }
 return receipt;
}
export async function analysisProfile(packet){
 const source=JSON.parse(packet.writing_prompt.slice(packet.writing_prompt.indexOf('\n{')+1));
 const catalog=quoteCatalog(source.materials);
 if(!catalog.length)throw Error('Missing analysis source catalog');
 const analysis=packet.previous?.[0]?.value;
 const fields=packet.stage==='analysis'?null:validateAnalysis(analysis,source.materials);
 const hash=fields?await analysisHash(analysis):null;
 let receipt=null,diagnostic=null,rejectedJSON=null;
 return {
  async run(binding,model,input,options){
   receipt=null;diagnostic=null;rejectedJSON=null;
   const native=structuredClone(input),schema=native.response_format?.json_schema;
   if(!schema||native.response_format.type!=='json_schema')throw Error('Expected native structured schema');
   if(packet.stage==='analysis'){
    // The live low-effort analysis repeatedly invented efficacy/threshold rules.
    // Reuse the same model/call/token cap with more reasoning on this stage only.
    native.reasoning_effort='medium';
    schema.properties.inferences.items={type:'string',pattern:'^Giả thuyết chưa xác nhận: .+$',minLength:label.length+2};
    schema.properties.inferences.description='Có thể để []. Nếu cần, nêu giả thuyết biên tập còn chưa kiểm chứng, dựa trên điều nguồn thực sự cung cấp. Phân biệt kết luận giả thuyết với tiền đề factual: nhãn chưa xác nhận không hợp thức hóa công cụ, khả năng áp dụng hay khuyến nghị tổ chức bị bịa.';
    if(schema.properties.direction)schema.properties.direction.description='KẾ HOẠCH NỘI DUNG cho người viết: góc tiếp cận và đóng góp khác nhau của các phần, phù hợp Project/mode/format/direction đã chọn. Không biến thành quy trình kiểm chứng cho độc giả, không yêu cầu dùng công cụ ngoài snapshot, số lượng nguồn tối thiểu hoặc hứa kết quả. Giữ đúng yêu cầu factual/no-fiction nếu có.';
    native.messages[0].content+=' Every inference MUST start Giả thuyết chưa xác nhận: followed by an actual unconfirmed proposition. A bare label is invalid. Use inferences:[] when no substantive inference is needed. Direction is a voluntary editorial proposal, never a source-endorsed procedure. Do not prescribe an external tool, handbook exercise, quick-check workflow or device-specific use when the selected notes do not describe its applicability. A handbook containing tools establishes only that content exists, not how this reader can use it. Respect the selected Project purpose, mode, format and explicit manual direction, including factual/no-fiction briefs. Do not fabricate missing information or a sender message when the Project does not request them. COR questions are questions, not evidence that applying them identifies author/truth. Use natural Vietnamese, no invented institutional recommendation.';
   }
   if(packet.stage==='analysis')native.messages[0].content+=' Ràng buộc nguồn: không đặt ngưỡng số nguồn xác nhận để kết luận thật/giả khi snapshot không nêu đúng quy tắc đó. Tên một bộ câu hỏi không chứng minh hiệu quả giảm chia sẻ, độ tin cậy hay khả năng xác minh. Nhãn giả thuyết không cho phép giấu một khẳng định hiệu quả thiếu căn cứ; chỉ đề xuất lựa chọn biên tập không mang tiền đề thực nghiệm, hoặc để inferences:[]. Direction chỉ chọn cách trình bày phù hợp Project, không bổ sung kiến thức hay quy trình từ trí nhớ.';
   if(packet.stage==='analysis'&&projectPolicy(source).fictionalOpening)native.messages[0].content+=' For this specifically commissioned screenshot-sharing Project, propose a concrete fictional information gap and voluntary request for source/context instead of inventing a verification procedure.';
   if(packet.stage==='writing')native.messages[0].content+=' The generated analysis direction is NOT evidence or a mandatory procedure. Omit any tool applicability, institutional recommendation or promised result it invents. A source fact that a handbook contains tools does not support telling this reader to use those tools for a quick check on a phone. Keep external source facts separate from voluntary requests for missing context; use natural Vietnamese rather than repeating an English question list and its translation.';
   if(packet.stage==='editing')native.messages[0].content+=' A REWRITE must actually change the original passage after whitespace normalization. If a compliant opening or other passage remains word-for-word unchanged, choose KEEP with replacement:[]; never emit an identical REWRITE. Preserve existing mandatory-revision rules: they require an actual change or allowed removal. Assess the WHOLE original sentence, not merely its supported excerpt: a tool-applicability or outcome clause absent from the quote makes the original INSUFFICIENT even when its introductory source fact is supported. Preserve that negative assessment when replacing it with supported content. Do not retain a capability claim or an English/Vietnamese duplicate question list as advice.';
   if(packet.stage==='source_audit'){
    schema.properties.analysis_audit=coverageSchema(hash,fields,catalog);schema.required=[...schema.required,'analysis_audit'];
    native.messages[0].content+=' Separately audit EACH immutable analysis field in ordered analysis_audit checks. Enumerate every factual premise including institutional attribution, tool applicability and efficacy inside hypotheses/direction. A label never excuses unsupported facts. SUPPORTED requires exact catalog quote IDs; EDITORIAL_ONLY only for voluntary proposals with zero empirical premise; otherwise UNRESOLVED with issues. An observation needs support for its entire meaning, not merely an exact quote. Never rewrite analysis. A source list of questions does not establish a procedure or institutional tool recommendation. Missing support is UNRESOLVED.';
    native.messages[0].content+=' CONTRACT DISTINCTION: only inferences.N may instead use HYPOTHESIS with truth_status:UNCONFIRMED. This means the explicitly labelled conclusion is NOT verified and has NO demonstrated effect evidence. Check separately every embedded factual background premise against exact eligible quotes; at least one supported background premise is required. A related quote does not prove invented tool identity, availability, applicability, institutional recommendation or efficacy. Missing background support remains UNRESOLVED. Do not reject a clearly unconfirmed hypothesized conclusion solely because it lacks confirmation, and never mark that conclusion SUPPORTED. Observations and direction cannot use HYPOTHESIS. For article findings, judge the full meaning: an invitation to consider questions without a promised result does not require proof that it succeeds; a capability/benefit assertion hidden behind có thể or a purpose clause still requires evidence. Editor findings are leads, not binding verdicts; NOT_APPLICABLE needs a concrete comparison with the immutable final text, and PROJECT findings retain their mandatory rules.';
    native.messages.push({role:'user',content:JSON.stringify({immutable_analysis_sha256:hash,immutable_analysis_fields:fields,exact_quote_catalog:catalog})});
   }
   const result=await binding.run(model,native,options);
   diagnostic={response_received:!!result,response_channel:typeof result?.choices?.[0]?.message?.content==='string'?'choices':Object.hasOwn(result??{},'response')?'response':'unknown',validation:'NOT_REQUIRED'};
   try{
   if(packet.stage==='analysis'){
    const {value}=nativeJSON(result);rejectedJSON=value;validateAnalysis(value,source.materials);
   }
   if(packet.stage==='source_audit'){
    const {value,replace}=nativeJSON(result);
    rejectedJSON=value;
    receipt=value.analysis_audit;
    validateCoverage(receipt,hash,fields,catalog);
    const {analysis_audit,...article}=value;
    diagnostic.validation='PASS';rejectedJSON=null;return replace(article);
   }
   if(packet.stage==='analysis')diagnostic.validation='PASS';
   rejectedJSON=null;
   return result;
   }catch(error){diagnostic.validation='REJECTED';throw error;}
  },
  receipt:()=>receipt,
  diagnostic:()=>diagnostic,
  // Only parsed final JSON, never reasoning/headers/provider exception text.
  rejectedJSON:()=>rejectedJSON,
 };
}
