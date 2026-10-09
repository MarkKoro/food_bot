/* Food-owned payment hooks. Shared screen lives only in Progressor Core. */
(() => {
  'use strict';
  async function api(path,body){
    const headers={};const proof=window.Telegram?.WebApp?.initData||window.Progressor?.launchData;
    if(proof)headers['X-Tg-Init-Data']=proof;if(body)headers['Content-Type']='application/json';
    const r=await fetch('/api/'+path,{method:body?'POST':'GET',headers,body:body?JSON.stringify(body):undefined,cache:'no-store'});
    const v=await r.json();if(!r.ok)throw new Error(typeof v.detail==='string'?v.detail:'Не удалось подготовить оплату');return v;
  }
  const domain=window.ProgressorDomain=window.ProgressorDomain||{version:1};
  domain.subscriptionPlans=async()=>Object.entries((await api('config')).plans).map(([id,p])=>({id,label:p.title,RUB:p.price_rub*100,XTR:p.price_stars}));
  domain.purchaseSubscription=async(id,currency)=>{
    if(currency==='RUB'){
      const url=new URL(location.href);url.searchParams.set('screen','payment');url.searchParams.set('kind','subscription');url.searchParams.set('plan_id',id);url.searchParams.delete('progressor_subscription');location.assign(url.href);return;
    }
    const invoice=await api('payments/stars/create',{plan_id:id});
    if(window.Telegram?.WebApp?.openInvoice)window.Telegram.WebApp.openInvoice(invoice.invoice_link,()=>{window.dispatchEvent(new Event('focus'));});
    else window.open(invoice.invoice_link,'_blank','noopener');
  };
})();
