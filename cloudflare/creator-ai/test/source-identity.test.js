import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import worker from '../src/index.js';
import {publicCopyRevisions} from '../src/quality.js';

const actual=JSON.parse(readFileSync(new URL('../../../tests/fixtures/workers_b_6c4b660_unsupported_role.json',import.meta.url),'utf8'));
test('actual complete native audit cannot excuse an author role absent from its exact excerpt',async()=>{
  const before=JSON.stringify(actual),segments=actual.audit.paragraphs.map((p,index)=>({index,text:p.text,citation:actual.claims.find(c=>c.text===p.text)??null}));
  const revisions=publicCopyRevisions({body:actual.body,segments});
  assert.ok(revisions.some(r=>r.index===3 && r.issues.some(s=>s.includes('professional roles'))));
  assert.deepEqual(actual.audit.issues,[],'The actual false-clean audit is retained, not edited into PASS');
  let calls=0;
  const response=await worker.fetch(new Request('https://example.test/b/generate',{method:'POST',headers:{authorization:'Bearer fixture','content-type':'application/json'},body:JSON.stringify(actual.audit_request)}),{CREATOR_KEY:'fixture',CREATOR_MODEL:'@cf/openai/gpt-oss-120b',CREATOR_VERSION:{id:'fixture'},AI:{run:async()=>{calls++;throw new Error('No audit after retained source violation');}}});
  assert.equal(response.status,422);const rejection=await response.json();assert.equal(rejection.stage,'editing');assert.equal(rejection.retryable,false);
  assert.ok(rejection.audit_issues.some(s=>s.includes('professional roles')));assert.equal(calls,0);
  assert.equal(JSON.stringify(actual),before);
});
test('explicitly supplied author roles remain allowed; a question list alone does not establish them',()=>{
  const text='Các nhà nghiên cứu giới thiệu ba câu hỏi để xem xét thông tin.';
  const article=quote=>({body:text,segments:[{index:1,text,citation:{source_quote:quote}}]});
  assert.deepEqual(publicCopyRevisions(article(text)),[]);
  assert.ok(publicCopyRevisions(article('COR giới thiệu ba câu hỏi để xem xét thông tin.')).some(r=>r.issues.some(s=>s.includes('professional roles'))));
});
