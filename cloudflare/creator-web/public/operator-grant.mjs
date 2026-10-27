// Explicit owner operation only. No storage, inference, retries or credentials
// in URLs. Creating a short grant is separate from submitting a Draft job.
export function boundedGrant({remaining,observedAt,confirmed,accountPlan='Free',includedOnlyConfirmed=false,purpose='DRAFT',browserRemaining,browserObservedAt,browserIncludedConfirmed=false},now=Date.now()){
 if(!['DRAFT','SOURCE_TRANSCRIPTION','SOURCE_IMAGE_ANALYSIS','SOURCE_VIDEO_ANALYSIS'].includes(purpose))throw Error('Chọn quyền tạo Draft, trích lời nói hoặc đọc ảnh.');
 const neurons=Number(remaining);
 if(!['Free','Paid'].includes(accountPlan)||accountPlan==='Paid'&&!includedOnlyConfirmed)throw Error('Chọn đúng Workers plan. Paid chỉ dùng phần bao gồm; phải xác nhận usage và trần phí ngoài gói $0.');
 if(!confirmed||!Number.isFinite(observedAt)||now<observedAt||now-observedAt>120000||Math.floor(now/86400000)!==Math.floor(observedAt/86400000))throw Error('Hãy kiểm đúng tài khoản/Workers plan và phần Neurons bao gồm trong 2 phút vừa qua, cùng ngày UTC.');
 if(!Number.isFinite(neurons)||neurons<5000||neurons>10000)throw Error('Cần tối thiểu 5.000 Neurons còn lại cho cả lượt; trừ cả usage đã gửi nhưng dashboard chưa cập nhật.');
 const video=purpose==='SOURCE_VIDEO_ANALYSIS',seconds=Number(browserRemaining);if(video&&(accountPlan!=='Paid'||!browserIncludedConfirmed||!Number.isFinite(browserObservedAt)||now<browserObservedAt||now-browserObservedAt>120000||Math.floor(now/86400000)!==Math.floor(browserObservedAt/86400000)||!Number.isFinite(seconds)||seconds<600||seconds>36000))throw Error('Cần usage Browser Run trong gói Paid, cùng tài khoản/tháng, vừa kiểm trong2phút và dự phòng600giây; chưa cấp quyền.');
 return {grant:'B-finish-20261017-free',observed_at:observedAt/1000,expires_at:now/1000+240,remaining_calls:purpose==='DRAFT'?4:video?2:1,model:purpose==='SOURCE_IMAGE_ANALYSIS'?'@cf/google/gemma-4-26b-a4b-it':['SOURCE_TRANSCRIPTION','SOURCE_VIDEO_ANALYSIS'].includes(purpose)?'@cf/openai/whisper-large-v3-turbo':'@cf/openai/gpt-oss-120b',account_plan:accountPlan,remaining_neurons:neurons,estimated_chain_neurons:5000,...(video?{source_kind:'VIDEO',vision_model:'@cf/google/gemma-4-26b-a4b-it',browser_included_only:true,browser_observed_at:browserObservedAt/1000,browser_remaining_seconds:seconds,browser_forecast_seconds:600}:{}),...(accountPlan==='Paid'?{included_only:true,max_extra_charge_usd:0}:{})};
}
export async function submitGrant(fetcher,operatorKey,data){
 if(typeof operatorKey!=='string'||!operatorKey)throw Error('Cần mã operator riêng.');
 const response=await fetcher('/api/grant',{method:'POST',credentials:'same-origin',cache:'no-store',redirect:'error',referrerPolicy:'no-referrer',headers:{'Content-Type':'application/json','X-Operator-Key':operatorKey},body:JSON.stringify(data)});
 if(!response.ok)throw Error(response.status===401?'Phiên đăng nhập đã hết hạn.':response.status===403?'Mã operator không hợp lệ.':'Không xác nhận được quyền cho lượt này; chưa gửi job AI.');
 const result=await response.json();if(result.ok!==true)throw Error('Không xác nhận được quyền cho lượt này; chưa gửi job AI.');
 return result;
}
