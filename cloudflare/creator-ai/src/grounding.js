// Source-first generation inputs and explicit edit decisions. These constraints
// do not implement semantic entailment; that still needs real output/source QA.
import {publicCopyRevisions} from './quality.js';

// Keep original catalog indexes stable. Explicit note metadata/editorial
// proposals remain visible as context, but cannot supply a public factual cite.
function factualQuote(quote) {return !/^(?:Ghi chú nghiên cứu nhập thủ công\b|Source\s*:|URL\s*:|Hướng biên tập(?: đề xuất)?\s*:)/iu.test(quote.source_quote.trim());}
export function factualQuoteIds(catalog) {return catalog.flatMap((quote,id)=>factualQuote(quote)?[id]:[]);}

export function sourcedSegmentSchema(catalog) {
  const ids=factualQuoteIds(catalog);
  const branch=id=>({type:"object",properties:{
    source_quote_id:{type:"integer",const:id},text:{type:"string",minLength:1},
    premise_checks:id===-1?{type:"array",maxItems:0,items:{type:"object"}}:
      {type:"array",minItems:1,maxItems:8,items:{type:"object",properties:{assertion:{type:"string",minLength:1},quote_ids:{type:"array",minItems:1,maxItems:1,items:{type:"integer",const:id}},supported:{type:"boolean"}},required:["assertion","quote_ids","supported"],additionalProperties:false}},
  },required:["text","source_quote_id","premise_checks"],additionalProperties:false});
  return {oneOf:[branch(-1),...ids.map(branch)]};
}
export const articleRequirements = [
  {id:"PROJECT_NO_UNSOURCED_RULE",text:"Do not invent an evidence-sufficiency rule, minimum source count, verification procedure or promised result not supplied by the selected snapshots or explicit Project instruction. This also constrains normative ADVICE, independently of factual-premise classification. Voluntary questions, reading and requests without a claimed outcome remain allowed. The model's proposed direction is not permission or source evidence."},
  {id:"PROJECT_CLOSING_MESSAGE",text:"End with an actual short message the reader could send to the original sender asking for the missing original link, date or context. A general instruction to read or verify does not satisfy this deliverable. Write the message, not a description of it."},
  {id:"PROJECT_NONREPETITIVE_DEVELOPMENT",text:"Each editorial paragraph must add a distinct developed reader-facing idea, within the selected source limits. Merely saying to follow the questions above and then decide repeats the preceding advice; source support alone does not make that a complete article. Do not invent detail or add padding to fill four paragraphs. Develop supported questions, explicit information gaps, their limits and the requested sender message coherently. Compare all editorial paragraphs, and in this finding_check map at least one immutable segment from EVERY editorial paragraph. Explain their different contributions. Repetition, generic filler or undeveloped advice is UNRESOLVED, not NOT_APPLICABLE. No minimum word count or invented verification procedure."},
];
export function evidenceInput(sourceContext, catalog, analysis) {
  const data=JSON.parse(sourceContext);
  return {
    project:{purpose:data.purpose,audience:data.audience,style:data.style,direction:data.direction},
    article_requirements:articleRequirements,
    evidence:{authority:"PROVIDED_SNAPSHOT_ONLY",linked_pages_are_not_retrieved:true,
      materials:data.materials.map(m=>({material_id:m.material_id,source_url:m.source_url,scope:m.scope,text:m.text})),
      quote_catalog:catalog.map((quote,id)=>({id,...quote,citation_role:factualQuote(quote)?"SOURCE_EXCERPT":"CONTEXT_NOT_FACTUAL_EVIDENCE"})),
      factual_quote_ids:factualQuoteIds(catalog)},
    editorial_proposal:{direction:analysis.direction,status:"UNVERIFIED_PROPOSAL_NOT_SOURCE_EVIDENCE"},
  };
}

export const sourceFirstInstructions=" Work evidence-first: choose the exact quote ID before wording each factual segment. Its entire meaning, scope and attribution must be supported by the provided snapshot, not an unread linked page. The editorial proposal is an unverified plan, never evidence. Do not promote inferred efficacy or applicability into a fact. If the notes do not supply the needed steps, comparison, recommendation or result, omit that assertion or say explicitly what remains unknown, without asserting the missing detail. A voluntary suggestion may be uncited only if it contains no hidden factual premise or promised outcome. Write fluent connected Vietnamese, not a list of source checks; keep the source decisions out of public prose. The word-count target is subordinate to source limits: never invent facts or pad the article to meet it.";

