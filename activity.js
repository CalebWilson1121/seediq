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

  async function wireProductShell(){
    let me=null;
    try{const r=await fetch('/api/auth/me');if(r.ok)me=await r.json()}catch(e){}
    const farmer=me&&['farmer_admin','farmer_user'].includes(me.role||me.global_role);
    if(farmer){
      document.querySelectorAll('img[src*="acrefit-logo-light.svg"]').forEach(img=>img.src='/acrefit-farmer-logo-light.svg');
      document.querySelectorAll('img[src*="acrefit-logo.svg"]').forEach(img=>img.src='/acrefit-farmer-logo.svg');
      document.title=document.title.replace(/^AcreFit(?! Farmer)/,'AcreFit Farmer');
    }
    const navs=[...document.querySelectorAll('aside nav, .sidebar nav, .side nav')];
    for(const nav of navs){
      const dash=[...nav.querySelectorAll('a')].find(a=>(a.textContent||'').trim().toLowerCase()==='dashboard');
      if(dash) dash.setAttribute('href',farmer?'/farmer-dashboard.html':'/index.html');
      if(farmer){
        nav.querySelectorAll('a').forEach(a=>{
          const href=(a.getAttribute('href')||'').toLowerCase();
          const label=(a.textContent||'').trim().toLowerCase();
          if(href.includes('dealer-demo')||href.includes('pipeline')||href.includes('pricing')||href.includes('salesperson-profile')){
            a.style.display='none';
          }
          if(label==='farmer & fields'&&href.includes('prospects.html')) a.setAttribute('href','/farmer-dashboard.html');
        });
      }
      if(!nav.querySelector('[data-switch-demo-role]')){
        const a=document.createElement('a');
        a.href=farmer?'/farmer-login.html':'/login.html';
        a.textContent='Switch Demo Role';
        a.setAttribute('data-switch-demo-role','1');
        a.style.marginTop='8px';
        a.style.borderTop='1px solid rgba(255,255,255,.14)';
        a.style.paddingTop='12px';
        nav.appendChild(a);
      }
    }
  }

  wireProductShell();
  ping();
  setInterval(ping,60000);
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')ping()});
})();