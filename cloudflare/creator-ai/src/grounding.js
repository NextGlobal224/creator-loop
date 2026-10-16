// Source-first generation inputs and explicit edit decisions. These constraints
// do not implement semantic entailment; that still needs real output/source QA.
export function evidenceInput(sourceContext, catalog, analysis) {
  const data=JSON.parse(sourceContext);
  return {
    project:{purpose:data.purpose,audience:data.audience,style:data.style,direction:data.direction},
    evidence:{authority:"PROVIDED_SNAPSHOT_ONLY",linked_pages_are_not_retrieved:true,
      materials:data.materials.map(m=>({material_id:m.material_id,source_url:m.source_url,scope:m.scope,text:m.text})),
      quote_catalog:catalog.map((quote,id)=>({id,...quote}))},
    editorial_proposal:{direction:analysis.direction,status:"UNVERIFIED_PROPOSAL_NOT_SOURCE_EVIDENCE"},
  };
}

export const sourceFirstInstructions=" Work evidence-first: choose the exact quote ID before wording each factual segment. Its entire meaning, scope and attribution must be supported by the provided snapshot, not an unread linked page. The editorial proposal is an unverified plan, never evidence. Do not promote inferred efficacy or applicability into a fact. If the notes do not supply the needed steps, comparison, recommendation or result, omit that assertion or say explicitly what remains unknown, without asserting the missing detail. A voluntary suggestion may be uncited only if it contains no hidden factual premise or promised outcome. Write fluent connected Vietnamese, not a list of source checks; keep the source decisions out of public prose. The word-count target is subordinate to source limits: never invent facts or pad the article to meet it.";

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

export const revisionInstructions=" Before rewriting, fill source_checks for EVERY supplied first-draft segment in original index order, including uncited advice. Each check gives index, support, quote_ids, decision, final_segment_indexes and one short public evidence note, not private reasoning. SOURCE_SUPPORTED requires IDs whose exact excerpts support the entire original claim. SUGGESTION means a voluntary action without factual premise, superiority or promised outcome. HYPOTHETICAL applies only to the labelled fictional opening; it does not license real-world assertions. Otherwise use INSUFFICIENT and REMOVE, or REWRITE as a genuinely different supported narrower statement or an explicitly unknown/conditional passage; never KEEP. quote_ids may identify partial evidence for INSUFFICIENT, but the note must explain what the excerpt does not support and how the replacement limits that claim. SUGGESTION/HYPOTHETICAL use no quote IDs. A revised paragraph that retains the same unsupported assertion is not a correction. For KEEP map the unchanged original text; for REWRITE map the replacement segment(s); for REMOVE return no final indexes. Cover every final segment and recheck new facts against the exact quote catalog. Final factual segments need quote IDs; an insufficient assertion cannot be retained under a different paragraph label. Findings may be false positives: explain a supported passage using exact quotes instead of deleting valid facts. Then produce source_issues and the revised article. End with an actual short message asking the sender for the original link/date/context, rather than offering verification as an unspecified organization.";

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
