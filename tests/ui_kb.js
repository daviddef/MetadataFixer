const {chromium}=require('/opt/node22/lib/node_modules/playwright');
(async()=>{const b=await chromium.launch({args:['--no-sandbox']});const pg=await (await b.newContext({viewport:{width:480,height:900}})).newPage();await pg.addInitScript(()=>{try{localStorage.setItem("mode","full")}catch(e){}});const errs=[],bad=[];
pg.on('pageerror',e=>errs.push(e.message));pg.on('dialog',d=>d.dismiss());
await pg.goto('http://127.0.0.1:'+process.argv[2]+'/',{waitUntil:'domcontentloaded'});await pg.waitForTimeout(600);
if(await pg.$('#ackbox')){await pg.check('#ackbox');await pg.click('#ackgo')}
await pg.click('[data-tab=monitor]');await pg.waitForTimeout(400);
await pg.click('#kbdet summary');await pg.waitForTimeout(1200);
const n=await pg.evaluate(()=>document.querySelectorAll('#kblist details.sec').length);if(n<50)bad.push('only '+n+' entries listed');
await pg.fill('#kbq','4097');await pg.waitForTimeout(300);
const t=await pg.evaluate(()=>document.getElementById('kblist').innerText);if(!/interrupted/i.test(t))bad.push('search 4097 found nothing useful');
await pg.selectOption('#kbcat','icloud');await pg.fill('#kbq','');await pg.waitForTimeout(300);
const m=await pg.evaluate(()=>document.querySelectorAll('#kblist details.sec').length);if(m<10)bad.push('icloud filter '+m);
await pg.fill('#kbq','zzzzqq');await pg.waitForTimeout(300);const z=await pg.evaluate(()=>document.getElementById('kblist').innerText);if(!/Nothing matches/.test(z))bad.push('empty state');
console.log(JSON.stringify({bad,errs}));await b.close()})();
