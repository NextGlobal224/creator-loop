import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {evidenceInput,revisionIssues,applyEditOperations,checkedSegment,checkedWriting,factualQuoteIds,sourcedSegmentSchema} from '../src/grounding.js';
import {compileSourcedArticle,quoteCatalog} from '../src/quality.js';

test('generation evidence is exact snapshots; model inference is not promoted into the evidence pack',()=>{
  const materials=[{material_id:'s1',source_url:'https://example.test/book',scope:'Introduction only',text:'Only a general handbook introduction is supplied.'}];
  const data={purpose:'Explain limits',audience:'Readers',style:'Natural',direction:'User-selected angle',materials};
  const analysis={observations:[{observation:'MODEL_OBSERVATION_NOT_A_SOURCE'}],inferences:['MODEL_EFFICACY_GUESS'],direction:'Proposed angle'};
  const catalog=quoteCatalog(materials),pack=evidenceInput(JSON.stringify(data),catalog,analysis);
  assert.deepEqual(pack.evidence.materials,materials);
  assert.equal(pack.evidence.quote_catalog[0].source_quote,materials[0].text);
  assert.equal(pack.evidence.linked_pages_are_not_retrieved,true);
  assert.equal(pack.editorial_proposal.status,'UNVERIFIED_PROPOSAL_NOT_SOURCE_EVIDENCE');
  assert.equal(pack.project.direction,data.direction);
  assert.equal(JSON.stringify(pack).includes('MODEL_EFFICACY_GUESS'),false);
  assert.equal(JSON.stringify(pack).includes('MODEL_OBSERVATION_NOT_A_SOURCE'),false);
});

const catalog=[{material_id:'s',source_quote:'The supplied note describes an introduction, without case-specific procedures.'}];
function article(text){
  return compileSourcedArticle({hypothetical_opening:'A reader receives an incomplete record.',editorial_paragraphs:[text,'Ask for the original document.','Read the supplied excerpt.','If information is absent, leave it unconfirmed.'].map(text=>[{text,source_quote_id:-1}])},catalog);
}
function checks(initial,final){
  return initial.segments.map((s,index)=>({index,support:index===0?'HYPOTHETICAL':'SUGGESTION',quote_ids:[],decision:s.text===final.segments[index].text?'KEEP':'REWRITE',final_segment_indexes:[index],note:'Synthetic receipt for a structural check, not model quality evidence.'}));
}
test('an acknowledged unsupported passage cannot be kept or declared rewritten without a change',()=>{
  const initial=article('This handbook specifies a procedure applicable to every incomplete record.');
  const same=article(initial.segments[1].text),receipt=checks(initial,same);
  receipt[1].support='INSUFFICIENT';
  assert.ok(revisionIssues(initial,same,receipt,catalog).some(s=>s.includes('insufficiently supported')));
  receipt[1].decision='REWRITE';
  assert.ok(revisionIssues(initial,same,receipt,catalog).some(s=>s.includes('retained the original passage')));
  receipt[1].decision='REMOVE';receipt[1].final_segment_indexes=[];
  assert.ok(revisionIssues(initial,same,receipt,catalog).length);
  const revised=article('The supplied introduction does not provide the procedure; ask for the relevant passage.');
  const fixed=checks(initial,revised);fixed[1].support='INSUFFICIENT';
  assert.deepEqual(revisionIssues(initial,revised,fixed,catalog),[]);
  // Only verifies decisions/mappings; no semantic verdict is inferred from [].
  const narrow=compileSourcedArticle({hypothetical_opening:'A reader receives an incomplete record.',editorial_paragraphs:[
    [{text:catalog[0].source_quote,source_quote_id:0}],
    ...revised.body.split('\n\n').slice(2).map(text=>[{text,source_quote_id:-1}])]},catalog);
  const narrowed=checks(initial,narrow);narrowed[1].support='INSUFFICIENT';narrowed[1].quote_ids=[0];
  assert.deepEqual(revisionIssues(initial,narrow,narrowed,catalog),[]);
  narrowed[1].quote_ids=[];
  assert.ok(revisionIssues(initial,narrow,narrowed,catalog).some(s=>s.includes('no matching evidence')));
});
test('edit decisions must cover original/final text and valid evidence identities',()=>{
  const initial=article('Ask which part of the source is missing.'),receipt=checks(initial,initial);
  assert.deepEqual(revisionIssues(initial,initial,receipt,catalog),[]);
  const invalid=[receipt.slice(1),receipt.map(c=>({...c,index:0}))];
  for(const change of [{quote_ids:[44],support:'SOURCE_SUPPORTED'},{quote_ids:[],support:'SOURCE_SUPPORTED'},{support:'HYPOTHETICAL'},{final_segment_indexes:[99]},{note:' '}]){
    const c=structuredClone(receipt);Object.assign(c[1],change);invalid.push(c);
  }
  for(const value of invalid)assert.ok(revisionIssues(initial,initial,value,catalog).length);
});
test('the saved failed article has no v28 edit decisions and is not silently upgraded',()=>{
  const saved=JSON.parse(readFileSync(new URL('../../../tests/fixtures/workers_b_v25_editor_findings.json',import.meta.url),'utf8'));
  const before=JSON.stringify(saved);
  const initial=compileSourcedArticle({hypothetical_opening:saved.edited.hypothetical_opening,editorial_paragraphs:saved.edited.editorial_paragraphs},saved.catalog);
  assert.ok(revisionIssues(initial,initial,saved.edited.source_checks,saved.catalog).length);
  // Analyst-labelled regression only: the original model supplied no decisions.
  const receipt=checks(initial,initial),uncited=initial.segments.find(s=>s.index>0&&!s.citation);
  receipt[uncited.index].support='INSUFFICIENT';
  assert.ok(revisionIssues(initial,initial,receipt,saved.catalog).length);
  assert.equal(JSON.stringify(saved),before);
});

