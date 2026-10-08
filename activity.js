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

  function wireDemoNav(){
    const navs=[...document.querySelectorAll('aside nav, .sidebar nav, .side nav')];
    for(const nav of navs){
      const dash=[...nav.querySelectorAll('a')].find(a=>(a.textContent||'').trim().toLowerCase()==='dashboard');
      if(dash) dash.setAttribute('href','/index.html');
      if(!nav.querySelector('[data-switch-demo-role]')){
        const a=document.createElement('a');
        a.href='/login.html';
        a.textContent='Switch Demo Role';
        a.setAttribute('data-switch-demo-role','1');
        a.style.marginTop='8px';
        a.style.borderTop='1px solid rgba(255,255,255,.14)';
        a.style.paddingTop='12px';
        nav.appendChild(a);
      }
    }
  }

  wireDemoNav();
  ping();
  setInterval(ping,60000);
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')ping()});
})();