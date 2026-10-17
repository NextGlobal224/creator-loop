import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {generateStage} from '../src/sdk-stage.js';
import {safeFailure} from '../src/quality.js';

test('actual audit length exhaustion stays a failure; larger allowance neither repairs nor retries a truncated reply',async()=>{
  const actual=JSON.parse(readFileSync(new URL('../../../tests/fixtures/workers_b_b8250a3_audit_budget.json',import.meta.url),'utf8'));
  const diagnostic=JSON.parse(actual.rejection.stage_output);
  assert.equal(actual.rejection.stage,'source_audit');assert.equal(diagnostic.choice_finish_reason,'length');assert.equal(diagnostic.usage.completion_tokens,2200);
  let calls=0,last;
  const env={CREATOR_MODEL:'@cf/openai/gpt-oss-120b',AI:{run:async(_model,input)=>{
    calls++;assert.equal(input.max_tokens,4000);assert.equal(input.reasoning_effort,'medium');
    // Synthetic truncation, not a recovered or repaired private native reply.
    return {choices:[{message:{content:'{"article_sha256":'},finish_reason:'length'}],usage:diagnostic.usage};
  }}};
  await assert.rejects(generateStage(env,{instructions:'Protocol replay only',input:'Synthetic truncation',schema:{type:'object',properties:{article_sha256:{type:'string'}},required:['article_sha256'],additionalProperties:false},budget:4000,temperature:0,reasoningEffort:'medium'},r=>last=r),error=>{
    assert.equal(safeFailure(error,last).choice_finish_reason,'length');return true;
  });
  assert.equal(calls,1);
});
