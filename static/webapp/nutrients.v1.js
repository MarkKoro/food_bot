/* Read-only saved food composition. AI fills it together with Kcal/P/F/C. */
(() => {
  'use strict';
  function node(tag,text,parent){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(parent)parent.append(n);return n;}
  async function api(path){const headers={};const proof=window.Telegram?.WebApp?.initData||window.Progressor?.launchData;if(proof)headers['X-Tg-Init-Data']=proof;const r=await fetch('/api/nutrients/'+path,{headers,cache:'no-store'});if(!r.ok)throw new Error('Не удалось загрузить состав. Попробуйте позже.');return r.json();}
  function mount(){
    const root=document.querySelector('.app');if(!root)return;
    const details=node('details');details.id='food-nutrients-summary';details.className='card';node('summary','Состав пищи · оценка ИИ',details);
    node('p','Food автоматически оценивает нутриенты вместе с КБЖУ по фото, тексту и голосовому описанию. Значения приблизительные; неизвестный состав остаётся без данных.',details);
    const message=node('p','Откройте, чтобы посмотреть сохранённый состав.',details);message.setAttribute('role','status');
    const label=node('label','Приём пищи ',details);const select=node('select',undefined,label);select.setAttribute('aria-label','Приём пищи для просмотра состава');select.style.cssText='min-height:48px;max-width:100%;font:inherit';label.hidden=true;
    const rows=node('div',undefined,details);let meals=[],catalog=[];
    function render(){rows.replaceChildren();const meal=meals.find(m=>String(m.id)===select.value);if(!meal)return;const observations=new Map(meal.observations.map(n=>[n.nutrient_code,n]));
      for(const n of catalog){const o=observations.get(n.code);const known=o&&!o.stale&&o.value!==null;const amount=known?Number(o.value.toFixed(4)).toLocaleString('ru-RU')+' '+({ug:'мкг',mg:'мг',g:'г'}[n.unit]||n.unit):'нет данных';let coverage={};try{coverage=JSON.parse(o?.coverage_json||'{}');}catch(_){}
        const partial=known&&coverage.observed_components<coverage.total_components?' · часть состава':'';node('p',n.name+': '+amount+partial,rows);
      }
    }
    details.addEventListener('toggle',async()=>{if(!details.open)return;message.textContent='Загрузка…';try{const [c,m]=await Promise.all([api('catalog'),api('meals')]);catalog=c.nutrients;meals=m.items;select.replaceChildren();for(const meal of meals){const option=node('option',meal.date+' · '+meal.title,select);option.value=String(meal.id);}label.hidden=!meals.length;message.textContent=meals.length?'Состав рассчитан за съеденную порцию. Пустые значения не считаются нулём.':'Добавьте фото или описание еды в боте Food — состав появится автоматически.';render();}catch(e){message.textContent=e.message;}});
    select.addEventListener('change',render);root.append(details);
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mount,{once:true});else mount();
})();
