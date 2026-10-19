// Explicit owner operation only. No storage, inference, retries or credentials
// in URLs. Creating a short grant is separate from submitting a Draft job.
export function boundedGrant({remaining,observedAt,confirmed},now=Date.now()){
 const neurons=Number(remaining);
 if(!confirmed||!Number.isFinite(observedAt)||now<observedAt||now-observedAt>120000)throw Error('Hãy kiểm đúng tài khoản Free và xác nhận quota trong 2 phút vừa qua.');
 if(!Number.isFinite(neurons)||neurons<5000||neurons>10000)throw Error('Cần tối thiểu 5.000 Neurons còn lại cho cả lượt; trừ cả usage đã gửi nhưng dashboard chưa cập nhật.');
 return {grant:'B-finish-20261017-free',observed_at:observedAt/1000,expires_at:now/1000+240,remaining_calls:4,model:'@cf/openai/gpt-oss-120b',account_plan:'Free',remaining_neurons:neurons,estimated_chain_neurons:5000};
}
export async function submitGrant(fetcher,operatorKey,data){
 if(typeof operatorKey!=='string'||!operatorKey)throw Error('Cần mã operator riêng.');
 const response=await fetcher('/api/grant',{method:'POST',credentials:'same-origin',cache:'no-store',redirect:'error',referrerPolicy:'no-referrer',headers:{'Content-Type':'application/json','X-Operator-Key':operatorKey},body:JSON.stringify(data)});
 if(!response.ok)throw Error(response.status===401?'Phiên đăng nhập đã hết hạn.':response.status===403?'Mã operator không hợp lệ.':'Không xác nhận được quyền cho lượt này; chưa gửi job AI.');
 const result=await response.json();if(result.ok!==true)throw Error('Không xác nhận được quyền cho lượt này; chưa gửi job AI.');
 return result;
}
