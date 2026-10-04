const {chromium}=require('/opt/node22/lib/node_modules/playwright');
(async()=>{const b=await chromium.launch({args:['--no-sandbox']});const ctx=await b.newContext({viewport:{width:430,height:900},deviceScaleFactor:1.5});const p=await ctx.newPage();const errs=[];
p.on('pageerror',e=>errs.push(e.message));p.on('dialog',d=>d.dismiss());
await p.goto('http://127.0.0.1:'+process.argv[2]+'/',{waitUntil:'domcontentloaded'});await p.waitForTimeout(700);
if(await p.$('#ackbox')){await p.check('#ackbox');await p.click('#ackgo')}
await p.evaluate(()=>{try{localStorage.setItem('dock','off')}catch(e){}});
await p.reload({waitUntil:'domcontentloaded'});await p.waitForTimeout(700);
if(await p.$('#ackbox')&&await p.isVisible('#ackbox')){await p.check('#ackbox');await p.click('#ackgo')}
const out={};
out.tab=await p.evaluate(()=>document.body.dataset.tab);out.mode=await p.evaluate(()=>document.body.dataset.mode);
out.goals=await p.locator('.goal').count();out.sugg=(await p.locator('#suggest').innerText()).slice(0,60);
out.navHidden=!(await p.isVisible('nav.tabs'));
await p.waitForTimeout(1500);out.pillOnHome=await p.isVisible('#backhome');await p.screenshot({path:'/tmp/shots4/home.png',fullPage:true});
await p.click('.goal.gl1');out.afterGoal=await p.evaluate(()=>document.body.dataset.tab);out.backVisible=await p.isVisible('#backhome');
out.startLbl=await p.locator('#gst').innerText();
await p.waitForTimeout(1200);await p.screenshot({path:'/tmp/shots4/guided.png',fullPage:true});
await p.click('#backhome');out.home2=await p.evaluate(()=>document.body.dataset.tab);
await p.click('.goal.gl4');out.sheet=await p.isVisible('#probsheet');await p.screenshot({path:'/tmp/shots4/problems.png'});
await p.waitForTimeout(500);await p.screenshot({path:'/tmp/shots4/problems.png'});await p.click('[data-prob="sync-stuck"]');await p.waitForTimeout(600);out.guideTab=await p.evaluate(()=>document.body.dataset.tab);out.guideOpen=await p.locator('[data-pb="sync-stuck"][open]').count();
await p.screenshot({path:'/tmp/shots4/guide.png'});
await p.click('#backhome');await p.click('.goal.gl5');await p.waitForTimeout(900);await p.screenshot({path:'/tmp/shots4/monitor-simple.png',fullPage:true});await p.click('#backhome');await p.click('#modebtn');out.full=await p.evaluate(()=>[document.body.dataset.mode,!!document.querySelector('nav.tabs').offsetParent]);
await p.click('.tab[data-tab="monitor"]');await p.screenshot({path:'/tmp/shots4/monitor-full.png'});
await p.evaluate(()=>setMode('simple'));
await p.setViewportSize({width:430,height:900});
console.log(JSON.stringify(out),errs);await b.close();
if(errs.length||out.goals!==8||out.tab!=='home'||out.afterGoal!=='guided'||!out.sheet||!out.guideOpen||!out.navHidden||!out.backVisible||out.pillOnHome)process.exit(1)})()