test('duplicate edit evidence and final-index receipts remain rejected locally',()=>{
  const initial=article('Ask which part of the source is missing.');
  for(const duplicate of [{quote_ids:[0,0],support:'SOURCE_SUPPORTED'},{final_segment_indexes:[1,1]}]){
    const receipt=checks(initial,initial);Object.assign(receipt[1],duplicate);
    assert.ok(revisionIssues(initial,initial,receipt,catalog).length);
  }
});

test('edit operations bind KEEP/REMOVE/REWRITE to a single text and compute shifted indexes',()=>{
  const initial=article('A comparison gives a guaranteed result.');
  // Add a safe action to that paragraph so removal need not invent filler.
  const typed={hypothetical_opening:initial.segments[0].text,editorial_paragraphs:[
    [{text:initial.segments[1].text,source_quote_id:-1},{text:'Ask for the missing record.',source_quote_id:-1}],
    ...initial.segments.slice(2).map(s=>[{text:s.text,source_quote_id:-1}])]};
  const original=compileSourcedArticle(typed,catalog);
  const edits=original.segments.map((s,index)=>({index,support:index===0?'HYPOTHETICAL':'SUGGESTION',quote_ids:[],decision:'KEEP',note:'Structural test only',premise_checks:[],replacement:[]}));
  edits[1]={...edits[1],support:'INSUFFICIENT',decision:'REMOVE'};
  edits[edits.length-1]={...edits.at(-1),decision:'REWRITE',replacement:[{source_quote_id:-1,text:'Please send the original link and date.',premise_checks:[]}]};
  const output=applyEditOperations({source_issues:['Unsupported outcome removed'],edits},original,catalog);
  const emptySummary=applyEditOperations({source_issues:[],edits},original,catalog);
  assert.deepEqual(emptySummary.source_issues,[edits[1].note], 'The audit must receive the actual insufficient-support finding even when the summary omits it');
  const final=compileSourcedArticle({hypothetical_opening:output.hypothetical_opening,editorial_paragraphs:output.editorial_paragraphs},catalog);
  assert.ok(!final.body.includes(original.segments[1].text));assert.ok(final.body.includes(original.segments[2].text));
  assert.deepEqual(output.source_checks[1].final_segment_indexes,[]);assert.deepEqual(output.source_checks[2].final_segment_indexes,[1]);
  assert.deepEqual(revisionIssues(original,final,output.source_checks,catalog),[]);
  for(const change of [{decision:'KEEP'},{decision:'REWRITE',replacement:[{text:'The same conclusion in different words.',source_quote_id:-1}]}]){
    const invalid=structuredClone(edits);Object.assign(invalid[1],change);
    assert.throws(()=>applyEditOperations({source_issues:[],edits:invalid},original,catalog),/Insufficient/);
  }
  const invalid=structuredClone(edits);invalid[2].replacement=[{text:'Silently changed KEEP',source_quote_id:-1}];
  assert.throws(()=>applyEditOperations({source_issues:[],edits:invalid},original,catalog),/contradicts/);
});

