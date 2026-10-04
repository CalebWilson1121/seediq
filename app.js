
const ROUTES = {
  dashboard:'index.html', farmer:'farmers.html', analysis:'field-analysis.html', plan:'whole-farm-plan.html',
  genetics:'genetics.html', prospects:'prospects.html', packet:'sales-packet.html', pipeline:'pipeline.html', spec:'product-spec.html'
};
function goto(id){ window.location.href = ROUTES[id] || 'index.html'; }
function demoAlert(msg){ alert(msg); }
function setSelected(btn, group){
  document.querySelectorAll('[data-group="'+group+'"]').forEach(x=>x.classList.remove('active-choice'));
  btn.classList.add('active-choice');
}
document.addEventListener('DOMContentLoaded',()=>{
  document.querySelectorAll('[data-demo-alert]').forEach(el=>el.addEventListener('click',()=>alert(el.dataset.demoAlert)));
});
