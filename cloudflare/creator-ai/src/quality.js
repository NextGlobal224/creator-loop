// Model audit is evidence for review, never human approval or proof of truth.
export function safeFailure(error, result) {
  // Never return exception messages, request data, hidden reasoning or headers.
  const codes=new Set([3003,3006,3007,3008,3023,3036,3040,3041,3042,5004,5005,5007,5016,5018,5019,5035]);
  const found=String(error?.message ?? "").match(/\b(?:30\d\d|50\d\d)\b/u);
  const code=found && codes.has(Number(found[0]))?Number(found[0]):null;
  const usage={};
  for(const key of ["input_tokens","output_tokens","prompt_tokens","completion_tokens","total_tokens","neurons"]){
    if(Number.isFinite(result?.usage?.[key]) && result.usage[key]>=0) usage[key]=result.usage[key];
  }
  const stage=error?.stage_diagnostic;
  const stageDiagnostic=stage && ["SDK_BEFORE_BINDING","NATIVE_BINDING","SDK_AFTER_BINDING"].includes(stage.phase) &&
    Number.isInteger(stage.binding_calls) && stage.binding_calls>=0 && stage.binding_calls<=2?
    {phase:stage.phase,binding_calls:stage.binding_calls,...(stage.signal_aborted===true?{signal_aborted:true}:{}),native_failure:stage.native_failure &&
      (stage.native_failure.code===null || Number.isInteger(stage.native_failure.code) && stage.native_failure.code>=1000 && stage.native_failure.code<=99999) &&
      Array.isArray(stage.native_failure.flags) && stage.native_failure.flags.every(flag=>["SCHEMA","UNIQUE_ITEMS","MIN_ITEMS","MAX_ITEMS","CONSTRAINT","UNSUPPORTED","VALIDATION","JSON","ARRAY","INTEGER","TIMEOUT","QUOTA","RATE_LIMIT","CAPACITY","AUTH","MEMORY","MAX_TOKENS","INFERENCE","UPSTREAM","INTERNAL"].includes(flag))?
      {code:stage.native_failure.code,flags:stage.native_failure.flags}:null}:null;
  return {provider_code:code,usage,response_received:!!result,stage_diagnostic:stageDiagnostic,
    top_level_keys:["response","choices","output","usage","result","data","content","observations","inferences","direction","error","errors","success"].filter(key=>result && Object.hasOwn(result,key)),
    response_type:typeof result?.response,choice_content_type:typeof result?.choices?.[0]?.message?.content,
    choice_finish_reason:["stop","length","content_filter","tool_calls"].includes(result?.choices?.[0]?.finish_reason)?result.choices[0].finish_reason:null,
    output_types:Array.isArray(result?.output)?result.output.map(item=>item?.type).filter(t=>["message","reasoning","function_call"].includes(t)).slice(0,10):[]};
}

export function auditSchema(articleHash, segmentCount, findingCount=0,findings=[]) {
  const schema={type:"object",properties:{
    article_sha256:{type:"string",const:articleHash},
    finding_checks:{type:"array",minItems:findingCount,maxItems:findingCount,items:{
      type:"object",properties:{index:{type:"integer"},
        verdict:{type:"string",enum:["ADDRESSED","NOT_APPLICABLE","UNRESOLVED"]},
        segment_indexes:{type:"array",minItems:1,uniqueItems:true,items:{type:"integer",minimum:0,maximum:segmentCount-1}},
        reason:{type:"string",minLength:1}},
      required:["index","verdict","segment_indexes","reason"],additionalProperties:false}},
    segments:{type:"array",minItems:segmentCount,maxItems:segmentCount,items:{
      type:"object",properties:{index:{type:"integer",enum:Array.from({length:segmentCount},(_,i)=>i)},
        kind:{type:"string",enum:["HYPOTHETICAL","ADVICE","SOURCE_FACT"]},issues:{type:"array",items:{type:"string"}}},
      required:["index","kind","issues"],additionalProperties:false}},
  },required:["article_sha256","segments","finding_checks"],additionalProperties:false};
  if(findings.length){
    if(findings.length!==findingCount)throw new Error("Audit finding count mismatch");
    schema.properties.finding_checks.items.oneOf=findings.flatMap((finding,index)=>{
      const project=finding.startsWith("PROJECT_");
      const global=["PROJECT_NO_UNSOURCED_RULE","PROJECT_NONREPETITIVE_DEVELOPMENT"].includes(finding);
      const minItems=global?4:1;
      return (project?["ADDRESSED","UNRESOLVED"]:[null]).map(verdict=>({properties:{index:{const:index},...(verdict?{verdict:{const:verdict}}:{}),segment_indexes:{minItems,...(global?{const:Array.from({length:segmentCount},(_,i)=>i)}:{})}},required:["index","segment_indexes",...(verdict?["verdict"]:[])]}));
    });
  }
  return schema;
}

