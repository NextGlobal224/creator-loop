import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash,webcrypto} from 'node:crypto';
import {IMAGE_MODEL,analyzeSourceImage} from '../src/source-image.js';
globalThis.crypto??=webcrypto;
// Protocol bytes only, not decoded-image or real vision acceptance.
const bytes=Buffer.alloc(44),packet={request_id:'a'.repeat(32),input_sha256:createHash('sha256').update(bytes).digest('hex'),model:IMAGE_MODEL,mime_type:'image/png',image:bytes.toString('base64')};
test('one fixed vision call preserves exact original and native unknown usage',async()=>{
 let calls=0;const native={choices:[{finish_reason:'stop',message:{content:'protocol fixture'}}]};
 const r=JSON.parse(await analyzeSourceImage({async run(model,input){calls++;assert.equal(model,IMAGE_MODEL);assert.equal(input.max_completion_tokens,1200);assert.equal(input.stream,false);assert.equal(input.messages[0].content[1].image_url.url,'data:image/png;base64,'+packet.image);assert(!input.tools);return native;}},JSON.stringify(packet),'qa-image-version'));
 assert.equal(calls,1);assert.deepEqual(r.native_response,native);assert.equal(r.native_usage,null);assert.equal(r.request_id,packet.request_id);assert.equal(r.input_sha256,packet.input_sha256);assert.equal(r.worker_version,'qa-image-version');
 // Official current maximum256k context at9091/27273 neurons/M: below the
 // unchanged5000 envelope even allowing1200 output tokens in addition.
 assert(256000*9091/1e6+1200*27273/1e6<5000);
});
test('bad image identity or changed bytes never reach model; provider error is one unknown call',async()=>{
 for(const change of [{input_sha256:'0'.repeat(64)},{model:'other'},{request_id:['a'.repeat(32)]},{mime_type:'image/svg+xml'},{image:'??'},{image:''}])await assert.rejects(analyzeSourceImage({run(){assert.fail('no inference');}},JSON.stringify({...packet,...change}),'qa'));
 let calls=0;const r=JSON.parse(await analyzeSourceImage({async run(){calls++;throw Error('private provider diagnostic');}},JSON.stringify(packet),'qa'));
 assert.equal(calls,1);assert.equal(r.unknown_outcome,true);assert.equal(r.native_usage,null);assert(!JSON.stringify(r).includes('private provider'));
});
