import test from 'node:test';
import assert from 'node:assert/strict';
import {boundedGrant,submitGrant} from '../public/operator-grant.mjs';
test('image extraction has a distinct one-call model and cannot reuse previous UTC day quota',()=>{
 const data={remaining:6000,observedAt:100000,confirmed:true,purpose:'SOURCE_IMAGE_ANALYSIS',accountPlan:'Paid',includedOnlyConfirmed:true},p=boundedGrant(data,110000);
 assert.equal(p.model,'@cf/google/gemma-4-26b-a4b-it');assert.equal(p.remaining_calls,1);assert.equal(p.estimated_chain_neurons,5000);
 assert.throws(()=>boundedGrant({...data,observedAt:86399000},86401000),/UTC/);
});
test('explicit same-account confirmation is fresh, bounded and separate from inference',()=>{
 const input={remaining:6000,observedAt:100000,confirmed:true},payload=boundedGrant(input,110000);
 assert.equal(payload.remaining_calls,4);assert.equal(payload.expires_at,350);assert.equal(payload.observed_at,100);assert.equal(payload.estimated_chain_neurons,5000);
 for(const change of [{confirmed:false},{observedAt:-20000},{observedAt:120000},{remaining:4999},{remaining:10001},{remaining:NaN}])assert.throws(()=>boundedGrant({...input,...change},110000));
 assert.throws(()=>boundedGrant(input,220001));
});
test('operator key is only a transient header on one fixed-origin grant request',async()=>{
 const calls=[],key='FIXTURE-OPERATOR-NONSECRET',data=boundedGrant({remaining:6000,observedAt:100000,confirmed:true},110000);
 await submitGrant(async(...args)=>{calls.push(args);return {ok:true,json:async()=>({ok:true})};},key,data);
 assert.equal(calls.length,1);const [url,options]=calls[0];assert.equal(url,'/api/grant');assert.equal(options.redirect,'error');assert.equal(options.credentials,'same-origin');assert.equal(options.cache,'no-store');assert.equal(options.headers['X-Operator-Key'],key);assert.doesNotMatch(options.body,/FIXTURE-OPERATOR/);assert.deepEqual(JSON.parse(options.body),data);
});
test('ambiguous grant response is never retried and does not enqueue a job',async()=>{
 let calls=0;await assert.rejects(()=>submitGrant(async()=>{calls++;throw Error('Lost ACK');},'fixture',{}));assert.equal(calls,1);
 for(const status of [400,401,403,500])await assert.rejects(()=>submitGrant(async()=>({ok:false,status}),'fixture',{}));
});

test('Paid accepts only verified included allocation without increasing old forecast',()=>{
 const input={remaining:6000,observedAt:100000,confirmed:true,accountPlan:'Paid',includedOnlyConfirmed:true};
 const payload=boundedGrant(input,110000);assert.equal(payload.account_plan,'Paid');assert.equal(payload.included_only,true);assert.equal(payload.max_extra_charge_usd,0);assert.equal(payload.estimated_chain_neurons,5000);assert.equal(payload.grant,'B-finish-20261017-free');
 for(const change of [{includedOnlyConfirmed:false},{remaining:4999},{remaining:10001},{accountPlan:'Unlimited'}])assert.throws(()=>boundedGrant({...input,...change},110000));
});
test('source transcription uses one fixed model call with unchanged reserve and included-only gates',()=>{
 const input={remaining:6000,observedAt:100000,confirmed:true,purpose:'SOURCE_TRANSCRIPTION',accountPlan:'Paid',includedOnlyConfirmed:true};
 const grant=boundedGrant(input,110000);
 assert.equal(grant.model,'@cf/openai/whisper-large-v3-turbo');assert.equal(grant.remaining_calls,1);assert.equal(grant.estimated_chain_neurons,5000);assert.equal(grant.max_extra_charge_usd,0);
 for(const change of [{purpose:'OTHER'},{includedOnlyConfirmed:false},{remaining:4999},{confirmed:false}])assert.throws(()=>boundedGrant({...input,...change},110000));
});
