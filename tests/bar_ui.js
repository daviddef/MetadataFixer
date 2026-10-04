const {chromium}=require('/opt/node22/lib/node_modules/playwright');
(async()=>{const b=await chromium.launch({args:['--no-sandbox']});const p=await (await b.newContext({viewport:{width:430,height:900},deviceScaleFactor:1.5})).newPage();const errs=[];
p.on('pageerror',e=>errs.push(e.message));p.on('dialog',d=>d.dismiss());
await p.goto('http://127.0.0.1:'+process.argv[2]+'/',{waitUntil:'domcontentloaded'});await p.waitForTimeout(600);
if(await p.$('#ackbox')){await p.check('#ackbox');await p.click('#ackgo')}
await p.evaluate(()=>{localStorage.setItem('dock','off');localStorage.removeItem('folders');localStorage.removeItem('dest')});
await p.reload({waitUntil:'domcontentloaded'});await p.waitForTimeout(600);
if(await p.isVisible('#ackbox')){await p.check('#ackbox');await p.click('#ackgo')}
await p.click('.goal.gl1');await p.waitForTimeout(900);
const out={};const h=async()=>Math.round((await p.locator('header.hero').boundingBox()).height);
out.emptyText=[await p.locator('#fsum').innerText(),await p.locator('#fdname').innerText()];out.hEmpty=await h();
await p.screenshot({path:'/tmp/shots5/bar-empty.png',clip:{x:0,y:0,width:430,height:420}});
// start with nothing -> friendly nudge, no alert
await p.click('#gchk');await p.waitForTimeout(400);out.nudge=await p.locator('#nudge.on').count();
await p.evaluate(()=>{FOLDERS=['/Users/me/Takeout/takeout-001.zip','/Users/me/Takeout/takeout-002.zip','/Users/me/Pictures/Old'];saveFolders();$('fdest').value='/Users/me/Pictures/Fixed Photos';saveDest()});
await p.waitForTimeout(600);
out.setText=[await p.locator('#fsum').innerText(),await p.locator('#fdname').innerText()];out.hSet=await h();out.stat=await p.locator('#rtstat').innerText();
await p.screenshot({path:'/tmp/shots5/bar-set.png',clip:{x:0,y:0,width:430,height:420}});
await p.click('#fded');out.editing=await p.isVisible('#fdest');
console.log(JSON.stringify(out),errs);await b.close();
if(errs.length||out.emptyText[0]!=='Add your photos'||out.setText[0]!=='2 zips'&&!/zip/.test(out.setText[0])||!out.nudge||!out.editing)process.exit(1)})()
