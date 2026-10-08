(function(){
  async function ping(){
    if(document.visibilityState!=='visible') return;
    try{
      await fetch('/api/activity/heartbeat',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({path:location.pathname+location.search}),
        keepalive:true
      });
    }catch(e){}
  }
  ping();
  setInterval(ping,60000);
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')ping()});
})();