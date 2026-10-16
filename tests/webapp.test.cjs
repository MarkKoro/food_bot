const {test,before,after} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const html = fs.readFileSync(path.join(__dirname,'../static/webapp/index.html'),'utf8');
let browser;
before(async () => { browser = await chromium.launch({channel:process.env.PLAYWRIGHT_CHANNEL || 'msedge',headless:true}); });
after(async () => { await browser?.close(); });

async function fixture(t, query='') {
  const context = await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true});
  t.after(() => context.close());
  const page = await context.newPage();
  const state = {patches:[],creates:0,errors:[],failNext:false,patchDelay:0,dayDelays:{},version:0};
  const items = [{name:'<img src=x onerror="window.pwn=1">',grams:100,kcal:200,protein:10,fat:5,carbs:25},
                 {name:'Рис',grams:150,kcal:300,protein:5,fat:2,carbs:60}];
  await page.addInitScript(() => {
    window.Telegram={WebApp:{ready(){},expand(){},initData:'offline',initDataUnsafe:{user:{id:1}},openLink(url){window.openedPayment=url;}}};
    window.Chart=class {constructor(){ } destroy(){} update(){}};
  });
  page.on('pageerror',err => state.errors.push(String(err)));
  await page.route('**/*',async route => {
    const url=new URL(route.request().url());
    const json=data => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
    if (url.hostname!=='food.test') return route.fulfill({status:200,body:''});
    const asset=path.basename(url.pathname);
    if (asset==='progressor-bootstrap.v1.js') return route.fulfill({status:200,contentType:'application/javascript',body:fs.readFileSync(path.join(__dirname,'../static/webapp/assets',asset),'utf8')});
    if (['subscriptions.v1.js','progressor-settings.v1.js','nutrients.v1.js'].includes(asset)) return route.fulfill({status:200,contentType:'application/javascript',body:fs.readFileSync(path.join(__dirname,'../static/webapp',asset),'utf8')});
    if (url.pathname.startsWith('/webapp')) return route.fulfill({status:200,contentType:'text/html',body:html});
    if (url.pathname==='/api/config') return json({project_key:'food_bot',plans:{m1:{title:'1 месяц',price_rub:555}},payg_cost:3});
    if (url.pathname==='/api/payments/receipt-email') return json({receipt_email_status:'missing'});
    if (url.pathname==='/api/payments/redirect/create') {
      state.creates++;
      await new Promise(r=>setTimeout(r,100));
      return json({payment_id:'p1',confirmation_url:'https://yookassa.ru/pay',amount_rub:555,project_key:'food_bot'});
    }
    if (url.pathname==='/api/payments/yookassa/status') return json({status:'pending',finalized:false});
    if (url.pathname.startsWith('/api/meals/')) {
      const index=Number(url.pathname.split('/').at(-1));
      const body=route.request().postDataJSON();
      if (route.request().method()==='PATCH') {
        state.patches.push(body);
        if (state.patchDelay) await new Promise(r=>setTimeout(r,state.patchDelay));
        if (state.failNext) { state.failNext=false; return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'offline'})}); }
        if (body.version!==state.version) return route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({detail:'conflict'})});
        items[index].grams=body.grams;
        return json({ok:true,version:++state.version});
      }
      if (route.request().method()==='DELETE') {
        items.splice(index,1); state.version++;
        return json({ok:true,version:state.version});
      }
    }
    if (url.pathname==='/api/stats/day') {
      const day=url.searchParams.get('date');
      if (state.dayDelays[day]) await new Promise(r=>setTimeout(r,state.dayDelays[day]));
      return json({day,totals:{kcal:500,protein:15,fat:7,carbs:85},goal:{kcal:2000,protein:100,fat:60,carbs:250},meals:[{id:1,time:'12:00',version:state.version,items,kcal:500,protein:15,fat:7,carbs:85}]});
    }
    if (url.pathname==='/api/stats/history') return json({items:[]});
    if (url.pathname==='/api/stats/advanced') return json({items:[],totals:{},avg_per_day:{}});
    return route.fulfill({status:404,body:'unknown test route'});
  });
  await page.goto('https://food.test/webapp/'+query);
  await page.waitForFunction(() => !!lastDayData);
  return {page,state,items};
}

