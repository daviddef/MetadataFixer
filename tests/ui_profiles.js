const {chromium}=require('/opt/node22/lib/node_modules/playwright');
(async()=>{const b=await chromium.launch({args:['--no-sandbox']});const pg=await (await b.newContext({viewport:{width:900,height:1000}})).newPage();await pg.addInitScript(()=>{try{localStorage.setItem("mode","full")}catch(e){}});const errs=[];
pg.on('pageerror',e=>errs.push(e.message));pg.on('dialog',d=>d.dismiss());
await pg.goto('http://127.0.0.1:'+process.argv[2]+'/',{waitUntil:'domcontentloaded'});await pg.waitForTimeout(600);
if(await pg.$('#ackbox')){await pg.check('#ackbox');await pg.click('#ackgo')}
await pg.click('.tab[data-tab="guided"]');await pg.waitForTimeout(300);
const val=id=>pg.evaluate(i=>{const e=document.getElementById(i);return e.type==='checkbox'?e.checked:e.value},id);
const bad=[];const chk=async(n,id,exp)=>{const v=await val(id);if(v!==exp)bad.push(n+': '+id+'='+v+' expected '+exp)};
await pg.click('[data-prof=risky]');
for(const [id,e] of [['gfx',true],['ggps',true],['gnear',true],['gcv',true],['gdry',true],['fdfix',true],['fgps',true],['near',true],['dry',true],['simsens','10']])await chk('risky',id,e);
await pg.click('[data-prof=safest]');
for(const [id,e] of [['gfx',false],['ggps',false],['gow',false],['gnear',false],['gcv',false],['gdry',true],['fdfix',false],['ow',false],['simsens','3']])await chk('safest',id,e);
await pg.click('[data-prof=fastest]');
for(const [id,e] of [['gdedupe',false],['glive',false],['gfd',false],['galb',false],['gdry',true],['dedupe',false],['ppace','space']])await chk('fastest',id,e);
await pg.click('[data-prof=balanced]');
for(const [id,e] of [['gdedupe',true],['gow',true],['gfx',false],['ggps',false],['dry',true],['simsens','6'],['ppace','verify']])await chk('balanced',id,e);
// customise
await pg.evaluate(()=>document.querySelectorAll('details.more').forEach(d=>d.open=true));await pg.click('#gnear');const cust=await pg.evaluate(()=>document.getElementById('profinfo').innerText.includes('Customised'));if(!cust)bad.push('customised label missing');
// bursts follow the style
await pg.click('[data-prof=risky]');const bu=await pg.evaluate(()=>DP.bursts);if(bu!=='best')bad.push('risky bursts='+bu);
await pg.click('[data-prof=balanced]');
const reload=await pg.evaluate(()=>localStorage.getItem('profile'));if(reload!=='balanced')bad.push('profile not saved');
await pg.evaluate(()=>localStorage.setItem('dupeprefs',JSON.stringify({order:['favorite','oldest']})));
await pg.reload({waitUntil:'domcontentloaded'});await pg.waitForTimeout(600);
const n=await pg.evaluate(()=>document.querySelectorAll('.dprow').length);if(n<10)bad.push('dupeprefs not normalised: '+n+' rows');
console.log(JSON.stringify({bad,errs}));await b.close()})();
