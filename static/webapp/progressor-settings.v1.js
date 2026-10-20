/* Food-owned settings. No AI request is issued by opening, editing or calculating. */
(() => {
  'use strict';
  const root=document.getElementById('food-context-settings');
  if(!root) return;
  const status=root.querySelector('[role=status]');
  const form=root.querySelector('form');
  const reminders=root.querySelector('[data-reminders]');
  let loaded=false,loading=false,settings=null,plan=null,editRevision=0,actionRevision=0,profileBusy=false;
  const applyButton=root.querySelector('[data-apply]');
  const calculateButton=root.querySelector('[data-calculate]');
  const saveButton=form.querySelector('button[type=submit]');
  const fields=['sex_at_birth','age_years','height_cm','weight_kg','activity','goal','training'];
  const input=name=>root.querySelector('[name="'+name+'"]');
  async function api(path,body){
    const headers={};const init=window.Telegram?.WebApp?.initData || window.Progressor?.launchData;
    if(init) headers['X-Tg-Init-Data']=init;
    if(body) headers['Content-Type']='application/json';
    const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),3000);
    try{
      const response=await fetch('/api/progressor/'+path,{method:body?'POST':'GET',headers,body:body?JSON.stringify(body):undefined,cache:'no-store',signal:controller.signal});
      const data=await response.json();
      if(!response.ok){
        const unavailable=path==='profile/plan/confirm'&&[404,405].includes(response.status);
        const error=new Error(unavailable?'Версия сервера изменилась. Обновите экран перед сохранением; цель этим запросом не заменена.':typeof data.detail==='string'?data.detail:'Не удалось сохранить. Попробуйте ещё раз.');
        error.status=response.status;throw error;
      }
      return data;
    }finally{clearTimeout(timeout);}
  }
  function fill(data){
    settings=data;
    fields.forEach(k=>{input(k).value=data.profile[k] ?? '';});
    input('has_special_constraints').checked=!!data.profile.has_special_constraints;
    input('sharing_enabled').checked=data.sharing_enabled;
    input('workout_enabled').checked=data.workout_enabled;
    input('nutrient_enabled').checked=data.nutrient_enabled;
    root.querySelector('[data-profile-date]').textContent=data.profile_updated_at?'Сохранено: '+new Date(data.profile_updated_at).toLocaleDateString('ru-RU'):'';
    // This page must preserve diet restrictions not represented by its basic controls.
  }
  async function load(){
    if(loaded||loading) return;loading=true;
    try{fill(await api('preferences'));loaded=true;status.textContent='';}
    catch(e){status.textContent=e.message;}
    finally{loading=false;}
  }
  root.addEventListener('toggle',()=>{if(root.open) load();});
  if(root.open)load();
  function values(){
    const p={...settings?.profile};
    for(const k of fields){p[k]=input(k).value;}
    p.has_special_constraints=input('has_special_constraints').checked;
    return p;
  }
  function clearPlan(){plan=null;applyButton.hidden=true;}
  function profilePending(pending){
    profileBusy=pending;saveButton.disabled=pending;calculateButton.disabled=pending;
  }
  async function save(revision){
    if(!loaded) throw new Error('Сначала дождитесь загрузки профиля.');
    const saved=await api('profile',{profile:values(),sharing_enabled:input('sharing_enabled').checked});
    settings=saved;
    // Keep edits entered while the request was pending. They still need saving.
    if(revision!==editRevision)return false;
    fill(saved);return true;
  }
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(profileBusy)return;
    const action=++actionRevision,revision=editRevision;clearPlan();profilePending(true);
    const current=()=>action===actionRevision&&revision===editRevision;
    try{if(await save(revision)&&current())status.textContent='Профиль сохранён. Цель питания не менялась.';}
    catch(e){if(current())status.textContent=e.message;}finally{profilePending(false);}
  });
  calculateButton.addEventListener('click',async()=>{
    if(profileBusy)return;
    const action=++actionRevision,revision=editRevision,useShared=input('use_shared').checked;
    const current=()=>action===actionRevision&&revision===editRevision;
    clearPlan();profilePending(true);
    try{
      if(!await save(revision)||!current())return;
      const calculated=await api('profile/plan',{use_shared:useShared,apply:false});
      if(!current())return;
      plan=calculated;
      if(plan.status!=='estimated'){
        const names={sex_at_birth:'пол',age_years:'возраст',height_cm:'рост',weight_kg:'вес',activity:'активность',goal:'цель'};
        status.textContent=plan.status==='incomplete_profile'?'Укажите: '+plan.missing_fields.map(k=>names[k]||k).join(', ')+'.':plan.status==='conflicting_profile'?'Недавние значения профилей расходятся. Уточните их перед расчётом.':'Задайте согласованную цель вручную в настройках бота.';
        return;
      }
      const t=plan.targets;
      const names={food:'Food',supply:'Supply',gym:'Gym'};
      const sources=[...new Set(Object.values(plan.provenance||{}).map(v=>v?.source).filter(Boolean))];
      status.textContent=`Ориентир: ${t.kcal} ккал · Б ${t.protein} г · Ж ${t.fat} г · У ${t.carbs} г. Обычная активность уже учтена. Источники: ${sources.map(s=>names[s]||s).join(', ')}. Сохранение заменит текущую цель.`;
      applyButton.hidden=false;
    }catch(e){if(current())status.textContent=e.message;}finally{profilePending(false);}
  });
  applyButton.addEventListener('click',async event=>{
    const button=event.currentTarget;button.disabled=true;
    const action=++actionRevision,revision=editRevision,chosen=plan;
    const current=()=>action===actionRevision&&revision===editRevision;
    try{
      if(!chosen?.preview_id)throw new Error('Сначала рассчитайте ориентир.');
      const receipt=await api('profile/plan/confirm',{use_shared:input('use_shared').checked,apply:true,preview_id:chosen.preview_id});
      document.dispatchEvent(new Event('food:goal-updated'));
      if(!current())return;
      status.textContent=receipt.current_goal_matches===false?'Этот ориентир уже сохранялся. Позже цель изменена; оставили последнее значение.':'Цель сохранена.';
      clearPlan();
    }
    catch(e){if(current()){status.textContent=e.message;if(e.status===409)clearPlan();}}
    finally{button.disabled=false;}
  });
  // Editing after preview requires a new calculation; never apply an unseen profile.
  function edited(){editRevision++;clearPlan();status.textContent='Есть новые изменения. Сохраните профиль или рассчитайте ориентир ещё раз.';}
  form.addEventListener('input',edited);
  input('use_shared').addEventListener('change',edited);
  reminders.addEventListener('change',async()=>{
    if(!loaded) return;
    const controls=[input('workout_enabled'),input('nutrient_enabled')];controls.forEach(c=>c.disabled=true);
    try{
      const timezone=Intl.DateTimeFormat().resolvedOptions().timeZone || settings.timezone;
      settings=await api('preferences',{workout_enabled:controls[0].checked,nutrient_enabled:controls[1].checked,timezone});
      status.textContent='Настройки напоминаний сохранены. Ночью сообщения не приходят.';
    }catch(e){controls[0].checked=settings.workout_enabled;controls[1].checked=settings.nutrient_enabled;status.textContent=e.message;}
    finally{controls.forEach(c=>c.disabled=false);}
  });
})();
