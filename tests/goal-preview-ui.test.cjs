/* Real settings markup/script, controlled offline replies; no Telegram/provider. */
const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright');
const html=fs.readFileSync(path.join(__dirname,'../static/webapp/index.html'),'utf8');
const settings=html.match(/<details[^>]*id="food-context-settings"[\s\S]*?<\/details>/)[0];
const script=fs.readFileSync(path.join(__dirname,'../static/webapp/progressor-settings.v1.js'),'utf8');
const profile={sex_at_birth:'male',age_years:36,height_cm:180,weight_kg:80,activity:'moderate',goal:'maintain',training:'strength'};
const defaults={profile,sharing_enabled:false,workout_enabled:false,nutrient_enabled:false,timezone:'Europe/Moscow'};
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
let browser;
before(async()=>{browser=await chromium.launch({channel:process.env.PLAYWRIGHT_CHANNEL||'msedge',headless:true});});
after(async()=>{await browser?.close();});
async function fixture(t,opened=false){
  const context=await browser.newContext();t.after(()=>context.close());
  const page=await context.newPage();
  const state={profileStarted:deferred(),planStarted:deferred(),applyStarted:deferred(),previews:0,applied:[],legacyApplyWrites:0};
  await page.route('https://food.test/**',async route=>{
    const url=new URL(route.request().url());
    const json=(body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
    if(url.pathname==='/settings')return route.fulfill({contentType:'text/html; charset=utf-8',body:`<!doctype html><html><head><meta charset="utf-8"></head><body>${settings}${opened?'<script>document.querySelector("details").open=true</script>':''}<script src="/settings.js"></script></body></html>`});
    if(url.pathname==='/settings.js'){
      if(opened)await new Promise(r=>setTimeout(r,50));
      return route.fulfill({contentType:'application/javascript; charset=utf-8',body:script});
    }
    if(url.pathname==='/api/progressor/preferences')return json(defaults);
    const body=route.request().postDataJSON();
    if(url.pathname==='/api/progressor/profile'){
      state.profileStarted.resolve();if(state.profileReply)await state.profileReply.promise;
      return json({...defaults,profile:body.profile,sharing_enabled:body.sharing_enabled});
    }
    if(url.pathname==='/api/progressor/profile/plan'||url.pathname==='/api/progressor/profile/plan/confirm'){
      if(body.apply){
        // An older API ignores the unknown preview token on its old route.
        // The new route must fail closed instead of falling back to that writer.
        if(state.olderApi){
          if(url.pathname.endsWith('/confirm'))return json({detail:'Not Found'},404);
          state.legacyApplyWrites++;
          return json({saved:true,current_goal_matches:true,targets:{kcal:9999}});
        }
        state.applied.push(body.preview_id);state.applyStarted.resolve();
        if(state.applyReply)await state.applyReply.promise;
        if(state.applyError){const code=state.applyError;state.applyError=0;return json({detail:'offline or changed'},code);}
        return json({saved:true,current_goal_matches:true});
      }
      const number=++state.previews;state.planStarted.resolve();if(state.planReply)await state.planReply.promise;
      return json({status:'estimated',preview_id:'preview-'+number,targets:{kcal:2725,protein:128,fat:91,carbs:350},provenance:{weight_kg:{source:'food'}}});
    }
    throw new Error('Unexpected offline route '+url.pathname);
  });
  await page.goto('https://food.test/settings');
  if(!opened)await page.locator('summary').click();
  await page.waitForFunction(()=>document.querySelector('[name=weight_kg]').value==='80');
  return {page,state,apply:page.locator('[data-apply]'),calculate:page.locator('[data-calculate]'),weight:page.locator('[name=weight_kg]')};
}
test('an entry that opened settings before the script loads still reads the profile',async t=>{
  const {state,weight}=await fixture(t,true);
  assert.equal(await weight.inputValue(),'80');assert.equal(state.previews,0);assert.deepEqual(state.applied,[]);
});
test('a delayed profile save preserves a newer field edit',async t=>{
  const {page,state,weight}=await fixture(t);state.profileReply=deferred();
  await page.locator('button[type=submit]').click();await state.profileStarted.promise;
  await weight.fill('100');state.profileReply.resolve();
  await page.waitForFunction(()=>!document.querySelector('button[type=submit]').disabled);
  assert.equal(await weight.inputValue(),'100');
});
test('a delayed preview cannot restore confirmation after a newer profile edit',async t=>{
  const {page,state,weight,calculate,apply}=await fixture(t);state.planReply=deferred();
  await calculate.click();await state.planStarted.promise;
  await weight.fill('100');state.planReply.resolve();
  await page.waitForFunction(()=>!document.querySelector('[data-calculate]').disabled);
  assert.equal(await weight.inputValue(),'100');assert.equal(await apply.isVisible(),false);
});
test('a changed shared-data choice invalidates an in-flight calculation',async t=>{
  const {page,state,calculate,apply}=await fixture(t);state.planReply=deferred();
  await calculate.click();await state.planStarted.promise;
  await page.locator('[name=use_shared]').check();state.planReply.resolve();
  await page.waitForFunction(()=>!document.querySelector('[data-calculate]').disabled);
  assert.equal(await apply.isVisible(),false);
});
for(const code of [0,409])test('a late apply '+code+' cannot hide a newer preview',async t=>{
  const {page,state,calculate,apply,weight}=await fixture(t);
  await calculate.click();await apply.waitFor({state:'visible'});
  state.applyReply=deferred();state.applyError=code;
  await apply.click();await state.applyStarted.promise;
  await weight.fill('100');await calculate.click();await apply.waitFor({state:'visible'});
  state.applyReply.resolve();
  await page.waitForFunction(()=>!document.querySelector('[data-apply]').disabled);
  assert.equal(await apply.isVisible(),true);
  state.applyReply=null;await apply.click();
  assert.deepEqual(state.applied,['preview-1','preview-2']);
});
test('retry after a failed response keeps the same preview token',async t=>{
  const {page,state,calculate,apply}=await fixture(t);
  await calculate.click();await apply.waitFor({state:'visible'});state.applyError=503;
  await apply.click();await page.waitForFunction(()=>!document.querySelector('[data-apply]').disabled);
  assert.equal(await apply.isVisible(),true);await apply.click();
  await page.waitForFunction(()=>document.querySelector('[data-apply]').hidden);
  assert.deepEqual(state.applied,['preview-1','preview-1']);
});
test('an older API after preview cannot silently use its legacy goal writer',async t=>{
  const {page,state,calculate,apply}=await fixture(t);
  await calculate.click();await apply.waitFor({state:'visible'});
  state.olderApi=true;
  await apply.click();await page.waitForFunction(()=>!document.querySelector('[data-apply]').disabled);
  assert.equal(state.legacyApplyWrites,0);
  assert.equal(await apply.isVisible(),true);
  assert.match(await page.locator('[role=status]').textContent(),/обновите экран/i);
  state.olderApi=false;
  await apply.click();await page.waitForFunction(()=>document.querySelector('[data-apply]').hidden);
  assert.deepEqual(state.applied,['preview-1']);
});
