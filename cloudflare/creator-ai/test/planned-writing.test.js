import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {editorialRoles,plannedWritingSchema,plannedWriting,checkedWriting} from '../src/grounding.js';
import {bindAudit,auditSchema,publicCopyRevisions} from '../src/quality.js';

test('named editorial contributions order exact model prose without filling, stripping or accepting legacy arrays',()=>{
  const paragraphs=editorialRoles.map((r,i)=>[{text:`Synthetic independent contribution ${i}.`,source_quote_id:-1,premise_checks:[]}]);
  const value={hypothetical_opening:'Tình huống giả định: một tin nhắn.',editorial_paragraphs:Object.fromEntries([...editorialRoles].reverse().map(r=>[r.key,paragraphs[editorialRoles.indexOf(r)]]))};
  const before=JSON.stringify(value),normalized=plannedWriting(value);
  assert.deepEqual(normalized.editorial_paragraphs,paragraphs);
  assert.deepEqual(checkedWriting(normalized,[]).editorial_paragraphs.map(p=>p[0].text),paragraphs.map(p=>p[0].text));
  assert.equal(JSON.stringify(value),before);
  assert.throws(()=>plannedWriting({...value,editorial_paragraphs:paragraphs}),/named editorial roles/);
  const missing=structuredClone(value);delete missing.editorial_paragraphs.unresolved_information;
  assert.throws(()=>plannedWriting(missing),/named editorial roles/);
  const extra=structuredClone(value);extra.editorial_paragraphs.source_overview=[];
  assert.throws(()=>plannedWriting(extra),/named editorial roles/);
  const empty=structuredClone(value);empty.editorial_paragraphs.unresolved_information=[];
  assert.throws(()=>checkedWriting(plannedWriting(empty),[]));
  assert.deepEqual(plannedWritingSchema([]).properties.editorial_paragraphs.required,editorialRoles.map(r=>r.key));
});

test('actual ccca4a6 audit cannot waive source requirements or certify disputed paragraphs using one safe paragraph',()=>{
  const actual=JSON.parse(readFileSync(new URL('../../../tests/fixtures/workers_b_ccca4a6_misattributed_advice.json',import.meta.url),'utf8'));
  const before=JSON.stringify(actual),hash=createHash('sha256').update(actual.body).digest('hex');
  const segments=actual.audit.paragraphs.map((p,index)=>({index,paragraph_index:index,text:p.text,citation:actual.claims.find(c=>c.text===p.text)??null}));
  const findings=actual.audit.finding_checks.map(c=>c.finding);
  const raw={article_sha256:hash,segments:actual.audit.paragraphs.map((p,index)=>({index,kind:p.kind,issues:[]})),finding_checks:actual.audit.finding_checks.map((c,index)=>({index,verdict:c.verdict,reason:c.reason,segment_indexes:c.segments.map(s=>s.index)}))};
  const index=findings.indexOf('PROJECT_NO_UNSOURCED_RULE');assert.equal(raw.finding_checks[index].verdict,'NOT_APPLICABLE');
  assert.equal(bindAudit(actual.body,hash,raw,segments,findings),null);
  raw.finding_checks[index].verdict='ADDRESSED';
  assert.equal(bindAudit(actual.body,hash,raw,segments,findings),null,'Still lacks all-paragraph source coverage');
  const revisions=publicCopyRevisions({body:actual.body,segments});
  assert.ok(revisions.some(r=>r.index===2 && r.issues.some(s=>s.includes('attributed recommendation'))));
  assert.ok(revisions.some(r=>r.index===3 && r.issues.some(s=>s.includes('recording procedure'))));
  const branches=auditSchema(hash,segments.length,findings.length,findings).properties.finding_checks.items.oneOf.filter(b=>b.properties.index.const===index);
  assert.deepEqual(branches.map(b=>b.properties.verdict.const),['ADDRESSED','UNRESOLVED']);
  for(const branch of branches){
    assert.equal(branch.properties.segment_indexes.minItems,segments.length);
    assert.deepEqual(branch.properties.segment_indexes.const,segments.map(s=>s.index));
  }
  assert.equal(JSON.stringify(actual),before,'Actual automatic failure and false-empty audit are never rewritten into a PASS');
});
