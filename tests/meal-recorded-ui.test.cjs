/* Native Food rendering; mocked reads only, no consumers or providers. */
const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright');
const htmlPath=process.env.FOOD_RECORDED_HTML_PATH||path.join(__dirname,'../static/webapp/index.html');
const html=fs.readFileSync(htmlPath,'utf8');
let browser;
before(async()=>{browser=await chromium.launch({channel:process.env.PLAYWRIGHT_CHANNEL||'msedge',headless:true});});
after(async()=>{await browser?.close();});

for(const width of [320,390])for(const scale of [1,2])test(`Food saved-time label ${width}px text${scale*100}%`,async t=>{
 const context=await browser.newContext({viewport:{width,height:844},isMobile:true,hasTouch:true});
 t.after(()=>context.close());
 const page=await context.newPage(),errors=[],writes=[],sessions=[];
 await page.addInitScript(()=>{
  window.Telegram={WebApp:{ready(){},expand(){},initData:'offline',initDataUnsafe:{user:{id:1}}}};
  window.Chart=class{destroy(){}update(){}};
 });
 page.on('pageerror',error=>errors.push(String(error)));
 await page.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),name=path.basename(url.pathname);
  if(request.method()!=='GET'){
   const call={path:url.pathname,method:request.method()};
   if(url.pathname==='/progressor/api/v1/sessions'&&request.method()==='POST')sessions.push(call);
   else writes.push(call);
  }
  const json=data=>route.fulfill({status:200,contentType:'application/json; charset=utf-8',body:JSON.stringify(data)});
  if(url.hostname!=='food-time.test')return route.fulfill({status:200,body:''});
  if(url.pathname.startsWith('/webapp')&&!name.endsWith('.js'))return route.fulfill({status:200,contentType:'text/html; charset=utf-8',body:html});
  if(name==='progressor-bootstrap.v1.js')return route.fulfill({status:200,contentType:'application/javascript',body:fs.readFileSync(path.join(__dirname,'../static/webapp/assets',name),'utf8')});
  if(['subscriptions.v1.js','progressor-settings.v1.js','nutrients.v1.js'].includes(name))return route.fulfill({status:200,contentType:'application/javascript',body:fs.readFileSync(path.join(__dirname,'../static/webapp',name),'utf8')});
  if(url.pathname==='/api/config')return json({project_key:'food_bot',plans:{m1:{title:'1 месяц',price_rub:399}}});
  if(url.pathname==='/api/stats/day')return json({day:'2026-10-19',totals:{kcal:600,protein:30,fat:20,carbs:80},goal:{},meals:[
   {id:1,time:'12:34',recorded_at:'2026-10-20 12:34:56',recorded_at_basis:'server_save_time',recorded_display:'2026-10-20 12:34',consumed_at:null,version:0,kcal:500,protein:25,fat:15,carbs:70,items:[{name:'<img src=x onerror="window.pwn=1">',grams:100}]},
   {id:2,time:'12:35',version:0,kcal:100,protein:5,fat:5,carbs:10,items:[]}
  ]});
  if(url.pathname==='/api/stats/history')return json({items:[]});
  if(url.pathname==='/api/stats/advanced')return json({items:[],totals:{},avg_per_day:{}});
  return route.fulfill({status:404,contentType:'application/json',body:'{}'});
 });
 await page.goto('https://food-time.test/webapp/');
 await page.waitForFunction(()=>lastDayData?.meals?.length===2);
 assert.equal(await page.locator('.meals-table th').first().innerText(),'Добавлено');
 const first=page.locator('.meals-table tbody tr').first();
 assert.match(await first.innerText(),/2026-10-20 12:34/);
 assert.doesNotMatch(await first.innerText(),/МСК/);
 assert.equal(await page.locator('.meals-table img').count(),0);
 assert.match(await page.locator('.meals-table tbody tr').nth(1).innerText(),/12:35/);
 await page.locator('#btn-edit-day-meals').click();
 assert.match(await page.locator('.meal-block h4').first().innerText(),/^Добавлено 2026-10-20 12:34/);
 assert.equal(await page.locator('#meal-editor-list img').count(),0);
 if(scale!==1)await page.evaluate(scale=>{
  const fonts=[...document.querySelectorAll('body *')].filter(el=>el.children.length===0||['BUTTON','A','SUMMARY','LABEL','INPUT','SELECT','TEXTAREA','P','H4'].includes(el.tagName)).map(el=>[el,parseFloat(getComputedStyle(el).fontSize)]);
  for(const[el,size]of fonts)if(Number.isFinite(size)&&size>0)el.style.setProperty('font-size',String(size*scale)+'px','important');
 },scale);
 const geometry=await page.evaluate(()=>({width:innerWidth,document:document.documentElement.scrollWidth,headers:[...document.querySelectorAll('.meal-block h4')].map(el=>{const rect=el.getBoundingClientRect();return {left:rect.left,right:rect.right,client:el.clientWidth,scroll:el.scrollWidth};})}));
 if(geometry.width!==width||geometry.document>width+2)console.error(JSON.stringify({geometry,overflow:await page.evaluate(()=>[...document.querySelectorAll('body *')].filter(el=>{const r=el.getBoundingClientRect();return r.left<0||r.right>320.1;}).slice(0,30).map(el=>({id:el.id,class:el.className,tag:el.tagName,text:el.innerText?.slice(0,65),width:el.getBoundingClientRect().width,right:el.getBoundingClientRect().right,scroll:el.scrollWidth,client:el.clientWidth})))}));
 assert.equal(geometry.width,width);assert.ok(geometry.document<=width+2,'saved timestamp expanded native document');
 for(const header of geometry.headers){assert.ok(header.left>=0&&header.right<=width+2);assert.ok(header.scroll<=header.client+2,'saved timestamp clipped');}
 assert.equal(await page.evaluate(()=>window.pwn),undefined);
 assert.deepEqual(errors,[]);assert.deepEqual(writes,[],'read/edit open must not mutate domain data or call AI');
 assert.ok(sessions.length<=1,'read/edit open added authentication attempts beyond ordinary bootstrap');
});
