/* Actual Food markup/script, controlled offline API; real API has separate tests. */
const {test,before,after}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),{chromium}=require('playwright');
const html=fs.readFileSync(path.join(__dirname,'../static/webapp/index.html'),'utf8');
const markup=html.match(/<details[^>]*id="next-meal-choice"[\s\S]*?<\/details>/)[0];
const css=[...html.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map(m=>m[1]).join('\n');
const script=fs.readFileSync(path.join(__dirname,'../static/webapp/next-meal.v1.js'),'utf8');
const idea=(id='beans_rice')=>({id,title:id==='beans_rice'?'Бобовые, гарнир и овощи':'Птица с гарниром и овощами',components:['Фасоль, чечевица или нут','Рис или картофель','Брокколи'],food_groups:[],amounts:null});
const base=uid=>({owner_subject:uid,day:'2026-10-21',timezone:'Europe/Moscow',available:true,unavailable_reason:null,preferences_revision:1,catalog_version:1,data_reset_revision:0,choice_revision:0,choice:null,choice_state:'none',has_current_intent:false,ideas:[idea(),idea('poultry_rice')],recorded_as_eaten:false,note:'Идеи без расчёта порции и КБЖУ. Проверяйте состав конкретных продуктов.'});
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};let browser;
before(async()=>{browser=await chromium.launch({channel:process.env.PLAYWRIGHT_CHANNEL||'msedge',headless:true});});
after(async()=>{await browser?.close();});
async function fixture(t,{width=390,theme='dark',storageBlocked=false}={}){
  const context=await browser.newContext({viewport:{width,height:850},colorScheme:theme});t.after(()=>context.close());
  const page=await context.newPage(),control={gets:0,posts:[],states:{123:base('123'),456:base('456')},receipts:new Map(),postStarted:deferred(),getStarted:deferred(),jsErrors:[]};
  page.on('pageerror',error=>control.jsErrors.push(error.message));
  await page.addInitScript(({storageBlocked})=>{window.Telegram={WebApp:{initData:'proof-A'}};window.Progressor={openConnections:()=>{window.settingsOpened=(window.settingsOpened||0)+1;}};if(storageBlocked)Storage.prototype.setItem=()=>{throw Error('storage blocked');};},{storageBlocked});
  await page.route('https://food.test/**',async route=>{
    const url=new URL(route.request().url());
    if(url.pathname==='/food')return route.fulfill({contentType:'text/html; charset=utf-8',body:`<!doctype html><html><head><meta charset="utf-8"><style>${css}body{margin:0;padding:16px}.card{max-width:100%;box-sizing:border-box}</style></head><body>${markup}<button id="native-log">Записать еду</button><script src="/next-meal.js"></script></body></html>`});
    if(url.pathname==='/next-meal.js')return route.fulfill({contentType:'application/javascript; charset=utf-8',body:script});
    if(url.pathname!=='/api/next-meal')throw Error('Unexpected offline path '+url.pathname);
    const uid=route.request().headers()['x-tg-init-data']==='proof-B'?'456':'123';
    const json=(body,status=200)=>route.fulfill({contentType:'application/json',status,body:JSON.stringify(body)});
    if(route.request().method()==='GET'){
      control.gets++;const snapshot=structuredClone(control.states[uid]);control.getStarted.resolve();
      if(control.getWait)await control.getWait.promise;
      return json(snapshot,control.getError||200);
    }
    const op=route.request().postDataJSON().operation;control.posts.push(op);control.postStarted.resolve();
    if(control.postWait)await control.postWait.promise;
    if(control.conflict)return json({detail:'changed'},409);
    const state=control.states[uid],receipt=control.receipts.get(uid+':'+op.operation_id),duplicate=!!receipt;
    if(op.data_reset_revision!==state.data_reset_revision)return json({detail:'reset'},409);
    if(!receipt){state.choice=op.action==='cancel'?null:idea(op.idea_id);state.choice_revision++;state.has_current_intent=!!state.choice;state.choice_state=state.choice?'selected':'none';control.receipts.set(uid+':'+op.operation_id,{revision:state.choice_revision});}
    if(control.loseResponse){control.loseResponse=false;return json({},503);}
    return json({...state,applied:true,duplicate,operation_id:op.operation_id,current_matches:(receipt?.revision||state.choice_revision)===state.choice_revision});
  });
  await page.goto('https://food.test/food');
  return {page,control,box:page.locator('#next-meal-choice'),open:async()=>{await page.locator('summary').click();await page.waitForFunction(()=>document.querySelectorAll('#next-meal-choice article').length||document.querySelector('[data-next-meal-status]').textContent.includes('недоступен'));}};
}
test('collapsed tool performs no requests or automatic selection',async t=>{
  const {page,control,box}=await fixture(t);assert.equal(await box.getAttribute('open'),null);await page.waitForTimeout(50);assert.equal(control.gets,0);assert.equal(control.posts.length,0);assert.equal(await page.locator('#native-log').isVisible(),true);
});
test('explicit selection and cancellation stay a plan, without food writer',async t=>{
  const {page,control,open}=await fixture(t);await open();assert.equal(control.posts.length,0);
  await page.getByRole('button',{name:'Выбрать этот вариант'}).first().click();await page.getByText('Выбрано на следующий приём',{exact:true}).waitFor();
  assert.equal(control.states[123].choice.id,'beans_rice');assert.match(await page.locator('[data-next-meal-status]').innerText(),/В дневник еда не добавлена/);
  await page.getByRole('button',{name:'Отменить выбор'}).click();await page.waitForFunction(()=>document.querySelector('[data-next-meal-status]').textContent.includes('Выбор отменён'));
  assert.equal(control.states[123].choice,null);assert.equal(control.posts.length,2);assert.notEqual(control.posts[0].operation_id,control.posts[1].operation_id);assert.deepEqual(control.jsErrors,[]);
});
test('lost response reload retries same operation once and preserves later replacement',async t=>{
  const {page,control,open}=await fixture(t);await open();control.loseResponse=true;
  await page.getByRole('button',{name:'Выбрать этот вариант'}).first().click();await page.getByRole('button',{name:'Проверить выбор'}).waitFor();
  const id=control.posts[0].operation_id;await page.reload();await page.locator('summary').click();await page.getByRole('button',{name:'Проверить выбор'}).waitFor();
  control.states[123].choice=idea('poultry_rice');control.states[123].choice_revision=2;
  await page.getByRole('button',{name:'Проверить выбор'}).click();await page.getByText('Выбрано на следующий приём',{exact:true}).waitFor();
  assert.equal(control.posts[1].operation_id,id);assert.equal(control.states[123].choice.id,'poultry_rice');assert.match(await page.locator('[data-next-meal-status]').innerText(),/актуальное состояние/);
});
test('late old-account GET cannot reveal recipe in new-account UI',async t=>{
  const {page,control,box}=await fixture(t);control.states[123].choice=idea('poultry_rice');control.getWait=deferred();
  await page.locator('summary').click();await control.getStarted.promise;
  await page.evaluate(()=>window.Telegram.WebApp.initData='proof-B');control.getWait.resolve();await page.waitForTimeout(100);
  assert.doesNotMatch(await box.innerText(),/Птица/);
  await page.locator('summary').click();control.getWait=null;await page.locator('summary').click();await page.waitForFunction(()=>document.querySelectorAll('article').length===2);
  assert.equal(control.posts.length,0);assert.doesNotMatch(await page.locator('[data-next-meal-content]').innerText(),/Выбрано на следующий/);
});
test('late success after section close does not reopen or overwrite status',async t=>{
  const {page,control,box,open}=await fixture(t);await open();control.postWait=deferred();
  await page.getByRole('button',{name:'Выбрать этот вариант'}).first().click();await control.postStarted.promise;
  await page.locator('summary').click();control.postWait.resolve();await page.waitForTimeout(100);
  assert.equal(await box.getAttribute('open'),null);assert.equal(await page.locator('[data-next-meal-content]').innerText(),'');
});
test('late old-account POST cannot display success or recipe for the new owner',async t=>{
  const {page,control,box,open}=await fixture(t);await open();control.postWait=deferred();
  await page.getByRole('button',{name:'Выбрать этот вариант'}).first().click();await control.postStarted.promise;
  await page.evaluate(()=>window.Telegram.WebApp.initData='proof-B');control.postWait.resolve();await page.waitForTimeout(1150);
  assert.doesNotMatch(await box.innerText(),/Выбрано на следующий|Выбор сохранён|Бобовые|Птица/);
  assert.equal(await page.getByRole('button',{name:'Обновить варианты'}).count(),1);
});
test('native clear-data epoch rejects a pre-reset uncertain operation',async t=>{
  const {page,control,open}=await fixture(t);await open();control.loseResponse=true;
  await page.getByRole('button',{name:'Выбрать этот вариант'}).first().click();await page.getByRole('button',{name:'Проверить выбор'}).waitFor();
  control.states[123]={...base('123'),data_reset_revision:1};
  await page.getByRole('button',{name:'Проверить выбор'}).click();await page.waitForFunction(()=>document.querySelector('[data-next-meal-status]').textContent.includes('настройки изменились'));
  assert.equal(control.states[123].choice,null);assert.equal(await page.locator('article').count(),0);
});
test('preference conflict clears stale idea and requires refresh',async t=>{
  const {page,control,open}=await fixture(t);await open();control.conflict=true;
  await page.getByRole('button',{name:'Выбрать этот вариант'}).first().click();await page.waitForFunction(()=>document.querySelector('[data-next-meal-status]').textContent.includes('настройки изменились'));
  assert.equal(await page.locator('article').count(),0);assert.equal(await page.getByRole('button',{name:'Проверить выбор'}).count(),0);assert.equal(control.states[123].choice,null);
});
test('unconfirmed restrictions have an explicit canonical settings shortcut',async t=>{
  const {page,control}=await fixture(t);control.states[123]={...base('123'),available:false,unavailable_reason:'preferences_required',ideas:[]};
  await page.locator('summary').click();await page.getByRole('button',{name:'Рацион и исключения'}).click();assert.equal(await page.evaluate(()=>window.settingsOpened),1);assert.equal(control.posts.length,0);
});
test('Core failure keeps native logging visible and offers bounded retry',async t=>{
  const {page,control}=await fixture(t);control.getError=503;await page.locator('summary').click();await page.getByRole('button',{name:'Повторить загрузку'}).waitFor();
  assert.equal(await page.locator('#native-log').isVisible(),true);assert.equal(control.posts.length,0);
});
test('blocked session storage keeps an explicit memory retry',async t=>{
  const {page,control,open}=await fixture(t,{storageBlocked:true});await open();control.loseResponse=true;
  await page.getByRole('button',{name:'Выбрать этот вариант'}).first().click();await page.getByRole('button',{name:'Проверить выбор'}).click();await page.getByText('Выбрано на следующий приём',{exact:true}).waitFor();assert.equal(control.posts[0].operation_id,control.posts[1].operation_id);
});
test('keyboard selection preserves focus inside the section',async t=>{
  const {page,open}=await fixture(t);await open();const choose=page.getByRole('button',{name:'Выбрать этот вариант'}).first();await choose.focus();await choose.press('Enter');await page.getByText('Выбрано на следующий приём',{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>document.querySelector('#next-meal-choice').contains(document.activeElement)),true);
});
for(const width of [320,390])for(const scale of [1,2])for(const theme of ['light','dark'])test(`layout ${width}px ${scale*100}% ${theme} remains readable with large actions`,async t=>{
  const {page,control,open}=await fixture(t,{width,theme});await open();
  if(scale===2)await page.evaluate(()=>{const measured=[...document.querySelectorAll('*')].map(node=>[node,getComputedStyle(node).fontSize]);for(const [node,size] of measured)node.style.fontSize=(parseFloat(size)*2)+'px';});
  const result=await page.evaluate(()=>({viewport:innerWidth,width:document.documentElement.scrollWidth,buttons:[...document.querySelectorAll('#next-meal-choice button')].map(n=>({h:n.getBoundingClientRect().height,w:n.getBoundingClientRect().width,right:n.getBoundingClientRect().right})),text:document.querySelector('#next-meal-choice').innerText}));
  assert.ok(result.width<=width+1,JSON.stringify(result));assert.ok(result.buttons.every(n=>n.h>=44&&n.w>=44&&n.right<=width+1));assert.match(result.text,/не добавляет еду/);assert.equal(control.posts.length,0);assert.deepEqual(control.jsErrors,[]);
});
