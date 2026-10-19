import test from 'node:test';
import assert from 'node:assert/strict';
import {compactStagePrompt,READABLE_CONTRACT_MARKER} from '../src/prompt-contract.js';
const marker='\nReturn only JSON matching this schema:\n';
const roles=Object.fromEntries(['a','b','c','closing'].map(k=>[k,{type:'array',description:'Original purpose '+k,items:{type:'object'}}]));
function fixture(opening='opening'){
 const schema={type:'object',properties:{[opening]:opening==='opening'?{type:'array'}:{type:'string'},editorial_paragraphs:{type:'object',properties:roles}},required:[opening,'editorial_paragraphs'],additionalProperties:false};
 return {max_tokens:3000,reasoning_effort:'medium',response_format:{type:'json_schema',json_schema:schema},messages:[{role:'system',content:'Keep original factual guards.'},{role:'user',content:'SELECTED SOURCE PACK EXACT\n{"text":"Nguồn nguyên văn Việt Nam"}'+marker+JSON.stringify(schema)},{role:'user',content:'Other original message'}]};
}
test('compaction changes only the recognized suffix and preserves full native/source/receipt contracts',()=>{
 for(const opening of ['opening','hypothetical_opening']){
  const native=fixture(opening),original=structuredClone(native);
  assert.equal(compactStagePrompt(native,'writing'),true);
  assert.deepEqual({...native,messages:original.messages},{...original});
  assert.equal(native.messages[0].content,original.messages[0].content);assert.deepEqual(native.messages[2],original.messages[2]);
  assert.equal(native.messages[1].content.split(READABLE_CONTRACT_MARKER)[0],original.messages[1].content.split(marker)[0]);
  const summary=JSON.parse(native.messages[1].content.split(READABLE_CONTRACT_MARKER)[1]);
  assert.equal(summary.fields.editorial_paragraphs.closing.purpose,roles.closing.description);
  assert.match(summary.fields.editorial_paragraphs.a.items.premise_checks,/every factual assertion/);
  assert.equal(compactStagePrompt(native,'writing'),false);
 }
});
test('unknown SDK roots or missing required native constraints fail before a model binding',()=>{
 for(const mutate of [n=>n.response_format.json_schema.required.pop(),n=>n.response_format.json_schema.additionalProperties=true,n=>{const s=JSON.parse(n.messages[1].content.split(marker)[1]);s.properties.unknown={};n.messages[1].content=n.messages[1].content.split(marker)[0]+marker+JSON.stringify(s);}]){
  const n=fixture();mutate(n);assert.throws(()=>compactStagePrompt(n,'writing'),/Unexpected/);
 }
});