export function premiseSchema(catalogCount) {
  return {type:"array",maxItems:8,items:{type:"object",properties:{assertion:{type:"string",minLength:1},quote_ids:{type:"array",uniqueItems:true,items:{type:"integer",minimum:0,maximum:catalogCount-1}},supported:{type:"boolean"}},required:["assertion","quote_ids","supported"],additionalProperties:false}};
}

export function checkPremises(premises,catalog) {
  if(!Array.isArray(premises) || premises.length>8 || premises.some(p=>!p || Object.keys(p).sort().join(",")!=="assertion,quote_ids,supported" || typeof p.assertion!=="string" || !p.assertion.trim() || typeof p.supported!=="boolean" || !Array.isArray(p.quote_ids) || new Set(p.quote_ids).size!==p.quote_ids.length || p.quote_ids.some(q=>!Number.isInteger(q) || q<0 || q>=catalog.length) || p.supported && !p.quote_ids.length))throw new Error("Invalid factual premise checks");
}

// This validates declared support, not completeness or semantic entailment.
// The same boundary applies before writing is accepted and to every replacement.
export function checkedSegment(segment,catalog) {
  if(!segment || Object.keys(segment).sort().join(",")!=="premise_checks,source_quote_id,text" || typeof segment.text!=="string" || !segment.text.trim() || segment.text.includes("\n") || !Number.isInteger(segment.source_quote_id) || segment.source_quote_id< -1 || segment.source_quote_id>=catalog.length)throw new Error("Invalid checked segment");
  checkPremises(segment.premise_checks,catalog);
  if(segment.premise_checks.some(p=>!p.supported))throw new Error("Generated segment retains an unsupported factual premise");
  if(segment.source_quote_id===-1 && segment.premise_checks.length)throw new Error("Factual premises cannot be hidden in uncited advice");
  if(segment.source_quote_id>=0 && (!segment.premise_checks.length || segment.premise_checks.some(p=>!p.quote_ids.includes(segment.source_quote_id))))throw new Error("Segment citation does not cover its declared factual premises");
  if(segment.source_quote_id>=0 && (!factualQuote(catalog[segment.source_quote_id]) || segment.premise_checks.some(p=>p.quote_ids.length!==1)))throw new Error("Factual segment must use one eligible source excerpt; note metadata/editorial proposals are context only");
  return {source_quote_id:segment.source_quote_id,text:segment.text};
}

export const generatedPremiseInstructions=" Before wording each segment, enumerate its factual assertions AND presuppositions in premise_checks, including availability/applicability of tools and expected outcomes hidden inside advice. Exact supporting quote IDs are required for each premise. Omit a segment with any unsupported premise; do not merely label it advice. An uncited voluntary request has no factual premises and premise_checks:[]. A cited segment has at least one supported premise, each backed by its source_quote_id. ONE SEGMENT, ONE SUPPORTING EXCERPT: every premise_checks entry in that segment must include its source_quote_id in quote_ids. If two assertions need different excerpts, split them into separate segments with their own citations, or omit the unsupported assertion. Do not bundle an institutional source fact with a note author's editorial instruction under one citation. These are public evidence annotations, not private reasoning. This applies separately to every replacement: support for the original does not establish a new assertion introduced by rewriting.";

export function checkedWriting(value,catalog) {
  if(!value || Object.keys(value).sort().join(",")!=="editorial_paragraphs,hypothetical_opening" || !Array.isArray(value.editorial_paragraphs))throw new Error("Invalid checked writing");
  return {...value,editorial_paragraphs:value.editorial_paragraphs.map(p=>{
    if(!Array.isArray(p))throw new Error("Invalid checked paragraph");
    return p.map(segment=>checkedSegment(segment,catalog));
  })};
}

