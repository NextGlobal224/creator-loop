import test from 'node:test';
import assert from 'node:assert/strict';
import {analysisProfile} from '../src/analysis-audit.js';

const materials=[{material_id:'source',text:'Source: Note\nTư liệu chỉ nêu giới hạn của đoạn ghi chú.'}];
const selected='Giải thích giới hạn của tư liệu, không thêm quy trình kiểm chứng.';
const value=direction=>({observations:[{material_id:'source',quote:'Tư liệu chỉ nêu giới hạn của đoạn ghi chú.',observation:'Tư liệu chỉ nêu giới hạn của đoạn ghi chú.'}],inferences:[],direction});
async function replay(mode,direction,returned){
 const packet={stage:'analysis',format:'TEXT',mode,writing_prompt:'fixture\n'+JSON.stringify({materials,direction})};
 const profile=await analysisProfile(packet);let calls=0,input;
 const run=profile.run({run:async(_,native)=>{calls++;input=native;return {response:value(returned)};}},'fixture-model',{messages:[{role:'system',content:'Original'}],response_format:{type:'json_schema',json_schema:{properties:{observations:{},direction:{type:'string'},inferences:{items:{}}}}}});
 return {profile,run,inspect:()=>({calls,input})};
}
test('MANUAL analysis rejects a new model plan without repairing the retained reply',async()=>{
 const changed=selected+' Compare metadata to identify authenticity.';
 const {profile,run,inspect}=await replay('MANUAL',selected,changed);
 await assert.rejects(run,/preserve the user selection/);
 assert.equal(profile.rejectedJSON().direction,changed);
 assert.equal(inspect().calls,1);
 assert.equal(inspect().input.response_format.json_schema.properties.direction.const,selected);
});
test('MANUAL keeps the exact selected Unicode direction through its existing 2000-character limit',async()=>{
 const direction='Đ🙂'.repeat(1000);
 const {run,inspect}=await replay('MANUAL',direction,direction);
 assert.equal((await run).response.direction,direction);
 assert.equal(inspect().input.response_format.json_schema.properties.direction.maxLength,2000);
 await assert.rejects(replay('MANUAL',direction+'x',direction),/Invalid selected manual direction/);
});
test('AUTOMATIC still generates a bounded plan without a selected-direction const',async()=>{
 const {run,inspect}=await replay('AUTOMATIC',selected,'Một kế hoạch được tạo tự động.');
 await run;
 assert.equal(inspect().input.response_format.json_schema.properties.direction.const,undefined);
 assert.equal(inspect().input.response_format.json_schema.properties.direction.maxLength,1200);
});
