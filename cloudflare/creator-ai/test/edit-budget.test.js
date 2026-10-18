import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {generateStage} from '../src/sdk-stage.js';
import {safeFailure} from '../src/quality.js';

test('actual exhausted edit metrics are retained; synthetic length reply fails once without output repair',async()=>{
  const actual=JSON.parse(readFileSync(new URL('../../../tests/fixtures/workers_b_2a5b58d_edit_budget.json',import.meta.url),'utf8'));
  const diagnostic=JSON.parse(actual.rejection.stage_output);
  assert.equal(diagnostic.usage.completion_tokens,3000);
  assert.equal(diagnostic.stage_diagnostic.phase,'SDK_AFTER_BINDING');
  assert.equal(diagnostic.stage_diagnostic.binding_calls,1);
  assert.equal(actual.rejection.stage,'editing');
  let calls=0,raw;
  // The archived diagnostic did not record finish_reason. This reply is a
  // synthetic regression case, not a claimed recovery of the private response.
  const env={CREATOR_MODEL:'@cf/openai/gpt-oss-120b',AI:{run:async(_model,input)=>{
    calls++;assert.equal(input.max_tokens,4500);assert.equal(input.reasoning_effort,'medium');
    return {choices:[{message:{content:null,reasoning_content:'private reasoning'},finish_reason:'length'}],usage:diagnostic.usage};
  }}};
  await assert.rejects(generateStage(env,{instructions:'JSON only',input:'Synthetic bounded input',schema:{type:'object',properties:{x:{type:'string'}},required:['x'],additionalProperties:false},budget:4500,temperature:0,reasoningEffort:'medium'},r=>raw=r),error=>{
    const safe=safeFailure(error,raw);assert.equal(safe.choice_finish_reason,'length');
    assert.equal(safe.stage_diagnostic.binding_calls,1);
    assert.equal(JSON.stringify(safe).includes('private reasoning'),false);return true;
  });
  assert.equal(calls,1);
  assert.equal(safeFailure(new Error('private'),{choices:[{finish_reason:'private-secret'}]}).choice_finish_reason,null);
});
