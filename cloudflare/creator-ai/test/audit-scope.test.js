import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import worker from '../src/index.js';

test('actual incomplete global audit remains rejected; native schema declares every immutable segment',async()=>{
  const actual=JSON.parse(readFileSync(new URL('../../../tests/fixtures/workers_b_6ff5fb0_audit_coverage.json',import.meta.url),'utf8'));
  const unchanged=JSON.stringify(actual),captured=[];
  const env={CREATOR_KEY:'fixture',CREATOR_MODEL:'@cf/openai/gpt-oss-120b',CREATOR_VERSION:{id:'fixture'},AI:{run:async(model,options)=>{
    captured.push(options);
    return {choices:[{message:{content:JSON.stringify(actual.audit_output)},finish_reason:'stop'}],usage:{prompt_tokens:12,completion_tokens:30}};
  }}};
  const response=await worker.fetch(new Request('https://example.test/b/generate',{method:'POST',headers:{authorization:'Bearer fixture','content-type':'application/json'},body:JSON.stringify(actual.audit_request)}),env);
  const result=await response.json();
  assert.equal(captured.length,1,'Only audit is replayed through the real SDK; no cloud request');
  const schema=captured[0].response_format.json_schema;
  const branches=schema.properties.finding_checks.items.oneOf;
  const globals=branches.filter(b=>b.properties.segment_indexes.const?.length===schema.properties.segments.minItems);
  assert.equal(globals.length,4,'Two verdicts for each of the two global requirements');
  for(const branch of globals){
    assert.deepEqual(branch.properties.segment_indexes.const,[0,1,2,3,4]);
    assert.equal(branch.properties.segment_indexes.type,'array');
    assert.ok(branch.required.includes('reason'));
    assert.ok(branch.required.includes('verdict'));
    assert.ok(['ADDRESSED','UNRESOLVED'].includes(branch.properties.verdict.const));
  }
  assert.equal(response.status,422);
  assert.equal(result.stage,'source_audit');
  assert.equal(result.retryable,false);
  assert.equal(result.candidate_body,actual.body);
  assert.equal(JSON.stringify(actual),unchanged,'Incomplete real audit is never repaired into PASS');
});
