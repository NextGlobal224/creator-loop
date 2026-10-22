// A readable contract is not a second JSON Schema. The full machine schema,
// source pack, SDK validation and downstream domain guards remain unchanged.
export const READABLE_CONTRACT_MARKER='\nReturn one JSON object. The complete JSON Schema is attached as response_format. Readable shape summary (not another schema):\n';
const schemaMarker='\nReturn only JSON matching this schema:\n';

// MANUAL analysis.direction is the exact selected brief, not an AI proposal.
// Replace the inherited conflicting clauses instead of appending an override.
export function bindSelectedManualBrief(native,packet,source){
 if(packet.mode!=='MANUAL'||!['TEXT','IMAGE_CAPTION'].includes(packet.format)||!['writing','editing'].includes(packet.stage))return false;
 const system=native.messages.find(m=>m.role==='system'&&typeof m.content==='string');
 if(!system||typeof source.direction!=='string'||!source.direction.trim())throw Error('Selected manual brief missing');
 if(packet.stage==='writing'){
  const clauses=[['The generated analysis direction is NOT evidence or a mandatory procedure.','The selected MANUAL direction is the required editorial brief, never source evidence or permission to invent a factual procedure.']];
  if(packet.format==='TEXT')clauses.push(['The analysis direction is only a creative proposal, never selected instructions or source evidence.','The MANUAL analysis direction preserves the user-selected editorial brief; follow it within all source constraints, never treat it as factual evidence.']);
  for(const [old,replacement] of clauses){
   if(!system.content.includes(old))throw Error('Expected inherited direction clause missing');
   system.content=system.content.replace(old,replacement);
  }
 }
 native.messages.push({role:'user',content:'SELECTED MANUAL EDITORIAL BRIEF (authenticated user choices, not source evidence; factual/source rules still apply):\n'+JSON.stringify({purpose:source.purpose,audience:source.audience,style:source.style,direction:source.direction})});
 if(packet.stage==='editing')system.content+=' Operation semantics: if an original passage is both supported and usable for this brief, KEEP it with replacement:[]. A REWRITE cannot copy that original passage anywhere into its replacements, including copying a supported sentence and then appending advice; change the passage itself, or keep it and avoid an unnecessary addition. REMOVE only where the existing paragraph/mandatory-revision rules allow. Judge source support separately from relevance: a supported publication date can still be irrelevant as the opening. An uncited voluntary request with no factual premise or promised outcome is SUGGESTION, not INSUFFICIENT solely for lacking a source; outcome/tool/institution claims still need exact evidence. If the brief asks for a sender message, write that actual message once at the end, not repeated advice describing a request.';
 return true;
}

export function compactStagePrompt(native,stage){
 const message=native.messages.find(m=>m.role==='user'&&typeof m.content==='string'&&m.content.includes(schemaMarker));
 if(!message)return false; // Direct profile fixtures can omit SDK wrappers.
 const at=message.content.lastIndexOf(schemaMarker),original=JSON.parse(message.content.slice(at+schemaMarker.length)),schema=native.response_format?.json_schema;
 let expected,fields;
 const segment={text:'Connected natural Vietnamese prose; every factual meaning needs exact selected-source support.',source_quote_id:'Exact eligible quote catalog ID, or -1 only for voluntary advice/fiction without real-world premises.',premise_checks:'Enumerate every factual assertion: assertion, exact quote_ids, supported. Unsupported premises cannot be presented as facts.'};
 if(stage==='writing'){
  const opening=Object.hasOwn(schema?.properties??{},'hypothetical_opening')?'hypothetical_opening':'opening';
  expected=[opening,'editorial_paragraphs'].sort().join();
  const roles=schema?.properties?.editorial_paragraphs?.properties;
  if(!roles||Object.keys(roles).length!==4)throw Error('Unexpected writing roles');
  fields={
   [opening]:opening==='hypothetical_opening'?'One explicitly commissioned fictional scene beginning Tình huống giả định:, with no unsupported real-world premise.':{shape:'Array of source-checked segments',items:segment,purpose:'An opening appropriate to the selected Project. Explicitly labelled fiction only when the brief permits; preserve factual/no-fiction requests.'},
   editorial_paragraphs:Object.fromEntries(Object.entries(roles).map(([key,value])=>[key,{shape:'Array of segments composing one connected paragraph',items:segment,purpose:value.description}]))
  };
 }else if(stage==='editing'){
  expected='edits,source_issues';
  fields={source_issues:'Array of strings: unresolved problems in the original article, including relevance/development and factual premises.',edits:{shape:'Array with one operation per original segment',one_per_original_segment_in_order:{index:'Original segment index.',premise_checks:segment.premise_checks,support:'SOURCE_SUPPORTED, INSUFFICIENT, SUGGESTION or HYPOTHETICAL with the original contract distinctions.',quote_ids:'Array of exact catalog IDs for the ORIGINAL assessment.',decision:'KEEP, REWRITE or allowed REMOVE; mandatory revisions cannot KEEP.',note:'Explain the original assessment and actual decision.',replacement:{shape:'Array of checked replacement segments',items:segment,rule:'KEEP/REMOVE have no replacements. REWRITE must actually change the original; the full native schema excludes exact-copy replacements and runtime also rejects normalized no-ops.'}}}};
 }else throw Error('Unsupported compact prompt stage');
 if(original.type!=='object'||original.additionalProperties!==false||Object.keys(original.properties??{}).sort().join()!==expected||[...(original.required??[])].sort().join()!==expected||schema.type!=='object'||schema.additionalProperties!==false||Object.keys(schema.properties??{}).sort().join()!==expected||[...(schema.required??[])].sort().join()!==expected)throw Error('Unexpected original SDK stage schema');
 const contract={constraint_authority:'Full response_format JSON Schema; all exact field, source, receipt and choice constraints remain enforced.',fields};
 if(stage==='writing')contract.development='Follow the actual brief and each existing role purpose. Develop distinct connected contributions: a relevant supported idea and its explanation, condition, concrete illustration or limitation where the brief calls for it. One-line source summaries are not developed paragraphs. A concise brief or closing message may stay concise; do not add facts, padding, repeated questions or filler to reach a segment/word count.';
 message.content=message.content.slice(0,at)+READABLE_CONTRACT_MARKER+JSON.stringify(contract);
 return true;
}
