/* Explicit owner plan, separate from recorded food. No AI or automatic logging. */
(() => {
  'use strict';
  const box=document.getElementById('next-meal-choice');if(!box)return;
  const content=box.querySelector('[data-next-meal-content]'),notice=box.querySelector('[data-next-meal-status]'),actions=box.querySelector('[data-next-meal-actions]');
  const style=document.createElement('style');style.textContent=`
    .next-meal-choice summary{min-height:48px;display:flex;align-items:center;cursor:pointer;font-weight:600}
    .next-meal-choice p,.next-meal-choice h3,.next-meal-choice button{overflow-wrap:anywhere}
    .next-meal-choice article{border:1px solid var(--border,rgba(148,163,184,.3));padding:12px;border-radius:12px;margin:12px 0}
    .next-meal-choice h3{font-size:1em;margin:0 0 8px}
    .next-meal-choice button{display:block;width:100%;white-space:normal;min-height:48px;margin:8px 0;padding:10px 12px;font:inherit;border-radius:12px;cursor:pointer}
    .next-meal-choice :focus-visible{outline:2px solid var(--accent,#3b82f6);outline-offset:3px}
  `;document.head.append(style);
  let state=null,owner=null,pending=null,busy=false,serial=0,proofAtOpen='',restoreFocus=false;
  const launch=()=>{
    const saved=window.Progressor?._launchBridge?.getState?.();
    return saved?.source&&saved.source!=='food'?saved.launchData||'':window.Telegram?.WebApp?.initData||saved?.launchData||window.Progressor?.launchData||'';
  };
  const scope=()=>launch();
  const key=uid=>'food.next-meal.pending.v1:'+uid;
  const removeSaved=uid=>{try{sessionStorage.removeItem(key(uid));}catch{}};
  function element(tag,text,parent=content){const node=document.createElement(tag);if(text!==null)node.textContent=text;parent.append(node);return node;}
  function button(text,fn,parent=actions){const b=element('button',text,parent);b.type='button';b.className='pill pill-secondary';b.disabled=busy;b.onclick=fn;return b;}
  async function proofHash(value){if(!crypto?.subtle)throw Error('Не удалось подтвердить сессию. Откройте Food заново из бота.');return [...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(value)))].map(n=>n.toString(16).padStart(2,'0')).join('');}
  function clearIdentity(){serial++;state=null;owner=null;pending=null;busy=false;content.replaceChildren();actions.replaceChildren();notice.textContent='Сессия изменилась. Обновите варианты.';button('Обновить варианты',load);}
  const active=(ticket,proof)=>ticket===serial&&box.open&&proof===scope();
  async function request(operation,proof){
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),5000);
    try{
      const response=await fetch('/api/next-meal',{method:operation?'POST':'GET',headers:{...(proof?{'X-Tg-Init-Data':proof}:{}),...(operation?{'Content-Type':'application/json'}:{})},body:operation?JSON.stringify({operation}):undefined,signal:controller.signal,cache:'no-store'});
      if(!response.ok){const error=Error(response.status===409?'Выбор или настройки изменились. Обновите варианты.':response.status===401?'Откройте Food заново из бота.':response.status===403?'Нужен действующий доступ Food.':'Выбор пока недоступен. Еду можно записывать как обычно.');error.status=response.status;throw error;}
      const result=await response.json();
      if(!result||typeof result.owner_subject!=='string'||!/^[1-9][0-9]*$/.test(result.owner_subject))throw Error('Сессия не подтверждена.');
      return result;
    }finally{clearTimeout(timer);}
  }
  function render(){
    if(box.contains(document.activeElement)&&document.activeElement.tagName==='BUTTON')restoreFocus=true;
    renderContent();
    if(!busy&&restoreFocus&&document.activeElement===document.body){notice.tabIndex=-1;notice.focus({preventScroll:true});}
    if(!busy)restoreFocus=false;
  }
  function renderContent(){
    content.replaceChildren();actions.replaceChildren();if(!state)return;
    if(pending){element('p','Ответ на ваш выбор пока не подтверждён. Проверка повторяет ту же операцию; второй выбор не создаётся.');button('Проверить выбор',send);return;}
    if(!state.available){
      const reasons={preferences_required:'Сначала выберите рацион и исключения в общих настройках. Если исключений нет, это тоже нужно подтвердить.',preferences_unavailable:'Настройки питания не удалось прочитать. Проверьте рацион и исключения.',food_connection_disabled:'Связь Food отключена. Её можно включить в общих настройках.',food_access_required:'Нужен действующий доступ Food. Другие продукты не требуются.'};
      element('p',reasons[state.unavailable_reason]||'Данные связи пока недоступны. Записывать съеденное можно как обычно.');
      if(['preferences_required','preferences_unavailable','food_connection_disabled'].includes(state.unavailable_reason))button('Рацион и исключения',()=>window.Progressor?.openConnections?.());
      button('Обновить варианты',load);return;
    }
    if(state.choice){element('h3','Выбрано на следующий приём');element('p',state.choice.title);element('p',state.choice.components.join(' · '));element('p','Это план, не запись съеденного.');button('Отменить выбор',()=>choose(null));}
    else if(state.has_current_intent)element('p','Настройки или каталог изменились. Проверьте их и выберите заново; прежний вариант не считается подходящим.');
    else if(state.choice_state==='different_day')element('p','Прошлый выбор относится к другому дню. На сегодня ничего не выбрано.');
    element('p',state.choice?'Можно заменить выбор:':'До трёх идей по вашим настройкам:');
    for(const idea of state.ideas||[]){const article=element('article',null);element('h3',idea.title,article);element('p',idea.components.join(' · '),article);button(state.choice?.id===idea.id?'Уже выбрано':state.choice?'Заменить на этот вариант':'Выбрать этот вариант',()=>choose(idea.id),article).disabled=busy||state.choice?.id===idea.id;}
    if(!state.ideas?.length)element('p','Для выбранных исключений нет варианта в этом небольшом каталоге. Можно выбрать еду самостоятельно.');
    element('p',state.note);button('Обновить варианты',load);
  }
  async function load(){
    const proof=scope();if(proofAtOpen&&proofAtOpen!==proof)clearIdentity();proofAtOpen=proof;
    const ticket=++serial;busy=true;state=null;render();notice.textContent='Загружаем ваши варианты…';
    try{
      const value=await request(null,proof);if(!active(ticket,proof))return;
      if(owner&&owner!==value.owner_subject){pending=null;state=null;}
      owner=value.owner_subject;state=value;
      if(!pending){try{const stored=JSON.parse(sessionStorage.getItem(key(owner))||'null');const hash=await proofHash(proof);if(!active(ticket,proof))return;if(stored?.owner===owner&&stored.proof===hash&&stored.expires>Date.now()&&stored.operation)pending=stored.operation;else removeSaved(owner);}catch{}}
      notice.textContent=pending?'Проверьте результат прежнего выбора.':'';
    }catch(error){if(active(ticket,proof)){state=null;notice.textContent=error.message;}}
    finally{if(active(ticket,proof)){busy=false;render();if(!state)button('Повторить загрузку',load);}}
  }
  async function choose(idea){
    if(busy||!state?.available||pending)return;
    const proof=scope(),ticket=++serial,uid=owner;busy=true;render();
    try{
      const hash=await proofHash(proof);if(!active(ticket,proof)||owner!==uid)return;
      if(typeof crypto.randomUUID!=='function')throw Error('Откройте Food в актуальной версии Telegram.');
      pending={operation_id:crypto.randomUUID().replaceAll('-',''),action:idea?'choose':'cancel',day:state.day,choice_revision:state.choice_revision,preferences_revision:state.preferences_revision,catalog_version:state.catalog_version,data_reset_revision:state.data_reset_revision,...(idea?{idea_id:idea}:{})};
      try{sessionStorage.setItem(key(uid),JSON.stringify({owner:uid,proof:hash,expires:Date.now()+86400000,operation:pending}));}catch{}
    }catch(error){if(active(ticket,proof))notice.textContent=error.message;}
    finally{if(active(ticket,proof)){busy=false;render();}}
    if(active(ticket,proof)&&pending)await send();
  }
  async function send(){
    if(busy||!pending||!owner)return;
    if(proofAtOpen!==scope()){clearIdentity();return;}
    const proof=scope(),ticket=++serial,uid=owner,operation=pending;busy=true;render();notice.textContent='Проверяем выбор…';
    try{
      const value=await request(operation,proof);if(!active(ticket,proof)||owner!==uid||pending!==operation)return;
      if(value.owner_subject!==uid)throw Error('Сессия изменилась. Обновите варианты.');
      state=value;pending=null;removeSaved(uid);
      notice.textContent=value.current_matches?(operation.action==='cancel'?'Выбор отменён. Дневник еды не менялся.':'Выбор сохранён как план. В дневник еда не добавлена.'):'Прежняя операция была принята. Сейчас показано ваше актуальное состояние.';
    }catch(error){
      if(active(ticket,proof)){
        if([401,403,409].includes(error.status)){pending=null;removeSaved(uid);state=null;}
        notice.textContent=error.status===409?error.message:pending?'Ответ не подтверждён. Нажмите «Проверить выбор»; записывать еду можно как обычно.':error.message;
      }
    }finally{if(active(ticket,proof)){busy=false;render();if(!state)button('Обновить варианты',load);}}
  }
  box.addEventListener('toggle',()=>{if(box.open)void load();else{serial++;busy=false;content.replaceChildren();actions.replaceChildren();notice.textContent='';}});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden&&box.open){if(proofAtOpen!==scope())clearIdentity();void load();}});
  // Only an identity comparison, never a polling request or a hint.
  setInterval(()=>{if(box.open&&proofAtOpen!==scope()){clearIdentity();proofAtOpen=scope();}},1000);
  if(box.open)void load();
})();
