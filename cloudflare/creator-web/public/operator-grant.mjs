// Explicit owner operation only. No storage, inference, retries or credentials
// in URLs. Creating a short grant is separate from submitting a Draft job.
export function boundedGrant({remaining,observedAt,confirmed,accountPlan='Free',includedOnlyConfirmed=false,purpose='DRAFT'},now=Date.now()){
 if(!['DRAFT','SOURCE_TRANSCRIPTION','SOURCE_IMAGE_ANALYSIS'].includes(purpose))throw Error('Chọn quyền tạo Draft, trích lời nói hoặc đọc ảnh.');
 const neurons=Number(remaining);
 if(!['Free','Paid'].includes(accountPlan)||accountPlan==='Paid'&&!includedOnlyConfirmed)throw Error('Chọn đúng Workers plan. Paid chỉ dùng phần bao gồm; phải xác nhận usage và trần phí ngoài gói $0.');
 if(!confirmed||!Number.isFinite(observedAt)||now<observedAt||now-observedAt>120000||Math.floor(now/86400000)!==Math.floor(observedAt/86400000))throw Error('Hãy kiểm đúng tài khoản/Workers plan và phần Neurons bao gồm trong 2 phút vừa qua, cùng ngày UTC.');
 if(!Number.isFinite(neurons)||neurons<5000||neurons>10000)throw Error('Cần tối thiểu 5.000 Neurons còn lại cho cả lượt; trừ cả usage đã gửi nhưng dashboard chưa cập nhật.');
 return {grant:'B-finish-20261017-free',observed_at:observedAt/1000,expires_at:now/1000+240,remaining_calls:purpose==='DRAFT'?4:1,model:purpose==='SOURCE_IMAGE_ANALYSIS'?'@cf/google/gemma-4-26b-a4b-it':purpose==='SOURCE_TRANSCRIPTION'?'@cf/openai/whisper-large-v3-turbo':'@cf/openai/gpt-oss-120b',account_plan:accountPlan,remaining_neurons:neurons,estimated_chain_neurons:5000,...(accountPlan==='Paid'?{included_only:true,max_extra_charge_usd:0}:{})};
}
export async function submitGrant(fetcher,operatorKey,data){
 if(typeof operatorKey!=='string'||!operatorKey)throw Error('Cần mã operator riêng.');
 const response=await fetcher('/api/grant',{method:'POST',credentials:'same-origin',cache:'no-store',redirect:'error',referrerPolicy:'no-referrer',headers:{'Content-Type':'application/json','X-Operator-Key':operatorKey},body:JSON.stringify(data)});
 if(!response.ok)throw Error(response.status===401?'Phiên đăng nhập đã hết hạn.':response.status===403?'Mã operator không hợp lệ.':'Không xác nhận được quyền cho lượt này; chưa gửi job AI.');
 const result=await response.json();if(result.ok!==true)throw Error('Không xác nhận được quyền cho lượt này; chưa gửi job AI.');
 return result;
}
