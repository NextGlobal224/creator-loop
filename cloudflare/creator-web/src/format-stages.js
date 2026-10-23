// Non-article layouts reuse TEXT's source/premise/edit/immutable-audit guards.
import {generateStage,stageSignal} from "../../creator-ai/src/sdk-stage.js";
import {quoteCatalog,publicArticleIssues,publicCopyRevisions,uncitedAssertionIssues,auditSchema,bindAudit,validAudit} from "../../creator-ai/src/quality.js";
import {sourcedSegmentSchema,checkedSegment,checkPremises,sourceFirstInstructions,generatedPremiseInstructions,operationSchema,revisionIssues} from "../../creator-ai/src/grounding.js";

const names=["analysis","writing","editing","source_audit"];
const findings=["PROJECT_NO_UNSOURCED_RULE","PROJECT_NONREPETITIVE_DEVELOPMENT","PROJECT_REQUEST_AND_FORMAT"];
const stageResult=(env,stage,value,extra={})=>({model:env.CREATOR_MODEL,worker_version:env.CREATOR_VERSION.id,stage_result:{stage,value,usage:{},...extra}});

export function composeFormat(value,catalog){
  if(!value || Object.keys(value).join()!=="paragraphs" || !Array.isArray(value.paragraphs) || !value.paragraphs.length || value.paragraphs.length>24)throw Error("Invalid format paragraphs");
  const segments=[],claims=[];
  const paragraphs=value.paragraphs.map((paragraph,paragraph_index)=>{
    if(!Array.isArray(paragraph)||!paragraph.length||paragraph.length>12)throw Error("Invalid format paragraph");
    return paragraph.map(raw=>{
      const {text,source_quote_id}=checkedSegment(raw,catalog),clean=text.trim();
      const citation=source_quote_id>=0?{text:clean,material_ids:[catalog[source_quote_id].material_id],source_quote:catalog[source_quote_id].source_quote}:null;
      if(citation)claims.push(citation);
      segments.push({index:segments.length,paragraph_index,text:clean,citation});return clean;
    }).join(" ");
  });
  const body=paragraphs.join("\n\n");
  if(body.length>12000 || claims.length>30 || claims.some(c=>body.split(c.text).length!==2))throw Error("Invalid/ambiguous Draft spans");
  return {body,claims,segments,fictional_opening:false};
}

