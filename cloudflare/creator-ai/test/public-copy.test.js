import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {checkedWriting,applyEditOperations} from '../src/grounding.js';
import {compileSourcedArticle,quoteCatalog,publicArticleIssues,publicCopyRevisions} from '../src/quality.js';
import worker from '../src/index.js';

const actual=JSON.parse(readFileSync(new URL('../../../tests/fixtures/workers_b_v31_editing_failed.json',import.meta.url),'utf8'));
const catalog=quoteCatalog(actual.materials);
const currentMiss=JSON.parse(readFileSync(new URL('../../../tests/fixtures/workers_b_dc8326b_public_repetition.json',import.meta.url),'utf8'));

test('actual dc8326b paraphrase cannot bypass bilingual repetition revision with different Vietnamese wording',()=>{
  const before=JSON.stringify(currentMiss);
  const sources=quoteCatalog(currentMiss.materials);
  const writing=checkedWriting(currentMiss.written,sources);
  const initial=compileSourcedArticle(writing,sources);
  assert.ok(publicArticleIssues(currentMiss.body).some(s=>s.includes('bilingual')));
  const revisions=publicCopyRevisions(initial);
  assert.ok(revisions.some(r=>r.issues.some(s=>s.includes('bilingual'))));
  assert.throws(()=>applyEditOperations(currentMiss.operations,initial,sources),/Public copy violation requires REWRITE or REMOVE/);
  assert.equal(JSON.stringify(currentMiss),before,'Actual output and erroneous audit remain immutable');
});
// Historical text is still useful for public-copy regression after the new
// citation-role guard rejects it earlier in the current generation route.
const initial=compileSourcedArticle({...actual.written,editorial_paragraphs:actual.written.editorial_paragraphs.map(p=>p.map(({premise_checks,...s})=>s))},catalog);

test('actual v31 citation receipts cannot exempt internal metadata or duplicate bilingual prose from revision',()=>{
  assert.throws(()=>checkedWriting(actual.written,catalog),/eligible source excerpt/);
  const revisions=publicCopyRevisions(initial);
  assert.deepEqual(revisions.map(r=>r.index),[1,3]);
  assert.ok(revisions[0].issues.some(s=>s.includes('research-note')));
  assert.ok(revisions[1].issues.some(s=>s.includes('bilingual')));
  assert.throws(()=>applyEditOperations(actual.operations,initial,catalog),/Public copy violation requires REWRITE or REMOVE/);
});

test('observed bilingual repetition is rejected without banning English names or a standalone source quotation',()=>{
  assert.ok(publicArticleIssues(actual.candidate_body).some(s=>s.includes('bilingual')));
  assert.deepEqual(publicArticleIssues('COR: “Who’s behind the information? What’s the evidence? What do other sources say?”'),[]);
  assert.deepEqual(publicArticleIssues('UNESCO và COR giới thiệu các tài liệu. Hãy hỏi ai đứng sau thông tin, căn cứ nào được đưa ra và nguồn khác nói gì.'),[]);
});

test('public copy constraints never silently rewrite the recorded automatic output',()=>{
  const before=JSON.stringify(actual);
  const operations=structuredClone(actual.operations);
  // Synthetic edit to exercise mapping only; not an automatic quality result.
  operations.edits[1].decision='REWRITE';
  operations.edits[1].replacement=[{text:'Hãy đọc nguồn và ngữ cảnh trước khi đưa ra kết luận.',source_quote_id:-1,premise_checks:[]}];
  operations.edits[3].decision='REMOVE';
  const edited=applyEditOperations(operations,initial,catalog);
  const composed=compileSourcedArticle({hypothetical_opening:edited.hypothetical_opening,editorial_paragraphs:edited.editorial_paragraphs},catalog);
  assert.deepEqual(publicArticleIssues(composed.body),[]);
  assert.ok(composed.claims.every(c=>composed.body.includes(c.text)));
  assert.equal(JSON.stringify(actual),before);
  assert.equal(actual.operations.edits[1].decision,'KEEP');
});

test('actual legacy metadata citation cannot enter editing through a supplied predecessor',async()=>{
  let calls=0;
  const analysis={observations:[{material_id:actual.materials[0].material_id,quote:catalog[0].source_quote,observation:'Synthetic analysis for protocol replay only.'}],inferences:[],direction:'Protocol replay only.'};
  const packet={prompt:'Protocol replay',writing_prompt:'Selected public notes\n'+JSON.stringify({format:'TEXT',materials:actual.materials}),schema:{properties:{observations:{type:'array'},inferences:{type:'array'},direction:{type:'string'}}},stage:'editing',previous:[{stage:'analysis',value:analysis,usage:{}},{stage:'writing',value:actual.written,usage:{}}]};
  const response=await worker.fetch(new Request('https://example.test/b/generate',{method:'POST',headers:{authorization:'Bearer fixture','content-type':'application/json'},body:JSON.stringify(packet)}),{CREATOR_KEY:'fixture',CREATOR_MODEL:'@cf/openai/gpt-oss-120b',CREATOR_VERSION:{id:'fixture'},AI:{run:async(_,options)=>{
    calls++;
    assert.equal(calls,1,'Audit must not run after rejected editing');
    const input=options.messages[1].content;
    assert.ok(input.includes('mandatory_public_revisions'));
    assert.ok(input.includes('bilingual'));
    assert.ok(options.messages[0].content.includes('KEEP is invalid even when SOURCE_SUPPORTED'));
    return {response:actual.operations,usage:{prompt_tokens:1,completion_tokens:1}};
  }}});
  assert.equal(response.status,502);
  const rejection=await response.json();
  assert.equal(rejection.retryable,false);
  assert.equal(rejection.stage,'writing');
  assert.equal(calls,0);
});
