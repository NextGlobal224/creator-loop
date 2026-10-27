import test from 'node:test';
import assert from 'node:assert/strict';
import {auditSchema,bindAudit} from '../src/quality.js';

// Protocol coverage, not a simulated content-quality PASS.
const segments=[
 {index:0,paragraph_index:0,text:'Mở đầu.',citation:null},
 {index:1,paragraph_index:1,text:'Phần tổng hợp.',citation:null},
 {index:2,paragraph_index:1,text:'Lời mời.',citation:null},
];
const body='Mở đầu.\n\nPhần tổng hợp. Lời mời.';
const findings=['PROJECT_CLOSING_MESSAGE'];
function audit(indexes,verdict){return {article_sha256:'fixed',segments:segments.map(s=>({index:s.index,kind:'ADVICE',issues:[]})),finding_checks:[{index:0,verdict,segment_indexes:indexes,reason:'Coverage fixture only, no semantic approval.'}]};}

test('native closing audit binds the entire immutable closing paragraph for either verdict',()=>{
 const schema=auditSchema('fixed',3,1,findings,segments);
 for(const branch of schema.properties.finding_checks.items.oneOf)assert.deepEqual(branch.properties.segment_indexes.const,[1,2]);
 assert.throws(()=>auditSchema('fixed',3,1,findings,[...segments].reverse()),/paragraph identity/);
});

test('last-sentence-only and earlier-sentence-only closing audits are rejected without inventing a verdict',()=>{
 for(const verdict of ['ADDRESSED','UNRESOLVED']){
  for(const indexes of [[1],[2]])assert.equal(bindAudit(body,'fixed',audit(indexes,verdict),segments,findings),null);
  const complete=bindAudit(body,'fixed',audit([1,2],verdict),segments,findings);
  assert.ok(complete);assert.equal(complete.finding_checks[0].verdict,verdict);
  assert.deepEqual(complete.issues,verdict==='UNRESOLVED'?findings:[]);
 }
});
