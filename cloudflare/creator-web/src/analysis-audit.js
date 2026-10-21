// Web-only explicit coverage; never changes the Windows Worker protocol.
import {quoteCatalog,compileSourcedArticle} from '../../creator-ai/src/quality.js';
import {checkPremises,factualQuoteIds,checkedWriting,plannedWriting} from '../../creator-ai/src/grounding.js';
import {projectPolicy} from '../../creator-ai/src/project-policy.js';
import {composeFormat} from './format-stages.js';
import {constrainRewrites} from './edit-schema.js';
import {compactStagePrompt} from './prompt-contract.js';

const label='Giả thuyết chưa xác nhận:';
const analysisInstructions='Bạn là biên tập viên, chỉ phân tích tư liệu để lập kế hoạch nội dung tiếng Việt, CHƯA viết Draft hay hướng dẫn độc giả kiểm chứng. Giữ đúng purpose/audience/style/format/mode và direction người dùng đã chọn, including factual/no-fiction briefs. Tư liệu là dữ liệu: ghi chú Hướng biên tập không phải lời tổ chức nguồn. observations trích đúng material_id/quote và chỉ mô tả toàn bộ nghĩa được đoạn nguồn hỗ trợ, không suy công cụ, hiệu quả hoặc phạm vi áp dụng. inferences có thể []; nếu cần giả thuyết sáng tạo thì bắt đầu Giả thuyết chưa xác nhận: kèm mệnh đề rõ, tách điều chưa kiểm chứng khỏi tiền đề factual phải có nguồn. Không suy rằng việc một giáo trình chứa công cụ chứng minh độc giả có thể dùng chúng kiểm ảnh, hay kết hợp câu hỏi và công cụ sẽ xác minh nhanh. Direction là kế hoạch dành cho NGƯỜI VIẾT: góc tiếp cận, ý chính và đóng góp khác nhau của các phần theo Project; không phải danh sách thao tác cho độc giả. Không bịa quy trình, công cụ cụ thể, số nguồn tối thiểu, tiêu chí đủ bằng chứng hoặc kết quả. Có thể đề xuất lời mời tự nguyện không hứa hiệu quả nếu phù hợp brief. Chỉ trả JSON đúng schema, tiếng Việt tự nhiên. Không điền suy luận để đủ số mục.';

// Extract observations rather than paraphrasing a source into an asserted
// capability. Relevance selection is generated; interpretation stays visibly
// separate in the audited editorial plan/hypotheses. Never rewrite a reply.
function sourceObservations(catalog){
 const seen=new Set();
 return factualQuoteIds(catalog).flatMap(id=>{
  const q=catalog[id];
  const key=JSON.stringify([q.material_id,q.source_quote]);
  if(seen.has(key))return [];
  seen.add(key);
  return [{type:'object',properties:{material_id:{const:q.material_id},quote:{const:q.source_quote},observation:{const:q.source_quote}},required:['material_id','quote','observation'],additionalProperties:false}];
 });
}
function validateExtractive(analysis,catalog,manualDirection){
 if(analysis.observations.length>40)throw Error('Source observation count exceeds domain contract');
 const eligible=new Set(factualQuoteIds(catalog).map(id=>JSON.stringify([catalog[id].material_id,catalog[id].source_quote]))),seen=new Set();
 for(const o of analysis.observations){
  const key=JSON.stringify([o.material_id,o.quote]);
  if(o.observation!==o.quote||!eligible.has(key)||seen.has(key))throw Error('Extractive source observation required');
  seen.add(key);
 }
 if(Array.from(analysis.direction).length>(manualDirection===null?1200:2000))throw Error('Editorial plan exceeds declared bound');
 if(manualDirection!==null&&analysis.direction!==manualDirection)throw Error('Manual direction must preserve the user selection');
}
function finalChannelDiagnostic(result){
 const content=result?.choices?.[0]?.message?.content;
 const finish=result?.choices?.[0]?.finish_reason;
 let jsonStatus='NOT_AVAILABLE';
 if(typeof content==='string'){try{JSON.parse(content);jsonStatus='VALID';}catch{jsonStatus='INVALID';}}
 return {finish_reason:['stop','length','content_filter','tool_calls','function_call'].includes(finish)?finish:'NOT_AVAILABLE',final_content_utf8_bytes:typeof content==='string'?new TextEncoder().encode(content).length:null,final_json_status:jsonStatus};
}

