import test from 'node:test';
import assert from 'node:assert/strict';
import {saveCloudBackup} from '../public/pending.mjs';

function storage(){const values=new Map();return {getItem:k=>values.get(k)||null,setItem:(k,v)=>values.set(k,v),removeItem:k=>values.delete(k)};}
test('committed backup with lost ACK is read-reconciled after reload, not duplicated',async()=>{
 const cache=storage(),saved=[];let posts=0;
 const api=async(path,data,key)=>{if(data){posts++;saved.push({id:key});throw Error('Lost ACK');}return {backups:saved};};
 await assert.rejects(()=>saveCloudBackup(api,cache,7,()=> 'original'));
 const confirmed=await saveCloudBackup(api,cache,8,()=>{throw Error('must not allocate');});
 assert.equal(confirmed.id,'original');assert.equal(posts,1);assert.equal(cache.getItem('creator-pending-backup-v1'),null);
});
test('unknown result and failed list read retain identity; confirmed absence reuses exact revision',async()=>{
 const cache=storage(),posts=[];
 await assert.rejects(()=>saveCloudBackup(async(path,data,key)=>{posts.push({data,key});throw Error('timeout');},cache,7,()=> 'original'));
 await assert.rejects(()=>saveCloudBackup(async()=>{throw Error('list unavailable');},cache,9,()=> 'wrong'));
 assert.equal(posts.length,1);
 const result=await saveCloudBackup(async(path,data,key)=>{if(!data)return {backups:[]};posts.push({data,key});return {id:key,saved_on_cloud:true};},cache,9,()=> 'wrong');
 assert.equal(result.id,'original');assert.deepEqual(posts[0],posts[1]);assert.equal(posts[1].data.expected_revision,7);
});
test('server-confirmed refusal releases intent; no automatic new save or retry',async()=>{
 const cache=storage();let calls=0;
 await assert.rejects(()=>saveCloudBackup(async()=>{calls++;const e=Error('stale');e.status=409;throw e;},cache,7,()=> 'old'));
 assert.equal(calls,1);assert.equal(cache.getItem('creator-pending-backup-v1'),null);
});
