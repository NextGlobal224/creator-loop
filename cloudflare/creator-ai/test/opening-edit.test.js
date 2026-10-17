import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {operationSchema,applyEditOperations} from '../src/grounding.js';
import {generateStage} from '../src/sdk-stage.js';

test('opening rewrite native item keeps inherited fields but cannot acquire a source citation; actual merge/blank remains rejected',async()=>{
  const actual=JSON.parse(readFileSync(new URL('../../../tests/fixtures/workers_b_f6aa611_opening_merge.json',import.meta.url),'utf8'));
  const before=JSON.stringify(actual),schema=operationSchema(actual.initial.segments.length,actual.catalog,actual.initial);let calls=0;
  const env={CREATOR_MODEL:'@cf/openai/gpt-oss-120b',AI:{run:async(_model,input)=>{
    calls++;const branches=input.response_format.json_schema.properties.edits.items.oneOf;
    const opening=branches.find(b=>b.properties.index.const===0 && b.properties.decision.const==='REWRITE');
    assert.ok(opening);assert.equal(opening.properties.replacement.minItems,1);assert.equal(opening.properties.replacement.maxItems,1);
    assert.equal(opening.properties.replacement.items.oneOf.length,1);
    const item=opening.properties.replacement.items.oneOf[0];
    assert.equal(item.type,'object');assert.equal(item.additionalProperties,false);
    assert.deepEqual(new Set(item.required),new Set(['source_quote_id','text','premise_checks']));
    assert.equal(item.properties.source_quote_id.type,'integer');assert.equal(item.properties.source_quote_id.const,-1);
    assert.equal(item.properties.premise_checks.type,'array');assert.deepEqual(item.properties.premise_checks.const,[]);
    assert.equal(item.properties.text.type,'string');assert.equal(item.properties.text.minLength,1);
    const ordinary=branches.find(b=>b.properties.index.const===1 && b.properties.decision.const==='REWRITE');
    assert.equal(ordinary.properties.replacement.maxItems,12);assert.deepEqual(ordinary.properties.replacement.items.oneOf.map(b=>b.properties.source_quote_id.const),[-1,3,8,9]);
    return {response:actual.operations,usage:{completion_tokens:1}};
  }}};
  const returned=await generateStage(env,{instructions:'Synthetic replay only',input:'Bounded actual failure replay',schema,budget:4500,temperature:0,reasoningEffort:'medium'},()=>{});
  assert.deepEqual(returned.value,actual.operations);
  assert.throws(()=>applyEditOperations(returned.value,actual.initial,actual.catalog),/Fictional opening cannot acquire a citation/);
  assert.equal(calls,1);assert.equal(JSON.stringify(actual),before,'No repair into automatic PASS');
});