export function revisionSchema(segmentCount,catalogCount) {
  return {type:"array",minItems:segmentCount,maxItems:segmentCount,items:{type:"object",properties:{
    index:{type:"integer"},
    support:{type:"string",enum:["SOURCE_SUPPORTED","SUGGESTION","HYPOTHETICAL","INSUFFICIENT"]},
    quote_ids:{type:"array",uniqueItems:true,items:{type:"integer",minimum:0,maximum:catalogCount-1}},
    decision:{type:"string",enum:["KEEP","REWRITE","REMOVE"]},
    final_segment_indexes:{type:"array",uniqueItems:true,items:{type:"integer",minimum:0}},
    note:{type:"string",minLength:1}},
    required:["index","support","quote_ids","decision","final_segment_indexes","note"],additionalProperties:false}};
}

export const revisionInstructions=" Check EVERY supplied first-draft segment, including uncited advice. Write the final article first, then fill source_checks in original index order using the actual final segment indexes, not predicted indexes. Each check gives index, support, quote_ids, decision, final_segment_indexes and one short public evidence note, not private reasoning. SOURCE_SUPPORTED requires IDs whose exact excerpts support the entire original claim. SUGGESTION means a voluntary action without factual premise, superiority or promised outcome. HYPOTHETICAL applies only to the labelled fictional opening; it does not license real-world assertions. Otherwise use INSUFFICIENT and REMOVE, or REWRITE as a genuinely different supported narrower statement or an explicitly unknown/conditional passage; never KEEP. quote_ids may identify partial evidence for INSUFFICIENT, but the note must explain what the excerpt does not support and how the replacement limits that claim. SUGGESTION/HYPOTHETICAL use no quote IDs. A revised paragraph that retains the same unsupported assertion is not a correction. For KEEP map the unchanged original text; for REWRITE map the replacement segment(s); for REMOVE return no final indexes. Cover every final segment and recheck new facts against the exact quote catalog. Final factual segments need quote IDs; an insufficient assertion cannot be retained under a different paragraph label. Findings may be false positives: explain a supported passage using exact quotes instead of deleting valid facts. Then produce source_issues and the revised article. End with an actual short message asking the sender for the original link/date/context, rather than offering verification as an unspecified organization.";

export function revisionIssues(initial, final, checks, catalog) {
  const issues=[],covered=new Set();
  if(!Array.isArray(checks) || checks.length!==initial.segments.length) return ["Editing source checks omitted first-draft segments"];
  for(let i=0;i<checks.length;i++){
    const c=checks[i],original=initial.segments[i];
    if(!c || Object.keys(c).sort().join(",")!=="decision,final_segment_indexes,index,note,quote_ids,support" || c.index!==i ||
       !["SOURCE_SUPPORTED","SUGGESTION","HYPOTHETICAL","INSUFFICIENT"].includes(c.support) ||
       !["KEEP","REWRITE","REMOVE"].includes(c.decision) || typeof c.note!=="string" || !c.note.trim() ||
       !Array.isArray(c.quote_ids) || new Set(c.quote_ids).size!==c.quote_ids.length ||
       c.quote_ids.some(id=>!Number.isInteger(id) || id<0 || id>=catalog.length) ||
       !Array.isArray(c.final_segment_indexes) || new Set(c.final_segment_indexes).size!==c.final_segment_indexes.length ||
       c.final_segment_indexes.some(id=>!Number.isInteger(id) || id<0 || id>=final.segments.length)){
      issues.push("Editing source check has invalid identity, evidence or mapping");continue;
    }
    if((c.support==="SOURCE_SUPPORTED" && !c.quote_ids.length) ||
       (["SUGGESTION","HYPOTHETICAL"].includes(c.support) && c.quote_ids.length)) issues.push("Editing support classification and evidence IDs disagree");
    if(c.support==="HYPOTHETICAL" && i!==0) issues.push("Only the opening can use the fictional-scene exemption");
    if(c.support==="INSUFFICIENT" && c.decision==="KEEP") issues.push("Editing retained an assertion it found insufficiently supported");
    if(c.decision==="REMOVE"){
      if(c.final_segment_indexes.length || final.body.includes(original.text)) issues.push("Editing declared removal but retained original text or mapping");
      continue;
    }
    if(!c.final_segment_indexes.length) issues.push("Editing omitted the final replacement mapping");
    if(c.decision==="KEEP" && !c.final_segment_indexes.some(id=>final.segments[id].text===original.text)) issues.push("Editing KEEP changed the original passage");
    if(c.decision==="REWRITE" && final.body.includes(original.text)) issues.push("Editing declared a rewrite but retained the original passage");
    for(const id of c.final_segment_indexes){
      covered.add(id);
      const citation=final.segments[id].citation;
      if(citation && !c.quote_ids.some(q=>catalog[q].material_id===citation.material_ids[0] && catalog[q].source_quote===citation.source_quote)) issues.push("Edited factual passage has no matching evidence in its source check");
    }
  }
  if(final.segments.some(s=>!covered.has(s.index))) issues.push("Editing added final segments without source checks");
  return [...new Set(issues)];
}