test('an acknowledged unsupported premise cannot survive under a suggestion label',()=>{
  const initial=article('Run the unprovided procedure to establish the result.');
  const edits=initial.segments.map((s,index)=>({index,support:index===0?'HYPOTHETICAL':'SUGGESTION',quote_ids:[],decision:'KEEP',note:'Structural test',premise_checks:[],replacement:[]}));
  edits[1].premise_checks=[{assertion:'An identified procedure establishes the result.',quote_ids:[],supported:false}];
  assert.throws(()=>applyEditOperations({source_issues:[],edits},initial,catalog),/Unsupported factual premise/);
  edits[1].support='SOURCE_SUPPORTED';edits[1].quote_ids=[0];
  assert.throws(()=>applyEditOperations({source_issues:[],edits},initial,catalog),/Unsupported factual premise/);
});

test('affirmed factual premises cannot be exempted as advice or omitted from original edit evidence',()=>{
  const initial=article('The excerpt describes a handbook.');
  const edits=initial.segments.map((s,index)=>({index,support:index===0?'HYPOTHETICAL':'SUGGESTION',quote_ids:[],decision:'KEEP',note:'Protocol fixture only',premise_checks:[],replacement:[]}));
  edits[1].premise_checks=[{assertion:initial.segments[1].text,quote_ids:[0],supported:true}];
  assert.throws(()=>applyEditOperations({source_issues:[],edits},initial,catalog),/exemption/);
  edits[1].support='SOURCE_SUPPORTED';
  assert.throws(()=>applyEditOperations({source_issues:[],edits},initial,catalog),/missing from the edit receipt/);
});

test('replacement premises are checked independently from a supported original',()=>{
  const initial=article('Ask for the original record.');
  const edits=initial.segments.map((s,index)=>({index,support:index===0?'HYPOTHETICAL':'SUGGESTION',quote_ids:[],decision:'KEEP',note:'Protocol fixture only',premise_checks:[],replacement:[]}));
  edits[1].decision='REWRITE';
  edits[1].replacement=[{text:'Applying the unspecified procedure establishes a reliable result.',source_quote_id:-1,premise_checks:[{assertion:'The procedure establishes that result.',quote_ids:[],supported:false}]}];
  assert.throws(()=>applyEditOperations({source_issues:[],edits},initial,catalog),/unsupported factual premise/);
  edits[1].replacement=[{text:'Please send the original record and its date.',source_quote_id:-1,premise_checks:[]}];
  assert.ok(applyEditOperations({source_issues:[],edits},initial,catalog).editorial_paragraphs[0][0].text.includes('date'));
  edits[1].replacement=[{text:initial.segments[1].text,source_quote_id:-1,premise_checks:[]}];
  assert.throws(()=>applyEditOperations({source_issues:[],edits},initial,catalog),/did not change/);
});

test('writer and replacement citations must cover all declared factual premises',()=>{
  const sources=[...catalog,{material_id:'other',source_quote:'A different excerpt.'}];
  const segment={text:'A factual passage.',source_quote_id:0,premise_checks:[{assertion:'A factual passage.',quote_ids:[1],supported:true}]};
  assert.throws(()=>checkedSegment(segment,sources),/does not cover/);
  segment.premise_checks[0].quote_ids=[0];
  assert.deepEqual(checkedSegment(segment,sources),{text:segment.text,source_quote_id:0});
  // A structurally consistent declaration is not proof the excerpt entails it.
  segment.source_quote_id=-1;
  assert.throws(()=>checkedSegment(segment,sources),/uncited advice/);
});

test('actual 1725b01 output cannot bind two different premises to a single quote',()=>{
  const actual=JSON.parse(readFileSync(new URL('../../../tests/fixtures/workers_b_1725b01_mixed_quotes.json',import.meta.url),'utf8'));
  const before=JSON.stringify(actual);
  assert.throws(()=>checkedWriting(actual.writing,actual.catalog),/Segment citation does not cover its declared factual premises/);
  const paragraph=actual.writing.editorial_paragraphs[2];
  assert.equal(paragraph[0].source_quote_id,8);
  assert.deepEqual(paragraph[0].premise_checks.map(p=>p.quote_ids),[[8],[4]]);
  assert.equal(JSON.stringify(actual),before,'Replay must not split or rewrite actual model prose');
});

