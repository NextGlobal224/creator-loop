import test from 'node:test';
import assert from 'node:assert/strict';
import {formProblem} from '../public/pending.mjs';

const request=()=>({project_id:'project',mode:'MANUAL',purpose:'Giải thích nguồn',direction:'Góc được chọn',material_ids:['source'],output_format:'TEXT'});
test('missing manual inputs identify the actual step before any submission',()=>{
 for(const [change,field] of [[{project_id:''},'project'],[{material_ids:[]},'materials'],[{material_ids:Array(9).fill('source')},'materials'],[{purpose:'   '},'purpose'],[{direction:'\n '},'direction']])assert.equal(formProblem({...request(),...change}).field,field);
 assert.equal(formProblem(request()),null);
 assert.equal(formProblem({...request(),mode:'AUTOMATIC',direction:''}),null);
});
test('caption needs real uploaded image identity and explicit source/rights',()=>{
 const caption={...request(),output_format:'IMAGE_CAPTION',draft_choices:{image_file_id:'image-id',image_provenance:'Ảnh tự tạo',image_usage_note:'OWNED'}};
 assert.equal(formProblem(caption),null);
 for(const key of ['image_file_id','image_provenance','image_usage_note'])assert.equal(formProblem({...caption,draft_choices:{...caption.draft_choices,[key]:'  '}})?.field,'draft-image');
});
