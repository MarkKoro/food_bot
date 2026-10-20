/* Native Food markup/CSS, offline layout regression; no application/API/provider. */
const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright');
const file=process.env.FOOD_LAYOUT_HTML_PATH||path.join(__dirname,'../static/webapp/index.html');
const html=fs.readFileSync(file,'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,'');
let browser;
before(async()=>{browser=await chromium.launch({channel:process.env.PLAYWRIGHT_CHANNEL||'msedge',headless:true});});
after(async()=>{await browser?.close();});
for(const width of [320,390])for(const scale of [1,2])for(const longLabels of [false,true])test(`Food headers ${width}px text${scale*100}% longLabels=${longLabels}`,async t=>{
 const context=await browser.newContext({viewport:{width,height:844},isMobile:true});t.after(()=>context.close());
 await context.route('**/*',route=>route.abort());const page=await context.newPage();await page.setContent(html);
 await page.locator('#selected-date-label').evaluate(el=>{el.textContent='19.10.2026';});
 if(longLabels)await page.evaluate(()=>{document.querySelector('.date-label').textContent='Выбрать дату записанного питания';document.querySelector('#btn-edit-day-meals').textContent='Редактировать сохранённые приёмы пищи';});
 if(scale!==1)await page.evaluate(scale=>{
  // Snapshot first: mutating parents before reading descendants would multiply
  // inherited sizes twice and would not represent a200% text test.
  const fonts=[...document.querySelectorAll('body *')].filter(el=>el.children.length===0 || ['BUTTON','A','SUMMARY','LABEL','INPUT','SELECT','TEXTAREA','P'].includes(el.tagName)).map(el=>[el,parseFloat(getComputedStyle(el).fontSize)]);
  for(const[el,size]of fonts)if(Number.isFinite(size)&&size>0)el.style.setProperty('font-size',String(size*scale)+'px','important');
 },scale);
 const geometry=await page.evaluate(()=>({layout:innerWidth,visual:visualViewport.width,document:document.documentElement.scrollWidth,buttons:['btn-date','btn-edit-day-meals'].map(id=>{const el=document.getElementById(id),b=el.getBoundingClientRect();return {id,left:b.left,right:b.right,width:b.width,height:b.height,client:el.clientWidth,scroll:el.scrollWidth};})}));
 assert.equal(geometry.layout,width,'native headers expanded mobile layout viewport');assert.equal(geometry.visual,width);assert.ok(geometry.document<=width+2,'horizontal document overflow');
 for(const b of geometry.buttons){assert.ok(b.left>=0&&b.right<=width+2,b.id+' outside viewport');assert.ok(b.height>=44,b.id+' target smaller than44px');assert.ok(b.scroll<=b.client+2,b.id+' text clipped');}
 for(const id of ['btn-date','btn-edit-day-meals']){await page.locator('#'+id).focus();assert.equal(await page.evaluate(()=>document.activeElement.id),id);}
});