test('mobile layout, escaped meal names and select-all on focus',async t=>{
  const {page,state}=await fixture(t);
  await page.click('#btn-edit-day-meals');
  const input=page.locator('[data-action=grams-input]').first();
  await input.click();
  assert.deepEqual(await input.evaluate(el=>[el.selectionStart,el.selectionEnd]),[0,3]);
  assert.equal(await page.evaluate(()=>window.pwn),undefined);
  assert.equal(await page.locator('#meal-editor-list img').count(),0);
  const before=await page.locator('.meal-editor-card').boundingBox();
  await page.setViewportSize({width:390,height:540});
  const after=await page.locator('.meal-editor-card').boundingBox();
  assert.ok(Math.abs(before.y-after.y)<2,'modal stays anchored when keyboard reduces viewport');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  assert.deepEqual(state.errors,[]);
  await page.screenshot({path:path.join(__dirname,'../../food-ui-audit.png'),fullPage:true});
});

test('switch fields while save runs: focus and second value survive, Done saves both',async t=>{
  const {page,state,items}=await fixture(t);
  state.patchDelay=150;
  await page.click('#btn-edit-day-meals');
  const inputs=page.locator('[data-action=grams-input]');
  await inputs.nth(0).fill('220');
  await inputs.nth(1).click();
  await inputs.nth(1).fill('330');
  await page.waitForFunction(()=>lastDayData.meals[0].version>=1);
  assert.equal(await inputs.nth(1).inputValue(),'330');
  assert.equal(await inputs.nth(1).evaluate(el=>el===document.activeElement),true);
  await page.click('#meal-editor-close');
  await page.waitForFunction(()=>!mealEditorOpen);
  assert.deepEqual(items.map(it=>it.grams),[220,330]);
  assert.deepEqual(state.errors,[]);
});

test('failed save retains draft and retry succeeds',async t=>{
  const {page,state,items}=await fixture(t);
  state.failNext=true;
  await page.click('#btn-edit-day-meals');
  await page.locator('[data-action=grams-input]').first().fill('222');
  await page.locator('[data-action=grams-input]').first().press('Tab');
  await page.waitForFunction(()=>document.getElementById('meal-editor-status').textContent.includes('Не удалось'));
  assert.equal(items[0].grams,100);
  assert.equal(await page.evaluate(()=>Object.keys(mealDrafts).length),1);
  await page.click('#meal-editor-retry');
  await page.waitForFunction(()=>Object.keys(mealDrafts).length===0);
  assert.equal(items[0].grams,222);
});

test('typed unsaved value survives reload',async t=>{
  const {page}=await fixture(t);
  await page.click('#btn-edit-day-meals');
  await page.locator('[data-action=grams-input]').first().fill('275');
  await page.reload();
  await page.waitForFunction(()=>!!lastDayData);
  await page.click('#btn-edit-day-meals');
  assert.equal(await page.locator('[data-action=grams-input]').first().inputValue(),'275');
});

test('old day response cannot overwrite a newer selection',async t=>{
  const {page,state}=await fixture(t);
  state.dayDelays['2026-09-01']=200;
  await page.evaluate(async()=>{
    const older=loadDay('date','2026-09-01');
    await loadDay('date','2026-09-02');
    await older;
  });
  assert.equal(await page.evaluate(()=>lastDayData.day),'2026-09-02');
});

test('rapid payment preparation reuses one request and shows actual quoted amount',async t=>{
  const {page,state}=await fixture(t,'?pay=sub&plan=m1');
  // The link format is exercised separately via the actual subscription button.
  await page.evaluate(()=>startRedirectPayment({kind:'subscription',plan_id:'m1'}));
  await page.evaluate(()=>Promise.all([startRedirectPayment({kind:'subscription',plan_id:'m1'}),startRedirectPayment({kind:'subscription',plan_id:'m1'})]));
  assert.equal(state.creates,1); // An existing pending invoice is reused even after the first request completes.
  assert.ok((await page.locator('#payment-amount').textContent()).includes('555'));
  assert.deepEqual(state.errors,[]);
});

test('invalid empty grams cannot close or erase a meal',async t=>{
  const {page,state}=await fixture(t);
  await page.click('#btn-edit-day-meals');
  await page.locator('[data-action=grams-input]').first().fill('');
  await page.click('#meal-editor-close');
  assert.equal(await page.evaluate(()=>mealEditorOpen),true);
  assert.equal(state.patches.length,0);
});