export function bindAudit(body,articleHash,audit,segments,editorFindings=[]) {
  const paragraphs=body.split(/\n\s*\n/u).map(p=>p.trim()).filter(Boolean);
  if(!audit || Object.keys(audit).sort().join(",")!=="article_sha256,finding_checks,segments" || audit.article_sha256!==articleHash ||
     !Array.isArray(segments) || !Array.isArray(audit.segments) || audit.segments.length!==segments.length ||
     audit.segments.some((item,i)=>!item || Object.keys(item).sort().join(",")!=="index,issues,kind" || item.index!==i ||
       !["HYPOTHETICAL","ADVICE","SOURCE_FACT"].includes(item.kind) || !Array.isArray(item.issues) || item.issues.some(issue=>typeof issue!=="string" || !issue.trim()))) return null;
  // Every composed segment is audited once. Reattach its exact original text;
  // the model classifies IDs, never copies source sentences or paragraph prose.
  if(segments.some((segment,i)=>segment.index!==i || !paragraphs[segment.paragraph_index]?.includes(segment.text) || (segment.citation && audit.segments[i].kind!=="SOURCE_FACT")) ||
     paragraphs.some((text,i)=>segments.filter(s=>s.paragraph_index===i).map(s=>s.text).join(" ")!==text)) return null;
  // Findings are leads, including possible false positives. Require an explicit,
  // text-bound disposition for every lead; an empty global issues list cannot
  // silently discard them. A model disposition is still not proof of entailment.
  if(!Array.isArray(audit.finding_checks) || audit.finding_checks.length!==editorFindings.length ||
     audit.finding_checks.some((c,i)=>!c || Object.keys(c).sort().join(",")!=="index,reason,segment_indexes,verdict" || c.index!==i ||
       !["ADDRESSED","NOT_APPLICABLE","UNRESOLVED"].includes(c.verdict) || typeof c.reason!=="string" || !c.reason.trim() ||
       !Array.isArray(c.segment_indexes) || !c.segment_indexes.length || new Set(c.segment_indexes).size!==c.segment_indexes.length ||
       c.segment_indexes.some(j=>!Number.isInteger(j) || j<0 || j>=segments.length))) return null;
  // Non-repetition compares the article, not one convenient sentence. Require
  // an actual disposition covering every editorial paragraph; never infer a
  // semantic PASS from coverage or automatically rewrite the saved output.
  const development=editorFindings.indexOf("PROJECT_NONREPETITIVE_DEVELOPMENT");
  if(development>=0){
    const check=audit.finding_checks[development];
    if(check.verdict==="NOT_APPLICABLE" || paragraphs.slice(1).some((_,i)=>!check.segment_indexes.some(j=>segments[j].paragraph_index===i+1)))return null;
  }
  for(const [i,finding] of editorFindings.entries()){
    if(finding.startsWith("PROJECT_") && audit.finding_checks[i].verdict==="NOT_APPLICABLE")return null;
    if(finding==="PROJECT_NO_UNSOURCED_RULE" && paragraphs.slice(1).some((_,p)=>!audit.finding_checks[i].segment_indexes.some(j=>segments[j].paragraph_index===p+1)))return null;
    if(finding==="PROJECT_CLOSING_MESSAGE" && !audit.finding_checks[i].segment_indexes.some(j=>segments[j].paragraph_index===paragraphs.length-1))return null;
  }
  const finding_checks=audit.finding_checks.map((c,i)=>({finding:editorFindings[i],verdict:c.verdict,reason:c.reason,
    segments:c.segment_indexes.map(j=>({index:j,text:segments[j].text}))}));
  return {draft_body:body,finding_checks,issues:[...audit.segments.flatMap(item=>item.issues),
    ...finding_checks.filter(c=>c.verdict==="UNRESOLVED").map(c=>c.finding),
    ...uncitedAssertionIssues(body,segments.flatMap(s=>s.citation?[s.citation]:[]))],paragraphs:paragraphs.map((text,i)=>{
    const items=audit.segments.filter((_,j)=>segments[j].paragraph_index===i);
    return {text,kind:i===0?items[0]?.kind:items.some(item=>item.kind==="SOURCE_FACT")?"SOURCE_FACT":"ADVICE",
      factual_passages:audit.segments.flatMap((item,j)=>segments[j].paragraph_index===i && item.kind==="SOURCE_FACT"?[segments[j].text]:[])};
  })};
}

