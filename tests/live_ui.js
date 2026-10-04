const {chromium}=require('/opt/node22/lib/node_modules/playwright');
(async()=>{const b=await chromium.launch({args:['--no-sandbox']});const p=await (await b.newContext({viewport:{width:430,height:900}})).newPage();await p.addInitScript(()=>{try{localStorage.setItem("mode","full")}catch(e){}});const errs=[];
p.on('pageerror',e=>errs.push(e.message));p.on('dialog',d=>d.dismiss());
await p.goto('http://127.0.0.1:'+process.argv[2]+'/',{waitUntil:'domcontentloaded'});await p.waitForTimeout(600);
if(await p.$('#ackbox')){await p.check('#ackbox');await p.click('#ackgo')}
await p.evaluate(()=>{try{localStorage.setItem('dock','off')}catch(e){}});
await p.click('.tab[data-tab="monitor"]');await p.click('#livego');await p.waitForSelector('#livelist .rec',{timeout:8000});
const t=await p.locator('#livelist').innerText();await p.screenshot({path:process.argv[3]||'/tmp/w/live.png'});
await p.click('#livego');const st=await p.locator('#livestat').innerText();
console.log(JSON.stringify(t.slice(0,120)),st,errs);await b.close();if(errs.length||!/quota|Quota|iCloud/.test(t))process.exit(1)})()
