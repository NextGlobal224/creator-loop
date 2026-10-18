import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import worker from '../src/index.js';
import {editorialRoles} from '../src/grounding.js';
const actual=JSON.parse(readFileSync(new URL('../../../tests/fixtures/workers_b_ede13a9_writing_failed.json',import.meta.url),'utf8'));
test('unsupported-benefit writing without a citation-role violation reaches the editor as a lead',async()=>{
  const before=JSON.stringify(actual);
  // Synthetic derivative isolates the benefit-lead path from the new earlier
  // metadata-citation rejection. Original model output stays immutable.
  const written=structuredClone(actual.written);
  for(const paragraph of written.editorial_paragraphs)for(const segment of paragraph){segment.source_quote_id=-1;segment.premise_checks=[];}
  const analysis={observations:[{material_id:actual.materials[0].material_id,quote:actual.materials[0].text.split('\n')[0],observation:'Protocol replay only'}],inferences:[],direction:'Protocol replay only'};
  const plan={...written,editorial_paragraphs:Object.fromEntries(editorialRoles.map((r,i)=>[r.key,written.editorial_paragraphs[i]]))};
  const packet={prompt:'Offline replay',writing_prompt:'Selected notes\n'+JSON.stringify({format:'TEXT',purpose:'Giúp người đọc nhận thức đúng về sự thật trước khi chia sẻ ảnh chụp màn hình',materials:actual.materials}),schema:{properties:{observations:{type:'array'},inferences:{type:'array'},direction:{type:'string'}}},stage:'editing',previous:[{stage:'analysis',value:analysis,usage:{}},{stage:'writing',value:plan,usage:{}}]};
  let calls=0,captured;
  const response=await worker.fetch(new Request('https://example.test/b/generate',{method:'POST',headers:{authorization:'Bearer fixture','content-type':'application/json'},body:JSON.stringify(packet)}),{CREATOR_KEY:'fixture',CREATOR_MODEL:'@cf/openai/gpt-oss-120b',CREATOR_VERSION:{id:'fixture'},AI:{run:async(_,options)=>{
    calls++;captured=options.messages[1].content;
    // Stop synthetic inference deliberately. Receiving a lead is not proof that
    // the model resolves it, or that the article is source-grounded/usable.
    throw new Error('Synthetic stop after observing editing input');
  }}});
  assert.equal(response.status,502);assert.equal(calls,1);
  const suffix=captured.indexOf('\nReturn only JSON matching this schema:\n');
  assert.ok(suffix>0);
  const input=JSON.parse(captured.slice(captured.indexOf('\n')+1,suffix));
  assert.ok(input.risk_leads.some(s=>s.includes('giúp giảm rủi ro')));
  assert.ok(input.risk_leads.some(s=>s.includes('xác minh nguồn gốc')));
  assert.ok(input.editorial_warnings.some(s=>s.includes('not a different assertion')));
  assert.ok(input.editorial_warnings.some(s=>s.includes('last public text')));
  assert.equal(JSON.stringify(actual),before);
});
