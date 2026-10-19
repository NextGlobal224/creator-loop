// A readable contract is not a second JSON Schema. The full machine schema,
// source pack, SDK validation and downstream domain guards remain unchanged.
export const READABLE_CONTRACT_MARKER='\nReturn one JSON object. The complete JSON Schema is attached as response_format. Readable shape summary (not another schema):\n';
const schemaMarker='\nReturn only JSON matching this schema:\n';

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
