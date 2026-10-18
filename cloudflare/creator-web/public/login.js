document.querySelector('#login').addEventListener('submit',async e=>{
  e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;
  try{const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.querySelector('#password').value})});
    const v=await r.json();if(!r.ok)throw Error(v.error);location.replace('/');
  }catch(e){document.querySelector('#message').textContent=e.message;}finally{button.disabled=false;}
});