export function hasHypotheticalOpening(body) {
  return /^(?:Giả sử(?:\s|[.,:;!?])|Tình huống giả định:)/iu.test(body.trim());
}

// A narrow fail-closed check for actual observed audit misses. This does not
// certify source entailment: a cited assertion still needs the separate audit/QA.
export function uncitedAssertionIssues(body, claims=[]) {
  let uncited=body;
  for(const claim of [...claims].sort((a,b)=>b.text.length-a.text.length)) uncited=uncited.replaceAll(claim.text," ");
  const issues=[];
  if(/khả năng[^.!?\n]{0,100}(?:cao|thấp) hơn/iu.test(uncited)) issues.push("Uncited comparative probability assertion");
  if(/luôn[^.!?\n]{0,100}(?:an toàn|tốt|hiệu quả) nhất/iu.test(uncited)) issues.push("Uncited absolute safety or efficacy assertion");
  return issues;
}

export function publicArticleIssues(body) {
  // Observed research-note headings are internal metadata, even with a quote.
  // Reject rather than strip them or relabel the saved automatic output.
  const issues=[];
  if(/(?:^|\n)\s*(?:Hướng biên tập(?: đề xuất)?|Ghi chú nghiên cứu nhập thủ công|Diễn giải phần giới thiệu đã đọc)\s*:/iu.test(body)) issues.push("Internal research-note heading remains in the public Draft");
  if(englishQuestions.test(body) && vietnameseQuestions.test(body)) issues.push("Repeated bilingual source questions remain in the public Draft");
  return issues;
}

