const {chromium}=require('/opt/node22/lib/node_modules/playwright');
(async()=>{const b=await chromium.launch({args:['--no-sandbox']});const p=await (await b.newContext({viewport:{width:430,height:900}})).newPage();const errs=[];
p.on('pageerror',e=>errs.push(e.message));p.on('dialog',d=>d.dismiss());
await p.goto('http://127.0.0.1:'+process.argv[2]+'/',{waitUntil:'domcontentloaded'});await p.waitForTimeout(600);
if(await p.$('#ackbox')){await p.check('#ackbox');await p.click('#ackgo')}
await p.evaluate(()=>{try{localStorage.setItem('dock','off')}catch(e){}});
await p.click('.tab[data-tab="monitor"]');
await p.click('#syncgo');await p.waitForFunction(()=>/MB\/s|KB\/s/.test(document.getElementById('syncbody').innerText),null,{timeout:12000});
const sync=await p.locator('#syncbody').innerText();await p.screenshot({path:(process.argv[3]||'/tmp/w/sync.png')});await p.click('#syncgo');
await p.click('#livego');await p.waitForSelector('#livelist .rec',{timeout:8000});
const hasfix=await p.locator('#livelist [data-fix]').count();
await p.click('#livego');
// apply a preset directly
await p.evaluate(()=>applyFix('slowsend'));
const vals=await p.evaluate(()=>[$('pbatch').value,$('ppace').value,$('pdry').checked,document.body.dataset.tab]);
const note=await p.locator('#fixnote').innerText();
await p.screenshot({path:(process.argv[4]||'/tmp/w/fix.png')});
await p.click('#fixundo');
console.log(JSON.stringify(sync.slice(0,90)),hasfix,vals,note.length>20,errs);await b.close();
if(errs.length||vals[0]!=='2'||vals[3]!=='photos')process.exit(1)})()
