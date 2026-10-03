const {chromium}=require('/opt/node22/lib/node_modules/playwright');
(async()=>{const b=await chromium.launch({args:['--no-sandbox']});const p=await (await b.newContext({viewport:{width:430,height:900}})).newPage();const errs=[];
p.on('pageerror',e=>errs.push(e.message));p.on('dialog',d=>d.dismiss());
await p.goto('http://127.0.0.1:'+process.argv[2]+'/',{waitUntil:'domcontentloaded'});await p.waitForTimeout(600);
if(await p.$('#ackbox')){await p.check('#ackbox');await p.click('#ackgo')}
await p.evaluate(()=>{try{localStorage.setItem('dock','off')}catch(e){}});
await p.click('.tab[data-tab="guided"]');await p.click('#profcmpgo');await p.waitForSelector('#profcmp table.cmp');
const rows=await p.locator('#profcmp tbody tr').count();const txt=await p.locator('#profcmp').innerText();
await p.screenshot({path:process.argv[3]||'/tmp/w/cmp1.png'});
await p.check('#cmpdiff');const rows2=await p.locator('#profcmp tbody tr').count();
await p.screenshot({path:process.argv[4]||'/tmp/w/cmp2.png'});
await p.click('[data-useprof="risky"]');const gone=await p.locator('#profcmp').count();const on=await p.evaluate(()=>[document.getElementById('ggps').checked,document.getElementById('gdry').checked]);
console.log(rows,rows2,gone,on,/undefined|NaN|\[object/.test(txt),errs);await b.close();
if(errs.length||rows<30||rows2>=rows||gone||!on[0]||!on[1]||/undefined|NaN|\[object/.test(txt))process.exit(1)})()
