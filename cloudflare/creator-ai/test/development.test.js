import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {articleRequirements,checkedWriting} from '../src/grounding.js';
import {quoteCatalog,compileSourcedArticle,bindAudit,validAudit} from '../src/quality.js';
const actual=JSON.parse(readFileSync(new URL('../../../tests/fixtures/workers_b_f103545_repetition.json',import.meta.url),'utf8'));

test('actual f103545 empty audit cannot substitute for all-paragraph development coverage',()=>{
  const catalog=quoteCatalog(actual.materials);
  assert.throws(()=>checkedWriting(actual.written,catalog),/eligible source excerpt/);
  // Recompose immutable legacy text only to exercise the independent audit
  // coverage guard. This is not an accepted current writing proposal.
  const composed=compileSourcedArticle({...actual.written,editorial_paragraphs:actual.written.editorial_paragraphs.map(p=>p.map(({premise_checks,...s})=>s))},catalog);
  assert.equal(composed.body,actual.content.draft_body);
  assert.deepEqual(actual.audit.issues,[]);
  assert.ok(composed.segments[3].text.includes('ba bước trên'));
  const findings=articleRequirements.map(r=>r.id);
  const hash=createHash('sha256').update(composed.body).digest('hex');
  const raw={article_sha256:hash,segments:composed.segments.map((s,index)=>({index,kind:index===0?'HYPOTHETICAL':s.citation?'SOURCE_FACT':'ADVICE',issues:[]})),finding_checks:actual.audit.finding_checks.map((c,index)=>({index,verdict:c.verdict,reason:c.reason,segment_indexes:c.segments.map(s=>s.index)}))};
  const before=JSON.stringify(actual);
  assert.equal(bindAudit(composed.body,hash,raw,composed.segments,findings),null,'Old real audit lacks the new mandatory check');
  // Isolate development coverage with synthetic valid dispositions for the
  // other mandatory Project checks; original failed audit remains unchanged.
  raw.finding_checks.forEach((check,i)=>{check.verdict='ADDRESSED';check.segment_indexes=findings[i]==='PROJECT_CLOSING_MESSAGE'?[4]:[0,1,2,3,4];});
  const all=[0,1,2,3,4];
  const check={index:2,verdict:'NOT_APPLICABLE',reason:'Synthetic protocol disposition, not semantic QA',segment_indexes:all};
  raw.finding_checks.push(check);
  assert.equal(bindAudit(composed.body,hash,raw,composed.segments,findings),null,'Editorial development cannot be waived');
  check.verdict='ADDRESSED';check.segment_indexes=[3];
  assert.equal(bindAudit(composed.body,hash,raw,composed.segments,findings),null,'One sentence cannot cover the other paragraphs');
  check.segment_indexes=all;check.verdict='UNRESOLVED';
  const rejected=bindAudit(composed.body,hash,raw,composed.segments,findings);
  assert.ok(rejected.issues.includes('PROJECT_NONREPETITIVE_DEVELOPMENT'));
  assert.equal(validAudit(actual.content,rejected),false);
  assert.equal(JSON.stringify(actual),before,'Actual failed output is immutable');
});
