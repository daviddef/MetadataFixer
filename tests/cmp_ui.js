const {chromium}=require('/opt/node22/lib/node_modules/playwright');
(async()=>{const b=await chromium.launch({args:['--no-sandbox']});const p=await (await b.newContext({viewport:{width:430,height:900}})).newPage();const errs=[];
p.on('pageerror',e=>errs.push(e.message));p.on('dialog',d=>d.dismiss());
await p.goto('http://127.0.0.1:'+process.argv[2]+'/',{waitUntil:'domcontentloaded'});await p.waitForTimeout(600);
if(await p.$('#ackbox')){await p.check('#ackbox');await p.click('#ackgo')}
await p.evaluate(()=>{try{localStorage.setItem('dock','off')}catch(e){}});
await p.click('.tab[data-tab="guided"]');await p.click('#profcmpgo');await p.waitForSelector('#profcmp table.cmp');
const rows=await p.locator('#profcmp tbody tr').count();const txt=await p.locator('#profcmp').innerText();
await p.screenshot({path:process.argv[3]||'/tmp/w/cmp1.png'});
await p.locator('#profcmp .info').nth(5).click();
const tip=await p.locator('#tip').innerText();const tipVis=await p.locator('#tip').isVisible();
await p.screenshot({path:'/tmp/w/cmp3.png'});
await p.keyboard.press('Escape');await p.mouse.click(5,5);
await p.locator('#profcmp [data-cgo]:not([data-cgo=""])').first().click();await p.waitForTimeout(400);
const wentTab=await p.evaluate(()=>document.body.dataset.tab);const gone0=await p.locator('#profcmp').count();
await p.click('#profcmpgo');await p.waitForSelector('#profcmp table.cmp');
console.log('tip',tipVis,JSON.stringify(tip.slice(0,60)),wentTab,gone0);if(!tipVis||tip.length<20||gone0)process.exit(1);
await p.check('#cmpdiff');const rows2=await p.locator('#profcmp tbody tr').count();
await p.screenshot({path:process.argv[4]||'/tmp/w/cmp2.png'});
await p.click('[data-useprof="risky"]');const gone=await p.locator('#profcmp').count();const on=await p.evaluate(()=>[document.getElementById('ggps').checked,document.getElementById('gdry').checked]);
console.log(rows,rows2,gone,on,/undefined|NaN|\[object/.test(txt),errs);await b.close();
if(errs.length||rows<30||rows2>=rows||gone||!on[0]||!on[1]||/undefined|NaN|\[object/.test(txt))process.exit(1)})()