test('actual340006b unsupported paragraph removal cannot become an incomplete article',()=>{
  const actual=JSON.parse(readFileSync(new URL('../../../tests/fixtures/workers_b_340006b_removed_paragraph.json',import.meta.url),'utf8'));
  const before=JSON.stringify(actual);
  assert.throws(()=>checkedWriting(actual.writing,actual.catalog),/eligible source excerpt/);
  const initial=compileSourcedArticle({...actual.writing,editorial_paragraphs:actual.writing.editorial_paragraphs.map(p=>p.map(({premise_checks,...segment})=>segment))},actual.catalog);
  assert.equal(actual.edits.edits[3].support,'INSUFFICIENT');assert.equal(actual.edits.edits[3].decision,'REMOVE');
  assert.throws(()=>applyEditOperations(actual.edits,initial,actual.catalog),/note metadata\/editorial context/);
  assert.equal(JSON.stringify(actual),before,'Keep both actual model outputs unchanged');
});

test('selected note metadata and editorial proposals stay visible but cannot become factual citations',()=>{
  const actual=JSON.parse(readFileSync(new URL('../../../tests/fixtures/workers_b_d7315a2_mixed_context.json',import.meta.url),'utf8'));
  assert.deepEqual(factualQuoteIds(actual.catalog),[3,8,9]);
  const body=JSON.stringify(actual);
  assert.throws(()=>checkedWriting(actual.writing,actual.catalog),/does not cover/);
  for(const id of [0,1,2,4,5,6,7,10]){
    const segment={source_quote_id:id,text:'A claimed institutional fact.',premise_checks:[{assertion:'A claimed institutional fact.',quote_ids:[id],supported:true}]};
    assert.throws(()=>checkedSegment(segment,actual.catalog),/eligible source excerpt/);
  }
  assert.equal(JSON.stringify(actual),body,'Neither catalog IDs nor actual text are repaired');
  const materials=[{material_id:'s',text:'Hướng biên tập: a suggestion.\nA source excerpt.'}];
  const pack=evidenceInput(JSON.stringify({materials}),quoteCatalog(materials),{direction:'Unverified'});
  assert.equal(pack.evidence.materials[0].text,materials[0].text);
  assert.equal(pack.evidence.quote_catalog[0].citation_role,'CONTEXT_NOT_FACTUAL_EVIDENCE');
  assert.deepEqual(pack.evidence.factual_quote_ids,[1]);
});

test('disjoint segment schema binds every premise to exactly its selected eligible excerpt',()=>{
  const catalog=[{material_id:'s',source_quote:'Source: identity only'},{material_id:'s',source_quote:'A public source statement.'}];
  const schema=sourcedSegmentSchema(catalog);
  assert.deepEqual(schema.oneOf.map(b=>b.properties.source_quote_id.const),[-1,1]);
  assert.equal(schema.oneOf[0].properties.premise_checks.maxItems,0);
  const premises=schema.oneOf[1].properties.premise_checks;
  assert.equal(premises.minItems,1);assert.equal(premises.items.properties.quote_ids.maxItems,1);
  assert.equal(premises.items.properties.quote_ids.items.const,1);
  const segment={source_quote_id:1,text:'A public source statement.',premise_checks:[{assertion:'A public source statement.',quote_ids:[1],supported:true}]};
  assert.equal(checkedSegment(segment,catalog).text,segment.text);
  segment.premise_checks[0].quote_ids=[0,1];assert.throws(()=>checkedSegment(segment,catalog),/one eligible/);
  segment.premise_checks[0].quote_ids=[1];segment.premise_checks[0].supported=false;assert.throws(()=>checkedSegment(segment,catalog),/unsupported factual premise/);
});

test('removing a whole required paragraph still fails after adding citation roles',()=>{
  const initial=article('An unsupported procedure.');
  const edits=initial.segments.map((segment,index)=>({index,support:index===0?'HYPOTHETICAL':'SUGGESTION',quote_ids:[],decision:'KEEP',premise_checks:[],replacement:[],note:'Synthetic protocol case only'}));
  edits[3].support='INSUFFICIENT';edits[3].decision='REMOVE';
  assert.throws(()=>applyEditOperations({source_issues:[],edits},initial,catalog),/Editing omitted a required paragraph/);
});
