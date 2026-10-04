// Plain-language lint: visible titles must be short, free of jargon and easy to read.
const {chromium}=require('/opt/node22/lib/node_modules/playwright');
const JARGON=/\b(metadata|exif|exiftool|timestamp|dedup\w*|perceptual|reconstruct\w*|heuristic|daemon|sidecar|uuid|re-?encod\w*|orphan\w*|derivative|livelock|mingle|cloudkit|sqlite|xattr|checksum|hash\w*|iterat\w*|granular\w*|repository|schema)\b/i;
(async()=>{const b=await chromium.launch({args:['--no-sandbox']});const p=await (await b.newContext({viewport:{width:430,height:900}})).newPage();
await p.goto('http://127.0.0.1:'+process.argv[2]+'/',{waitUntil:'domcontentloaded'});await p.waitForTimeout(700);
if(await p.$('#ackbox')){await p.check('#ackbox');await p.click('#ackgo')}await p.waitForTimeout(500);
const items=await p.evaluate(()=>{const o=[];const add=(k,t)=>{t=(t||'').replace(/\s+/g,' ').trim();if(t)o.push([k,t])};
 document.querySelectorAll('.tab').forEach(e=>add('tab',e.innerText));
 document.querySelectorAll('h2.ph').forEach(e=>add('title',e.childNodes[0]?e.childNodes[0].textContent:e.innerText));
 document.querySelectorAll('.goal b,.goal span,.prob span,.prob b').forEach(e=>add('home',e.childNodes[0]?e.childNodes[0].textContent:e.innerText));
 document.querySelectorAll('.opt').forEach(e=>{const t=e.querySelector('.ttl,label:not(.sub)');add('option',t?t.textContent:'')});
 document.querySelectorAll('.pane > .card > button.p, .pane button.p').forEach(e=>add('button',e.innerText));return o});
const syl=w=>{w=w.toLowerCase().replace(/[^a-z]/g,'');if(w.length<=3)return 1;w=w.replace(/(?:[^laeiouy]es|ed|[^laeiouy]e)$/,'').replace(/^y/,'');const m=w.match(/[aeiouy]{1,2}/g);return m?m.length:1};
const bad=[];let words=0,sy=0,sent=0;
for(const [k,t0] of items){const t=t0.replace(/[⚠️]/g,'').trim();const ws=t.split(/\s+/).filter(x=>/[a-z]/i.test(x));
 if(JARGON.test(t))bad.push([k,t,'jargon']);
 if(k==='option'&&ws.length>9)bad.push([k,t,'too long ('+ws.length+' words)']);
 ws.forEach(w=>{const c=w.replace(/[^a-z]/gi,'');if(c.length>=13&&!/^(iCloud|Photos|Takeout)/i.test(c))bad.push([k,t,'long word: '+c]);words++;sy+=syl(w)});sent++}
const fk=0.39*(words/sent)+11.8*(sy/words)-15.59;
console.log('checked',items.length,'labels; avg grade',fk.toFixed(1));bad.forEach(x=>console.log('  ',x.join(' | ')));
await b.close();process.exit(bad.length||fk>6.5?1:0)})()