// Narrow checks for the actual v31 miss, not a general language/entailment score.
const englishQuestions=/who[’']s behind the information\?\s*what[’']s the evidence\?\s*what do other sources say\?/iu;
const vietnameseQuestions=/ai (?:là người )?đứng sau thông tin[^.\n]{0,120}(?:căn cứ|bằng chứng)[^.\n]{0,120}(?:nguồn khác|những nguồn khác)/iu;
function questionOutcomeIssues(segment){
  const quote=segment.citation?.source_quote;
  if(!quote || !/ba câu hỏi|who[’']s behind the information/iu.test(quote))return [];
  // Observed question-only excerpts do not establish the result claimed in the
  // public sentence. This is a narrow lead/guard, not general entailment proof.
  const result=/(?:giúp|có thể|sẽ|để|nhằm)[^.!?\n]{0,160}(?:đánh giá|xác định|kiểm chứng|xác minh)/iu;
  const recommendation=/khuyến nghị|đề nghị|yêu cầu|nên ghi|ghi lại (?:câu trả lời|kết quả)|recommend|should record/iu;
  return [...(result.test(segment.text) && !result.test(quote)?["Question-only excerpt does not establish the asserted identification or reliability result"]:[]),...(recommendation.test(segment.text) && !recommendation.test(quote)?["Question-only excerpt does not establish the attributed recommendation or recording procedure"]:[])];
}
export function publicCopyRevisions(initial) {
  const repeated=englishQuestions.test(initial.body) && vietnameseQuestions.test(initial.body);
  return initial.segments.flatMap(segment=>{
    const issues=[...publicArticleIssues(segment.text),...questionOutcomeIssues(segment)];
    if(repeated && englishQuestions.test(segment.text) && !issues.some(s=>s.includes("bilingual"))) issues.push("Repeated bilingual source questions remain in the public Draft");
    return issues.length?[{index:segment.index,issues}]:[];
  });
}

export function compileEditedArticle(edited) {
  if (!edited || Object.keys(edited).sort().join(",")!=="editorial_paragraphs,hypothetical_opening" ||
      typeof edited.hypothetical_opening!=="string" || !edited.hypothetical_opening.trim() ||
      edited.hypothetical_opening.includes("\n") || !Array.isArray(edited.editorial_paragraphs) ||
      edited.editorial_paragraphs.length!==4 || edited.editorial_paragraphs.some(p=>typeof p!=="string" || !p.trim() || p.includes("\n"))) {
    throw new Error("Invalid typed editorial structure");
  }
  // Label the explicitly commissioned fictional scene at composition time.
  // This does not label the four editorial paragraphs or license facts in them.
  const opening=edited.hypothetical_opening.trim();
  return [/^Tình huống giả định:/iu.test(opening)?opening:"Tình huống giả định: "+opening,...edited.editorial_paragraphs.map(p=>p.trim())].join("\n\n");
}

// Citations travel with the exact text being composed, not with a second
// model-written copy of a sentence which can drift away from the article.
export function quoteCatalog(materials) {
  return materials.flatMap(m=>m.text.split(/\r?\n/u).map(text=>text.trim()).filter(Boolean).map(source_quote=>({material_id:m.material_id,source_quote})));
}

export function compileSourcedArticle(value, catalog) {
  if (!value || Object.keys(value).sort().join(",")!=="editorial_paragraphs,hypothetical_opening" ||
      !Array.isArray(value.editorial_paragraphs) || value.editorial_paragraphs.length!==4) throw new Error("Invalid sourced article");
  const claims=[],auditable=[];
  const paragraphs=value.editorial_paragraphs.map((segments,paragraphIndex)=>{
    if (!Array.isArray(segments) || !segments.length || segments.length>12) throw new Error("Invalid segments");
    return segments.map(segment=>{
      if (!segment || Object.keys(segment).sort().join(",")!=="source_quote_id,text" ||
          typeof segment.text!=="string" || !segment.text.trim() || segment.text.includes("\n") ||
          !Number.isInteger(segment.source_quote_id) || segment.source_quote_id < -1 ||
          segment.source_quote_id>=catalog.length) throw new Error("Invalid source segment");
      const text=segment.text.trim();
      const citation=segment.source_quote_id>=0?{text,material_ids:[catalog[segment.source_quote_id].material_id],source_quote:catalog[segment.source_quote_id].source_quote}:null;
      auditable.push({paragraph_index:paragraphIndex+1,text,citation});
      if (segment.source_quote_id>=0) {
        claims.push(citation);
      }
      return text;
    }).join(" ");
  });
  const body=compileEditedArticle({hypothetical_opening:value.hypothetical_opening,editorial_paragraphs:paragraphs});
  if (claims.length>30 || claims.some(c=>body.split(c.text).length!==2)) throw new Error("Ambiguous cited passage");
  return {body,claims,segments:[{paragraph_index:0,text:body.split("\n\n")[0]},...auditable].map((segment,index)=>({index,...segment}))};
}

export function validAudit(annotation, audit) {
  const body = annotation.draft_body;
  const paragraphs = body.split(/\n\s*\n/u).map(p=>p.trim()).filter(Boolean);
  if (!audit || Object.keys(audit).sort().join(",")!=="draft_body,finding_checks,issues,paragraphs" ||
      audit.draft_body!==body || !Array.isArray(audit.issues) || audit.issues.length ||
      !Array.isArray(audit.finding_checks) || audit.finding_checks.some(c=>c.verdict==="UNRESOLVED") ||
      !Array.isArray(audit.paragraphs) || audit.paragraphs.length!==paragraphs.length ||
      !Array.isArray(annotation.factual_claims)) return false;
  return audit.paragraphs.every((item,i)=>{
    if (!item || Object.keys(item).sort().join(",")!=="factual_passages,kind,text" ||
        item.text!==paragraphs[i] || !["HYPOTHETICAL","ADVICE","SOURCE_FACT"].includes(item.kind) ||
        (i===0 && item.kind!=="HYPOTHETICAL") || !Array.isArray(item.factual_passages) ||
        (item.kind==="SOURCE_FACT" && !item.factual_passages.length)) return false;
    return item.factual_passages.every(fact=>typeof fact==="string" && fact.trim() &&
      item.text.split(fact).length===2 && annotation.factual_claims.some(claim=>typeof claim.text==="string" && claim.text.includes(fact)));
  });
}