// The edit decision owns its replacement text. No second article/mapping copy
// can contradict KEEP/REMOVE or claim that a retained assertion was deleted.
export function operationSchema(count,catalog) {
  const catalogCount=catalog.length;
  const receipt=revisionSchema(count,catalogCount).items;
  const {final_segment_indexes,...properties}=receipt.properties;
  return {type:"object",properties:{source_issues:{type:"array",items:{type:"string"}},edits:{type:"array",minItems:count,maxItems:count,items:{type:"object",properties:{index:properties.index,premise_checks:premiseSchema(catalogCount),...properties,replacement:{type:"array",maxItems:12,items:sourcedSegmentSchema(catalog)}},required:[...receipt.required.filter(key=>key!=="final_segment_indexes"),"premise_checks","replacement"],additionalProperties:false}}},required:["source_issues","edits"],additionalProperties:false};
}

export const operationInstructions="Return source_issues and one edit operation per original segment, in original index order. Each operation assesses the ORIGINAL segment: index, premise_checks, support, quote_ids, decision, note, replacement. Before choosing support/decision, enumerate EVERY factual assertion or presupposition in premise_checks, including implied tool availability, applicability and outcomes inside an instruction or purpose clause. For each assertion, supported:true requires quote_ids whose exact excerpts establish the entire assertion; missing evidence means supported:false, even if the source has a related topic. A general description of a manual does not establish the identity, availability or capability of a particular tool or a case-specific procedure. A pure request to read or ask is a voluntary action; instructing someone to perform an unprovided procedure to establish a result presupposes capabilities and outcomes. If ANY premise is unsupported, choose INSUFFICIENT and REMOVE. An empty premise_checks is allowed only for a pure voluntary action or fictional opening with no real-world factual premise; SOURCE_SUPPORTED requires at least one checked assertion. An editorial warning in a note is advice, not evidence of a universal impossibility. The note is one short evidence explanation, not a reasoning transcript. SOURCE_SUPPORTED needs exact quotes supporting the whole assertion. SUGGESTION is a voluntary action WITHOUT an empirical premise, claimed benefit, ranking or predicted result, including a result hidden in a purpose clause. A question in a source supplies no evidence that using it will produce a reliable answer or better decisions. HYPOTHETICAL is only the explicitly fictional opening. If the original contains an unsupported assertion, use INSUFFICIENT and REMOVE the whole segment: do not retain or paraphrase its unsupported conclusion as advice. For KEEP and REMOVE, replacement MUST be []. KEEP uses the original text/citation exactly; REMOVE emits no text. REWRITE is for a supported original or pure suggestion whose wording needs editing; replacement contains the actual final free-written segments with source_quote_id before text. Do not label an insufficient original SOURCE_SUPPORTED in order to rewrite it. Citations only support source facts, not a source note authors editorial instructions. Preserve original paragraph placement. Remove internal metadata and irrelevant quotations; avoid repeating a source quote in English when a supported Vietnamese explanation already exists. Keep a fluent article, one fictional opening and four connected paragraphs; no minimum length and at most350words. Unconfirmed information remains unconfirmed; no invented procedures or outcomes. Rewrite the ending as an actual short quoted message asking the sender for a missing original link/date/context. Do not output a second article or forecast final indexes; the application composes only the declared operations.";

