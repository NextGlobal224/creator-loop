// Lost acknowledgments retain the original payload. Only reads reconcile them;
// starting another inference always requires a new explicit submit.
export const storageKey='creator-pending-request-v2';
export function preparePending(data,existing,newId){
  if(existing){
    if(JSON.stringify(existing.data)!==JSON.stringify(data))throw Error('Còn yêu cầu chưa rõ kết quả. Mở trạng thái hoặc bỏ yêu cầu chờ trước khi đổi nội dung.');
    return existing;
  }
  return {key:newId(),data};
}
export function reconcilePending(status,job){
  if(status===200&&job?.id)return {clear:true,job};
  if(status===404)return {clear:false,absent:true};
  throw Error('Chưa xác minh được yêu cầu đang chờ; không tự gửi lại.');
}