export function editFormat(operations,initial,catalog){
  if(!operations || Object.keys(operations).sort().join()!=="edits,source_issues" || !Array.isArray(operations.edits) || operations.edits.length!==initial.segments.length || !Array.isArray(operations.source_issues) || operations.source_issues.some(v=>typeof v!=="string"||!v.trim()))throw Error("Invalid edit coverage");
  const required=new Set(publicCopyRevisions(initial).map(r=>r.index));
  const paragraphs=Array.from({length:Math.max(...initial.segments.map(s=>s.paragraph_index))+1},()=>[]),checks=[];
  let finalIndex=0;
  for(const [i,edit] of operations.edits.entries()){
    const original=initial.segments[i];
    if(!edit || Object.keys(edit).sort().join()!=="decision,index,note,premise_checks,quote_ids,replacement,support" || edit.index!==i || !["KEEP","REWRITE","REMOVE"].includes(edit.decision) || !["SOURCE_SUPPORTED","SUGGESTION","HYPOTHETICAL","INSUFFICIENT"].includes(edit.support) || !Array.isArray(edit.replacement))throw Error("Invalid edit identity");
    checkPremises(edit.premise_checks,catalog);
    if(required.has(i)&&edit.decision==="KEEP" || edit.premise_checks.some(p=>!p.supported)&&(edit.support!=="INSUFFICIENT"||edit.decision==="KEEP") || edit.support==="SOURCE_SUPPORTED"&&!edit.premise_checks.length || ["SUGGESTION","HYPOTHETICAL"].includes(edit.support)&&edit.premise_checks.length || edit.support==="INSUFFICIENT"&&edit.decision==="KEEP")throw Error("Unsupported original retained");
    if(edit.support==="SOURCE_SUPPORTED"&&edit.premise_checks.some(p=>!p.quote_ids.every(q=>edit.quote_ids.includes(q))))throw Error("Missing original premise evidence");
    if(edit.decision!=="REWRITE"&&edit.replacement.length || edit.decision==="REWRITE"&&!edit.replacement.length)throw Error("Invalid edit replacement");
    const quoteId=original.citation?catalog.findIndex(q=>q.material_id===original.citation.material_ids[0]&&q.source_quote===original.citation.source_quote):-1;
    const produced=edit.decision==="REMOVE"?[]:edit.decision==="KEEP"?[{text:original.text,source_quote_id:quoteId,premise_checks:edit.premise_checks}]:edit.replacement;
    if(edit.support==="INSUFFICIENT"&&edit.decision==="REWRITE"){
      const words=text=>text.normalize("NFC").toLocaleLowerCase("vi").replace(/[\p{P}\p{S}]/gu," ").replace(/\s+/gu," ").trim();
      const final=words(produced.map(s=>s.text).join(" "));
      if([original.text,...edit.premise_checks.filter(p=>!p.supported).map(p=>p.assertion)].some(text=>words(text)&&final.includes(words(text))))throw Error("Insufficient original assertion remains in replacement");
    }
    if(edit.decision==="REWRITE"&&produced.map(s=>s.text.trim()).join(" ").normalize("NFC").replace(/\s+/gu," ")===original.text.trim().normalize("NFC").replace(/\s+/gu," "))throw Error("REWRITE did not change the original passage");
    const mapped=[];
    for(const segment of produced){checkedSegment(segment,catalog);paragraphs[original.paragraph_index].push(segment);mapped.push(finalIndex++);}
    const {replacement,premise_checks,...receipt}=edit;checks.push({...receipt,final_segment_indexes:mapped});
  }
  if(paragraphs.some(p=>!p.length))throw Error("Required format paragraph removed");
  const final=composeFormat({paragraphs},catalog);
  const issues=[...revisionIssues(initial,final,checks,catalog),...publicArticleIssues(final.body),...publicCopyRevisions(final).flatMap(r=>r.issues),...uncitedAssertionIssues(final.body,final.claims)];
  if(issues.length)throw Error("Source/public copy guard rejected edit");
  return {final,source_checks:checks,source_issues:[...new Set([...operations.source_issues,...operations.edits.filter(e=>e.support==="INSUFFICIENT").map(e=>e.note)])]};
}

