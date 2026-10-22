import test from 'node:test';
import assert from 'node:assert/strict';
import {codePointRange,currentEvidence} from '../public/source-review.mjs';

test('browser UTF16 selection becomes NFC snapshot Unicode code-point locator',()=>{
 const text='Mở đầu 🧭.\nĐây là tiếng Việt.';
 const start=text.indexOf('Đây'),end=text.length;
 const range=codePointRange(text,start,end);
 assert.equal(Array.from(text).slice(range.start,range.end).join(''),'Đây là tiếng Việt.');
 assert.equal(range.start,start-1);
 const emoji=text.indexOf('🧭');assert.throws(()=>codePointRange(text,emoji+1,end),/Unicode/);
 for(const pair of [[0,0],[-1,2],[2,1],[0,text.length+1]])assert.throws(()=>codePointRange(text,...pair));
});
test('source review offers only latest versions but leaves source history intact',()=>{
 const versions=[{evidence_id:'a',version_no:2,content:'new'},{evidence_id:'b',version_no:1,content:'other'},{evidence_id:'a',version_no:1,content:'old'}];
 assert.deepEqual(currentEvidence(versions).map(v=>v.content),['new','other']);
 assert.equal(versions[2].content,'old');assert.equal(versions.length,3);
});
test('timed source selection follows original time, not random Evidence IDs',()=>{
 const v=(id,no,start,content)=>({evidence_id:id,version_no:no,locator_type:'TIME_RANGE',locator_data:JSON.stringify({start_ms:start,end_ms:start+100,track:'audio'}),content});
 assert.deepEqual(currentEvidence([v('a',1,1000,'late'),v('b',1,0,'old'),v('b',2,200,'corrected')]).map(v=>v.content),['corrected','late']);
});
