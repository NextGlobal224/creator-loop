import {test} from 'node:test';
import assert from 'node:assert/strict';
import {fileKind,materialBytes,maxFileBytes} from '../public/intake.mjs';
test('Unicode filename selects image type and UTF8 Vietnamese keeps exact bytes',async()=>{
 assert.equal(fileKind('Ảnh thử có dấu.png','image/png'),'IMAGE');
 const text='Tư liệu tiếng Việt có dấu và khoảng trắng.';
 assert.deepEqual(await materialBytes(new File([text],'Tư liệu có dấu.txt'),'', 'TEXT'),new TextEncoder().encode(text));
});
test('invalid text/image/backup inputs identify the actual correction',async()=>{
 await assert.rejects(materialBytes(new File([new Uint8Array([255,216,255])],'ảnh.jpg'),'', 'TEXT'),/UTF-8/);
 await assert.rejects(materialBytes(new File(['<svg/>'],'ảnh.svg'),'', 'IMAGE'),/PNG/);
 await assert.rejects(materialBytes(new File(['tiếng Việt'.normalize('NFD')],'text.txt'),'', 'TEXT'),/NFC/);
 await assert.rejects(materialBytes(new File(['{"format":"CREATOR_WEB_BACKUP_V1"}'],'backup.json'),'', 'TEXT'),/Khôi phục/);
});
test('oversize is rejected before reading or uploading',async()=>{
 let read=false;await assert.rejects(materialBytes({size:maxFileBytes+1,arrayBuffer(){read=true;}},'', 'TEXT'),/8 MiB/);assert.equal(read,false);
});
test('supported image signatures are recognized without calling them image quality proof',async()=>{
 for(const bytes of [[137,80,78,71,13,10,26,10],[255,216,255],[82,73,70,70,0,0,0,0,87,69,66,80]]){
  const raw=new Uint8Array(bytes);assert.deepEqual(await materialBytes(new File([raw],'format fixture'),'', 'IMAGE'),raw);
 }
});
