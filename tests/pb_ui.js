const {chromium}=require('/opt/node22/lib/node_modules/playwright');
(async()=>{const b=await chromium.launch({args:['--no-sandbox']});const p=await (await b.newContext({viewport:{width:430,height:900}})).newPage();await p.addInitScript(()=>{try{localStorage.setItem("mode","full")}catch(e){}});const errs=[];
p.on('pageerror',e=>errs.push(e.message));p.on('dialog',d=>d.dismiss());
await p.goto('http://127.0.0.1:'+process.argv[2]+'/',{waitUntil:'domcontentloaded'});await p.waitForTimeout(600);
if(await p.$('#ackbox')){await p.check('#ackbox');await p.click('#ackgo')}
await p.evaluate(()=>{try{localStorage.setItem('dock','off')}catch(e){}});
await p.click('.tab[data-tab="monitor"]');await p.click('#pbdet summary');await p.waitForSelector('#pblist details.sec');
const n=await p.locator('#pblist details.sec').count();
await p.click('#pblist details.sec >> nth=0 >> summary');
await p.locator('#pblist input[type=checkbox]').first().check();
const prog=await p.locator('.pbprog').first().innerText();
await p.click('#pbaudit');await p.waitForSelector('#pbres .tip,#pbres .card');
const res=await p.locator('#pbres').innerText();
await p.screenshot({path:process.argv[3]||'/tmp/w/pb.png'});
console.log(n,prog,JSON.stringify(res.slice(0,80)),errs);if(errs.length||n<10)process.exit(1);await b.close()})()