export function applyEditOperations(value,initial,catalog) {
  if(!value || Object.keys(value).sort().join(",")!=="edits,source_issues" || !Array.isArray(value.edits) || value.edits.length!==initial.segments.length || !Array.isArray(value.source_issues) || value.source_issues.some(s=>typeof s!=="string" || !s.trim())) throw new Error("Invalid edit operations");
  const mandatory=new Set(publicCopyRevisions(initial).map(item=>item.index));
  const paragraphs=[[],[],[],[],[]],pending=[];
  for(let i=0;i<initial.segments.length;i++){
    const original=initial.segments[i],edit=value.edits[i];
    if(!edit || Object.keys(edit).sort().join(",")!=="decision,index,note,premise_checks,quote_ids,replacement,support" || edit.index!==i || !Array.isArray(edit.replacement) || !["KEEP","REMOVE","REWRITE"].includes(edit.decision) || !["SOURCE_SUPPORTED","SUGGESTION","HYPOTHETICAL","INSUFFICIENT"].includes(edit.support))throw new Error("Invalid edit identity");
    checkPremises(edit.premise_checks,catalog);
    if(mandatory.has(i) && edit.decision==="KEEP")throw new Error("Public copy violation requires REWRITE or REMOVE regardless of source support");
    if(edit.premise_checks.some(p=>!p.supported) && (edit.support!=="INSUFFICIENT" || edit.decision!=="REMOVE"))throw new Error("Unsupported factual premise cannot be kept as advice");
    if(edit.support==="SOURCE_SUPPORTED" && !edit.premise_checks.length)throw new Error("Source fact has no checked assertion");
    if(["SUGGESTION","HYPOTHETICAL"].includes(edit.support) && edit.premise_checks.length)throw new Error("Factual premises cannot use an advice or fictional exemption");
    if(edit.support==="SOURCE_SUPPORTED" && edit.premise_checks.some(p=>!p.quote_ids.every(q=>edit.quote_ids.includes(q))))throw new Error("Original premise evidence is missing from the edit receipt");
    if(edit.support==="INSUFFICIENT" && edit.decision!=="REMOVE")throw new Error("Insufficient assertion must be removed, not paraphrased");
    if(edit.decision!=="REWRITE" && edit.replacement.length || edit.decision==="REWRITE" && !edit.replacement.length)throw new Error("Edit replacement contradicts decision");
    const {replacement,premise_checks,...check}=edit;
    const quoteId=original.citation?catalog.findIndex(q=>q.material_id===original.citation.material_ids[0] && q.source_quote===original.citation.source_quote):-1;
    const produced=edit.decision==="REMOVE"?[]:edit.decision==="KEEP"?[{text:original.text,source_quote_id:quoteId}]:replacement.map(segment=>checkedSegment(segment,catalog));
    if(edit.decision==="REWRITE" && produced.map(s=>s.text.trim()).join(" ").normalize("NFC").replace(/\s+/gu," ")===original.text.trim().normalize("NFC").replace(/\s+/gu," "))throw new Error("REWRITE did not change the original passage");
    for(const segment of produced){
      if(!segment || Object.keys(segment).sort().join(",")!=="source_quote_id,text" || typeof segment.text!=="string" || !segment.text.trim() || segment.text.includes("\n") || !Number.isInteger(segment.source_quote_id) || segment.source_quote_id< -1 || segment.source_quote_id>=catalog.length)throw new Error("Invalid edit replacement");
      if(i===0 && segment.source_quote_id!==-1)throw new Error("Fictional opening cannot acquire a citation");
      if(segment.source_quote_id>=0 && !factualQuote(catalog[segment.source_quote_id]))throw new Error("Edited factual citation cannot use note metadata/editorial context");
      paragraphs[original.paragraph_index].push({segment,original_index:i});
    }
    pending.push(check);
  }
  if(paragraphs[0].length!==1 || paragraphs.slice(1).some(p=>!p.length))throw new Error("Editing omitted a required paragraph");
  const mapped=pending.map(check=>({...check,final_segment_indexes:[]}));
  let index=0;
  for(const paragraph of paragraphs)for(const item of paragraph)mapped[item.original_index].final_segment_indexes.push(index++);
  const sourceIssues=[...new Set([...value.source_issues,...value.edits.filter(edit=>edit.support==="INSUFFICIENT" && typeof edit.note==="string" && edit.note.trim()).map(edit=>edit.note)])];
  return {source_issues:sourceIssues,source_checks:mapped,hypothetical_opening:paragraphs[0][0].segment.text,editorial_paragraphs:paragraphs.slice(1).map(p=>p.map(item=>item.segment))};
}
