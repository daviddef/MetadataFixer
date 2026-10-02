const {chromium}=require('/opt/node22/lib/node_modules/playwright');
(async()=>{
 const b=await chromium.launch({args:['--no-sandbox']});
 const pg=await b.newPage();const errs=[];
 pg.on('pageerror',e=>errs.push('PAGEERR '+e.message));
 pg.on('console',m=>{if(m.type()==='error'&&!/ERR_|Failed to load/.test(m.text()))errs.push('CONSOLE '+m.text())});
 pg.on('dialog',d=>d.dismiss());
 await pg.goto('http://127.0.0.1:'+process.argv[2]+'/',{waitUntil:'domcontentloaded'});
 await pg.waitForTimeout(600);
 const names=await pg.evaluate(()=>{const o=[];document.querySelectorAll('[id]').forEach(e=>{if(e.matches('button')&&/^tab|tab/i.test(e.id+e.className))o.push(e.id)});return o});
 console.log('candidates',JSON.stringify(names));
 const tabs=await pg.$$('.tab, [role=tab], nav button');
 console.log('tabs',tabs.length);
 for(const t of tabs){try{await t.click({timeout:2000,force:true});await pg.waitForTimeout(150)}catch(e){errs.push('click '+e.message.slice(0,80))}}
 const dup=await pg.evaluate(()=>{const c={};document.querySelectorAll('[id]').forEach(e=>c[e.id]=(c[e.id]||0)+1);return Object.keys(c).filter(k=>c[k]>1)});
 console.log('dupids',JSON.stringify(dup));
 console.log('errors',JSON.stringify(errs,null,1));
 await b.close();
})().catch(e=>console.log('ERR',e.message));