export async function formatStage(env,packet){
  const requested=names.indexOf(packet.stage);
  if(!["IMAGE_CAPTION","VIDEO"].includes(packet.format)||requested<0||!Array.isArray(packet.previous)||packet.previous.length!==requested||packet.previous.some((p,i)=>p.stage!==names[i]))throw Error("Invalid format stage order");
  const data=JSON.parse(packet.writing_prompt.slice(packet.writing_prompt.lastIndexOf("\n")+1));
  if(data.format!==packet.format||!Array.isArray(data.materials)||!data.materials.length)throw Error("Format/source identity mismatch");
  const catalog=quoteCatalog(data.materials);
  if(!catalog.length)throw Error("No exact source passages");
  const jsonStage=async(instructions,input,schema,budget=4000)=>generateStage(env,{instructions,input,schema,budget,temperature:packet.stage==="writing"?0.3:0,reasoningEffort:packet.stage==="analysis"?"low":"medium",signal:stageSignal(env)},()=>{});
  if(requested===0){
    const schema={type:"object",properties:Object.fromEntries(["observations","inferences","direction"].map(k=>[k,packet.schema.properties[k]])),required:["observations","inferences","direction"],additionalProperties:false};
    const generated=await jsonStage("Phân tích nguồn tiếng Việt, chưa viết Draft. Quote phải nguyên văn trong đúng material. Observation chỉ mô tả điều đoạn nguồn nói. Inferences bắt đầu Giả thuyết chưa xác nhận. Hướng biên tập theo Project/mode và hướng người dùng, không tự đổi mục tiêu. Không coi đề xuất của ghi chú là khuyến nghị tổ chức. "+sourceFirstInstructions,JSON.stringify(data),schema,2600);
    const analysis=generated.value;
    if(!analysis?.observations?.length || analysis.observations.some(o=>!data.materials.some(m=>m.material_id===o.material_id&&typeof o.quote==="string"&&o.quote.trim()&&m.text.includes(o.quote))))throw Error("Invalid exact analysis source");
    return stageResult(env,packet.stage,analysis);
  }
  const analysis=packet.previous[0].value;
  const sourcePack={project:data,analysis,quote_catalog:catalog.map((q,id)=>({id,...q})),format_instructions:packet.prompt};
  const structure="Return paragraphs as arrays of checked segments. Join segments with spaces and paragraphs with blank lines. VIDEO headings and each scene line must each be a separate paragraph, with exact header/sections/six scene syntax from supplied format instructions. Blank lines between scene lines are allowed. Caption is 100–180 natural Vietnamese words matching the topic; never infer visible image details from text-only sources. For VIDEO align six narration/subtitle segments with six scene timings and name missing assets honestly. Invented descriptions must be labelled hypothetical. Keep private analysis/checklists out of public prose. ";
  const writingSchema={type:"object",properties:{paragraphs:{type:"array",minItems:1,maxItems:24,items:{type:"array",minItems:1,maxItems:12,items:sourcedSegmentSchema(catalog)}}},required:["paragraphs"],additionalProperties:false};
  if(requested===1){
    const generated=await jsonStage(structure+sourceFirstInstructions+generatedPremiseInstructions,JSON.stringify(sourcePack),writingSchema,4500);
    composeFormat(generated.value,catalog);
    return stageResult(env,packet.stage,generated.value);
  }
  const initial=composeFormat(packet.previous[1].value,catalog);
  if(requested===2){
    const generated=await jsonStage("Edit EACH immutable initial segment by index: KEEP, REWRITE with checked replacement segments, or REMOVE only when its format paragraph remains. Classify original source support SOURCE_SUPPORTED/SUGGESTION/HYPOTHETICAL/INSUFFICIENT. An unsupported original must keep INSUFFICIENT assessment and be removed or genuinely replaced; never relabel it advice. KEEP/REMOVE replacement:[]. Every mandatory_public_revisions index must be rewritten or removed. Preserve headings and six scene lines. Never output a replacement whole Draft. "+sourceFirstInstructions+generatedPremiseInstructions,JSON.stringify({source_pack:sourcePack,initial,mandatory_public_revisions:publicCopyRevisions(initial)}),operationSchema(initial.segments.length,catalog,initial),4500);
    editFormat(generated.value,initial,catalog);
    return stageResult(env,packet.stage,generated.value,{task_requirements:findings});
  }
  const edited=editFormat(packet.previous[2].value,initial,catalog),final=edited.final;
  const auditFindings=[...new Set([...edited.source_issues,...findings])];
  const hash=Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",new TextEncoder().encode(final.body)))).map(b=>b.toString(16).padStart(2,"0")).join("");
  const generated=await jsonStage("Audit every immutable segment by index and exact SHA, NEVER rewrite/copy text. Use SOURCE_FACT for every cited passage; classify all assertions, including factual presuppositions hidden in advice/fiction. Check complete meaning and attribution against exact quotes, not related words. Questions do not establish successful outcomes or institutional procedures. Report unsupported recommendations/efficacy/authors as issues. Each PROJECT finding is mandatory, never NOT_APPLICABLE. For PROJECT_NO_UNSOURCED_RULE and PROJECT_NONREPETITIVE_DEVELOPMENT check ALL indexes including headers/closing. Explain support/distinct contributions, and whether actual caption/VIDEO satisfies the selected Project/format. Missing assets stay missing. Check analysis too; errors go in first segment issues. Give each finding a concrete reason/relevant indexes. UNRESOLVED if any violation remains. No acceptance/approval/PASS. JSON only.",JSON.stringify({source_pack:sourcePack,immutable_segments:final.segments,article_sha256:hash,editor_findings:auditFindings,source_checks:edited.source_checks}),auditSchema(hash,final.segments.length,auditFindings.length,auditFindings),4500);
  const quality=bindAudit(final.body,hash,generated.value,final.segments,auditFindings);
  const annotation={...analysis,draft_body:final.body,factual_claims:final.claims};
  if(!validAudit(annotation,quality,false))throw Error("Immutable source audit rejected output");
  return {model:env.CREATOR_MODEL,worker_version:env.CREATOR_VERSION.id,result:{response:JSON.stringify(annotation)},quality_audit:quality,stages:[{stage:"analysis"},{stage:"writing",body:initial.body},{stage:"editing",body:final.body,source_issues:edited.source_issues,task_requirements:findings,source_checks:edited.source_checks},{stage:"source_audit"}]};
}
