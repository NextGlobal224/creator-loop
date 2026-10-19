export const maxFileBytes=8*1024*1024;
export function fileKind(name,mime=''){
 if(/\.(png|jpe?g|webp)$/i.test(name)||mime.startsWith('image/'))return 'IMAGE';
 if(mime.startsWith('video/'))return 'VIDEO';
 if(mime.startsWith('audio/'))return 'AUDIO';
 return 'TEXT';
}
export async function materialBytes(file,text,kind){
 if(file&&(!file.size||file.size>maxFileBytes))throw Error('Tệp phải có nội dung, tối đa 8 MiB; chưa gửi upload.');
 const raw=file?new Uint8Array(await file.arrayBuffer()):new TextEncoder().encode(text.normalize('NFC'));
 if(!raw.length||raw.length>maxFileBytes)throw Error('Nội dung phải có dữ liệu, tối đa 8 MiB.');
 if(kind==='TEXT'){
  let decoded;try{decoded=new TextDecoder('utf-8',{fatal:true}).decode(raw);}catch{throw Error('Tệp văn bản phải là UTF-8. Ảnh cần loại IMAGE; chỉ hỗ trợ PNG/JPEG/WebP.');}
  if(!decoded.trim()||decoded!==decoded.normalize('NFC'))throw Error('Text phải có nội dung UTF-8 chuẩn NFC. Lưu lại tệp đúng chuẩn hoặc dán text vào ô nội dung.');
  try{if(JSON.parse(decoded)?.format==='CREATOR_WEB_BACKUP_V1')throw Error('BACKUP');}catch(e){if(e.message==='BACKUP')throw Error('Đây là backup DB + media; dùng mục Khôi phục backup, không nhập như tư liệu.');}
 }
 if(kind==='IMAGE'){
  const png=[137,80,78,71,13,10,26,10].every((v,i)=>raw[i]===v),jpeg=raw[0]===255&&raw[1]===216&&raw[2]===255,webp=String.fromCharCode(...raw.slice(0,4))==='RIFF'&&String.fromCharCode(...raw.slice(8,12))==='WEBP';
  if(!png&&!jpeg&&!webp)throw Error('Ảnh chỉ hỗ trợ PNG, JPEG hoặc WebP thực; không nhận SVG/HTML hay tệp chỉ đổi đuôi.');
 }
 return raw;
}