function bindPromptSchema(native,schema,stage){
 const marker='\nReturn only JSON matching this schema:\n';
 const message=native.messages.find(m=>m.role==='user'&&typeof m.content==='string'&&m.content.includes(marker));
 if(!message)return; // Direct profile unit callers have no SDK prompt suffix.
 const at=message.content.lastIndexOf(marker);
 const original=JSON.parse(message.content.slice(at+marker.length));
 const expected=stage==='analysis'?'direction,inferences,observations':stage==='editing'?'edits,source_issues':'article_sha256,finding_checks,segments';
 if(original.type!=='object'||original.additionalProperties!==false||Object.keys(original.properties??{}).sort().join()!==expected||[...(original.required??[])].sort().join()!==expected)throw Error('Unexpected stage prompt schema');
 message.content=message.content.slice(0,at)+marker+JSON.stringify(schema);
}
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
// A plan may contain factual attributions. These are conservative coverage
// anchors, not an entailment classifier: the model/source review must still
// check the complete meaning, including attributions without these cues.
export function directionAttributions(text,catalog){
 const names=new Set();
 for(const q of catalog){
  const match=q.source_quote.match(/^Source\s*:\s*(.+)$/iu);
  if(!match)continue;
  names.add(match[1].trim().toLocaleLowerCase());
  for(const word of match[1].match(/\p{L}[\p{L}\p{N}.-]*/gu)??[]){
   if(word.length>=2&&word===word.toLocaleUpperCase())names.add(word.toLocaleLowerCase());
  }
 }
 return [...new Set(text.split(/(?<=[.!?;])\s+/u).map(s=>s.trim()).filter(Boolean))].filter(sentence=>{
  const normalized=sentence.toLocaleLowerCase();
  const reporting='(?:(?:không|chưa|có|đã|cũng|đều)\\s+)*(?:đề cập|nêu|khuyên|cho biết|mô tả|hướng dẫn|cung cấp|đề nghị|tìm|dùng|đối chiếu|xác nhận|cho rằng)(?![\\p{L}\\p{N}])';
  return [...names].some(name=>{
   const identity='(?<![\\p{L}\\p{N}])'+name.replace(/[.*+?^${}()|[\]\\]/gu,'\\$&')+'(?![\\p{L}\\p{N}])';
   // Mere names in instructions such as "giới thiệu AFP" or "không gán ...
   // cho AFP" are not factual claims. Reported speech, including negation,
   // and an explicit parenthetical attribution still need source support.
   return new RegExp('(?:^|[(:])\\s*theo\\s+'+identity,'u').test(normalized)||new RegExp(identity+'\\s+'+reporting,'u').test(normalized);
  })||new RegExp('(?:nguồn|tư liệu)\\s+'+reporting,'iu').test(sentence);
 });
}
export function coverageSchema(hash,fields,catalog){
 const items=fields.flatMap(f=>{
  const base={type:'object',properties:{field:{const:f.field},verdict:{type:'string',enum:['SUPPORTED','EDITORIAL_ONLY','UNRESOLVED']},premise_checks:{type:'array',maxItems:8,items:{type:'object',properties:{assertion:{type:'string',minLength:1},quote_ids:{type:'array',items:{type:'integer',minimum:0,maximum:catalog.length-1}},supported:{type:'boolean'}},required:['assertion','quote_ids','supported'],additionalProperties:false}},reason:{type:'string',minLength:1},issues:{type:'array',items:{type:'string'}}},required:['field','verdict','premise_checks','reason','issues'],additionalProperties:false};
  if(f.field==='direction'){
   const attributions=directionAttributions(f.text,catalog);
   if(attributions.length){
    if(attributions.length>8)throw Error('Direction attribution count exceeds audit premise bound');
    base.properties.verdict.enum=['SUPPORTED','UNRESOLVED'];
    base.properties.premise_checks.minItems=attributions.length;
    base.properties.premise_checks.items.properties.assertion.description='Cover every complete attribution sentence verbatim, separately; other empirical premises also require checks.';
   }
  }
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
  if(fields[i].field==='direction'){
   const attributions=directionAttributions(fields[i].text,catalog);
   if(attributions.length&&(check.verdict!=='SUPPORTED'||attributions.some(text=>!check.premise_checks.some(p=>p.assertion===text&&p.supported&&p.quote_ids.length&&p.quote_ids.every(id=>factualQuoteIds(catalog).includes(id))))))throw Error('Direction attribution coverage missing');
  }
 }
 return receipt;
}
export async function analysisProfile(packet){
 const source=JSON.parse(packet.writing_prompt.slice(packet.writing_prompt.indexOf('\n{')+1));
 const manualDirection=packet.mode==='MANUAL'?source.direction:null;
 if(manualDirection!==null&&(typeof manualDirection!=='string'||!manualDirection.trim()||Array.from(manualDirection).length>2000))throw Error('Invalid selected manual direction');
 const catalog=quoteCatalog(source.materials);
 if(!catalog.length)throw Error('Missing analysis source catalog');
 const analysis=packet.previous?.[0]?.value;
 const fields=packet.stage==='analysis'?null:validateAnalysis(analysis,source.materials);
 const hash=fields?await analysisHash(analysis):null;
 let receipt=null,diagnostic=null,rejectedJSON=null,finalText=null;
 return {
  async run(binding,model,input,options){
   receipt=null;diagnostic=null;rejectedJSON=null;finalText=null;
   const native=structuredClone(input),schema=native.response_format?.json_schema;
   if(!schema||native.response_format.type!=='json_schema')throw Error('Expected native structured schema');
   // Actual MANUAL TEXT stopped at3000 tokens before completing its final JSON.
   // Keep model/effort and all guards; match the existing caption writing cap.
   // 112000 input +17600 output tokens remain below the unchanged5000 forecast.
   if(packet.stage==='writing'&&packet.format==='TEXT')native.max_tokens=4500;
   if(packet.stage==='analysis'){
    // The live low-effort analysis repeatedly invented efficacy/threshold rules.
    // Reuse the same model/call/token cap with more reasoning on this stage only.
    native.reasoning_effort='medium';
    const observations=sourceObservations(catalog);
    if(!observations.length)throw Error('No factual source observations available');
    schema.properties.observations={type:'array',minItems:1,maxItems:Math.min(40,observations.length),items:{oneOf:observations}};
    if(schema.properties.direction)schema.properties.direction.maxLength=manualDirection===null?1200:2000;
    if(manualDirection!==null)schema.properties.direction.const=manualDirection;
    schema.properties.inferences.items={type:'string',pattern:'^Giả thuyết chưa xác nhận: .+$',minLength:label.length+2};
    schema.properties.inferences.description='Có thể để []. Nếu cần, nêu giả thuyết biên tập còn chưa kiểm chứng, dựa trên điều nguồn thực sự cung cấp. Phân biệt kết luận giả thuyết với tiền đề factual: nhãn chưa xác nhận không hợp thức hóa công cụ, khả năng áp dụng hay khuyến nghị tổ chức bị bịa.';
    if(schema.properties.direction)schema.properties.direction.description='KẾ HOẠCH NỘI DUNG cho người viết: góc tiếp cận và đóng góp khác nhau của các phần, phù hợp Project/mode/format/direction đã chọn. Không biến thành quy trình kiểm chứng cho độc giả, không yêu cầu dùng công cụ ngoài snapshot, số lượng nguồn tối thiểu hoặc hứa kết quả. Giữ đúng yêu cầu factual/no-fiction nếu có.';
    native.messages[0].content=analysisInstructions+' observations là sổ trích nguồn: chọn các câu nguồn liên quan, giữ observation bằng nguyên văn quote đúng schema, không thêm diễn giải. Chọn đủ dữ kiện và giới hạn quan trọng cho brief, không coi trích dẫn là toàn bộ nguồn hay sự tán thành. Phần diễn giải chỉ nằm trong direction/inferences với audit tiền đề riêng. Direction là kế hoạch ngắn tối đa 1200 ký tự, nêu đóng góp khác nhau của các phần; không phải bài viết hoặc các bước kiểm chứng. Không gộp đóng góp của hai nguồn thành một lời quy kết: nguồn nào nêu công cụ, nguồn nào nêu giới hạn phải tách đúng snapshot; không suy chi tiết từ URL chưa đọc. Có thể lập kế hoạch không quy kết tổ chức, để dữ kiện ở observations. Use inferences:[] when no substantive inference is needed.';
    if(manualDirection!==null)native.messages[0].content+=' MANUAL override: direction is the already selected user brief, not a new model plan. Return that exact schema const unchanged (up to 2000 Unicode characters); do not add steps, source claims or metadata comparisons. Its presence is NOT proof of source support: observations, article and immutable analysis audit retain all source checks.';
    bindPromptSchema(native,schema,packet.stage);
   }
   if(packet.stage==='analysis'&&manualDirection===null&&projectPolicy(source).fictionalOpening)native.messages[0].content+=' For this specifically commissioned screenshot-sharing Project, propose a concrete fictional information gap and voluntary request for source/context instead of inventing a verification procedure.';
   if(packet.stage==='writing')native.messages[0].content+=' The generated analysis direction is NOT evidence or a mandatory procedure. Omit any tool applicability, institutional recommendation or promised result it invents. A source fact that a handbook contains tools does not support telling this reader to use those tools for a quick check on a phone. Keep external source facts separate from voluntary requests for missing context; use natural Vietnamese rather than repeating an English question list and its translation. When continuing a commissioned fictional scene later in the article, explicitly refer to that hypothetical scene, not an observed real event. Develop connected distinct ideas instead of one-line source summaries or repeating a request for missing information before the actual closing message.';
   if(packet.stage==='writing'&&packet.format==='TEXT')compactStagePrompt(native,'writing');
   if(packet.stage==='writing'&&packet.format==='TEXT')native.messages[0].content+=' The extractive observation rule applies ONLY to the analysis ledger, not to article prose. Compose an actual article for the explicit user purpose and chosen direction: frame that subject in the opening and connect each relevant supported idea to it. Paraphrase naturally within each exact excerpt scope; source_quote_id and premise_checks retain provenance even when public wording changes. Do not merely concatenate observation sentences or lead with irrelevant publication metadata. Exact names, dates and necessary quotations may remain exact; novelty is not permission to add facts. A factual/no-fiction brief permits source-grounded explanation and voluntary questions, not an invented event. Write the requested closing itself in clear Vietnamese, not a description of asking someone for transparency.';
   if(packet.stage==='editing')native.messages[0].content+=' A REWRITE must actually change the original passage after whitespace normalization. If a compliant opening or other passage remains word-for-word unchanged, choose KEEP with replacement:[]; never emit an identical REWRITE. Preserve existing mandatory-revision rules: they require an actual change or allowed removal. Assess the WHOLE original sentence, not merely its supported excerpt: a tool-applicability or outcome clause absent from the quote makes the original INSUFFICIENT even when its introductory source fact is supported. Preserve that negative assessment when replacing it with supported content. Do not retain a capability claim or an English/Vietnamese duplicate question list as advice.';
   if(packet.stage==='editing'&&packet.previous?.[1]?.value){
    const written=packet.previous[1].value;
    const initial=packet.format==='TEXT'?compileSourcedArticle(checkedWriting(plannedWriting(written,projectPolicy(source)),catalog),catalog):composeFormat(written,catalog);
    constrainRewrites(schema,initial);
    compactStagePrompt(native,'editing');
   }
   if(packet.stage==='editing'&&packet.format==='TEXT')native.messages[0].content+=' Separately compare the complete article with the explicit user purpose/style/direction: correct facts alone do not establish brief fit or natural prose. A collage of source-note sentences can require REWRITE for relevance, transitions or awkward syntax while the ORIGINAL support remains SOURCE_SUPPORTED. Do not falsely mark a supported original INSUFFICIENT merely to edit it. KEEP requires both usable wording and contextual fit; preserve concise factual or quoted passages when they actually serve the brief. Do not replace editorial repair with generic advice, new empirical assertions or a promise of verification.';
   if(packet.stage==='source_audit'){
    // The fourth actual response consumed the old 4000-token ceiling without
    // a complete final channel. Keep one bounded call; preserve full coverage.
    native.max_tokens=6000;
    schema.properties.analysis_audit=coverageSchema(hash,fields,catalog);schema.required=[...schema.required,'analysis_audit'];
    native.messages[0].content+=' Separately audit EACH immutable analysis field in ordered analysis_audit checks. SUPPORTED requires exact catalog quote IDs supporting the ENTIRE meaning; EDITORIAL_ONLY is a voluntary content plan/proposal with zero empirical premise. HYPOTHESIS is the distinct unconfirmed category defined below. Unsupported embedded factual premises, institutional attribution, tool applicability or efficacy presented as established fact remain UNRESOLVED with issues. Never rewrite analysis. A source list of questions does not establish a verification procedure or institutional tool recommendation.';
    native.messages[0].content+=' CONTRACT DISTINCTION: only inferences.N may instead use HYPOTHESIS with truth_status:UNCONFIRMED. This means the explicitly labelled conclusion is NOT verified and has NO demonstrated effect evidence. Check separately every embedded factual background premise against exact eligible quotes; at least one supported background premise is required. A related quote does not prove invented tool identity, availability, applicability, institutional recommendation or efficacy. Missing background support remains UNRESOLVED. Do not reject a clearly unconfirmed hypothesized conclusion solely because it lacks confirmation, and never mark that conclusion SUPPORTED. Observations and direction cannot use HYPOTHESIS. For article findings, judge the full meaning: an invitation to consider questions without a promised result does not require proof that it succeeds; a capability/benefit assertion hidden behind có thể or a purpose clause still requires evidence. Editor findings are leads, not binding verdicts; NOT_APPLICABLE needs a concrete comparison with the immutable final text, and PROJECT findings retain their mandatory rules.';
    native.messages[0].content+=' Fictional scope: a later question explicitly referring back to the labelled invented opening is not a factual report that a real screenshot exists. Do not demand a source proving the invented event; still audit real-world assertions embedded anywhere. Only the opening itself may be classified HYPOTHETICAL under the existing segment rules. A later pure question/invitation can be ADVICE without empirical premises. Distinguish these from an assertion about an actual external event or capability; do not blanket-exempt later paragraphs. Evaluate continuity and distinct development independently.';
    native.messages.push({role:'user',content:JSON.stringify({immutable_analysis_sha256:hash,immutable_analysis_fields:fields,exact_quote_catalog:catalog})});
    native.messages.push({role:'user',content:JSON.stringify({direction_attribution_sentences:directionAttributions(analysis.direction,catalog),coverage_rule:'Each listed complete sentence needs its own verbatim assertion with eligible exact supporting quote IDs and SUPPORTED; unsupported or ambiguous whole-sentence attribution is UNRESOLVED, never blanket EDITORIAL_ONLY. This list is conservative and does not exempt other factual premises.'})});
    if(packet.format==='TEXT')native.messages[0].content+=' For PROJECT_NONREPETITIVE_DEVELOPMENT compare the actual explicit user purpose and chosen direction with the immutable article, not the generated plan. Distinct source summaries alone do not establish explanatory development. If the requested subject is absent, connections are missing or the ending is syntactically unclear, report UNRESOLVED with the actual passages; do not declare ADDRESSED just because facts are supported or paragraphs differ. Do not require invented detail, a word quota, or a fixed screenshot situation for a different Project.';
    bindPromptSchema(native,schema,packet.stage);
   }
   const result=await binding.run(model,native,options);
   const content=result?.choices?.[0]?.message?.content;
   if(result?.choices?.length===1&&typeof content==='string'&&new TextEncoder().encode(content).length<=65536)finalText=content;
   diagnostic={response_received:!!result,response_channel:typeof result?.choices?.[0]?.message?.content==='string'?'choices':Object.hasOwn(result??{},'response')?'response':'unknown',validation:'NOT_REQUIRED',...finalChannelDiagnostic(result)};
   try{
   if(packet.stage==='analysis'){
    const {value}=nativeJSON(result);rejectedJSON=value;validateAnalysis(value,source.materials);validateExtractive(value,catalog,manualDirection);
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
  // Authenticated failed-job evidence only, never reasoning/headers or repair.
  rejectedFinalText:()=>diagnostic&&(diagnostic.finish_reason==='length'||diagnostic.final_json_status==='INVALID')?finalText:null,
 };
}
