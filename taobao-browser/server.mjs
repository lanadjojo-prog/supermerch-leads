import http from 'node:http';
import crypto from 'node:crypto';
import dns from 'node:dns/promises';
import net from 'node:net';
import { chromium } from 'playwright';

const port = Number(process.env.PORT || 10000);
const secret = process.env.BROWSER_PROXY_SECRET;
if (!secret || secret.length < 24) throw Error('BROWSER_PROXY_SECRET must be set');
const sessions = new Map();
const IDLE_MS = 12 * 60_000;
let chromiumInstance;
let launching;
async function browser() {
  if (chromiumInstance?.isConnected()) return chromiumInstance;
  if (!launching) launching = chromium.launch({headless:true,args:['--no-sandbox','--disable-dev-shm-usage','--disable-gpu','--no-zygote']}).then(b=>{chromiumInstance=b;return b}).finally(()=>{launching=null});
  return launching;
}
function json(res, status, value) {const data=JSON.stringify(value);res.writeHead(status,{'content-type':'application/json; charset=utf-8','cache-control':'no-store','x-content-type-options':'nosniff','content-length':Buffer.byteLength(data)});res.end(data)}
function error(res,status,message){json(res,status,{error:message})}
async function body(req){let bytes=0,chunks=[];for await (const chunk of req){bytes+=chunk.length;if(bytes>8192)throw Error('Request too large');chunks.push(chunk)}return JSON.parse(Buffer.concat(chunks).toString('utf8')||'{}')}
function session(id){const s=sessions.get(id);if(!s)throw Error('Session expired');s.lastActive=Date.now();return s}
function blockedAddress(value){const ip=value.toLowerCase().replace(/^\[|\]$/g,'');if(net.isIP(ip)===4){const p=ip.split('.').map(Number);return p[0]===0||p[0]===10||p[0]===127||p[0]>=224||p[0]===169&&p[1]===254||p[0]===172&&p[1]>=16&&p[1]<=31||p[0]===192&&p[1]===168||p[0]===100&&p[1]>=64&&p[1]<=127||p[0]===198&&[18,19].includes(p[1])}if(net.isIP(ip)===6)return ip==='::'||ip==='::1'||ip.startsWith('fc')||ip.startsWith('fd')||/^fe[89ab]/.test(ip)||ip.startsWith('::ffff:');return false}
const dnsCache=new Map();
async function publicUrl(value){let u;try{u=new URL(value)}catch{throw Error('Ongeldig webadres')}if(!['http:','https:'].includes(u.protocol)||u.username||u.password||u.port&&!['80','443'].includes(u.port))throw Error('Alleen openbare webadressen zijn toegestaan');const host=u.hostname.toLowerCase().replace(/\.$/,'');if(!host||host==='localhost'||host.endsWith('.localhost')||host.endsWith('.local')||host.endsWith('.internal')||host.endsWith('.test')||blockedAddress(host))throw Error('Privéadressen zijn niet toegestaan');let addresses=dnsCache.get(host);if(!addresses||Date.now()-addresses.time>60000){const found=net.isIP(host)?[{address:host}]:await dns.lookup(host,{all:true,verbatim:true});addresses={time:Date.now(),list:found.map(a=>a.address)};dnsCache.set(host,addresses)}if(!addresses.list.length||addresses.list.some(blockedAddress))throw Error('Privéadressen zijn niet toegestaan');return u.href}
function safeTarget(value){const text=String(value||'').trim();if(!text||text.length>1800)throw Error('Ongeldig webadres');return /^(https?:)?\/\//i.test(text)?text.startsWith('//')?'https:'+text:text:'https://'+text}
function validNumber(n,min,max){return Number.isFinite(Number(n))&&Number(n)>=min&&Number(n)<=max}
const scan = () => {const target=window.__translationTarget||'nl';const lang=(document.documentElement.lang||'').toLowerCase().split('-')[0];const detect=text=>{if(/[\u3040-\u30ff]/.test(text))return 'ja';if(/[\uac00-\ud7af]/.test(text))return 'ko';if(/[\u3400-\u9fff]/.test(text))return 'zh-CN';if(/[\u0400-\u04ff]/.test(text))return 'ru';if(/[\u0600-\u06ff]/.test(text))return 'ar';if(/[\u0900-\u097f]/.test(text))return 'hi';if(/[\u0370-\u03ff]/.test(text))return 'el';if(lang&&lang!=='und'&&lang!=='auto')return lang;const words=' '+text.toLowerCase()+' ';if(/\b(the|and|with|from|your|this|example)\b/.test(words))return 'en';if(/\b(le|les|des|avec|pour|vous)\b/.test(words))return 'fr';if(/\b(der|die|das|und|mit|für)\b/.test(words))return 'de';if(/\b(el|los|las|para|con|una)\b/.test(words))return 'es';return ''};const visible=[],other=[],seen=new Set();const add=(text,element)=>{text=text.trim();if(!text||new TextEncoder().encode(text).length>450||!/[\p{L}]/u.test(text)||seen.has(text))return;const from=detect(text);if(!from||from===target)return;seen.add(text);const rect=element.getBoundingClientRect();(rect.bottom>0&&rect.top<innerHeight?visible:other).push({source:text,from})};const root=document.body;if(!root)return [];const walk=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);while(walk.nextNode()&&visible.length+other.length<300){const node=walk.currentNode,p=node.parentElement;if(!p||p.closest('script,style,noscript,textarea,input,select,option,[contenteditable],svg'))continue;const style=getComputedStyle(p);if(style.display==='none'||style.visibility==='hidden')continue;add(node.textContent,p)}for(const el of root.querySelectorAll('input[placeholder],textarea[placeholder]')){if(visible.length+other.length>=300)break;add(el.getAttribute('placeholder')||'',el)}return visible.concat(other)};
const apply = map => {const root=document.body||document.documentElement;const walk=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);let changed=0;while(walk.nextNode()){const node=walk.currentNode,p=node.parentElement;if(!p||p.closest('script,style,noscript,textarea,input,select,option,[contenteditable],svg'))continue;const old=node.textContent.trim(),translated=map[old];if(translated){node.textContent=node.textContent.replace(old,translated);changed++}}for(const el of root.querySelectorAll('input[placeholder],textarea[placeholder]')){const old=el.getAttribute('placeholder');if(map[old]){el.setAttribute('placeholder',map[old]);changed++}}return changed};
async function pending(s){const candidates=await s.page.evaluate(scan);return candidates.filter(t=>!s.cache.has(t.from+'|'+t.source)).slice(0,6)}
async function closeSession(id){const s=sessions.get(id);if(!s)return;s.closed=true;sessions.delete(id);clearInterval(s.timer);try{await s.context.close()}catch{}}
setInterval(()=>{for(const [id,s] of sessions)if(Date.now()-s.lastActive>IDLE_MS)closeSession(id)},30_000).unref();
const server=http.createServer(async(req,res)=>{try{const u=new URL(req.url,'http://localhost');if(u.pathname==='/health'){json(res,200,{ok:true,activeSessions:sessions.size});return}
const sent=req.headers['x-proxy-token'];const a=Buffer.from(String(sent||'')),b=Buffer.from(secret);if(a.length!==b.length||!crypto.timingSafeEqual(a,b)){error(res,401,'Unauthorized');return}
if(u.pathname==='/start'&&req.method==='POST'){
  const data=await body(req);const width=validNumber(data.width,320,1600)?Math.floor(data.width):390;const height=validNumber(data.height,480,1200)?Math.floor(data.height):760;const language=data.language==='en'?'en':'nl';if(sessions.size>=1){error(res,429,'Er is al een browsersessie actief. Probeer het later opnieuw.');return}
  const b=await browser();const context=await b.newContext({viewport:{width,height},deviceScaleFactor:1,isMobile:width<700,hasTouch:width<700,locale:'en-US',acceptDownloads:false});
  await context.route('**/*',async route=>{try{const url=route.request().url();if(/^(data:|blob:|about:)/.test(url)){await route.continue();return}await publicUrl(url);await route.continue()}catch{await route.abort('blockedbyclient').catch(()=>{})}});
  const page=await context.newPage();const cdp=await context.newCDPSession(page);const id=crypto.randomUUID();const s={id,context,page,cdp,language,cache:new Map(),translationError:'',lastActive:Date.now(),closed:false,timer:null};sessions.set(id,s);
  context.on('page',async popup=>{if(popup===s.page||s.closed)return;try{s.page=popup;s.cdp=await context.newCDPSession(popup);s.cache.clear();await popup.evaluate(lang=>{window.__translationTarget=lang},s.language)}catch{}});
  try{await page.goto('https://example.com/',{waitUntil:'domcontentloaded',timeout:25000})}catch(e){await closeSession(id);throw Error('Startpagina kon niet worden geopend')}
  await page.evaluate(lang=>{window.__translationTarget=lang},language).catch(()=>{});
  json(res,200,{id,url:page.url(),width,height});return}
const id=u.searchParams.get('id')||'';if(!id){error(res,400,'Session ID missing');return}const s=session(id);
if(u.pathname==='/pending'&&req.method==='GET'){json(res,200,{items:await pending(s),language:s.language,pageLanguage:await s.page.evaluate(()=>document.documentElement.lang||'').catch(()=>'')});return}
if(u.pathname==='/translations'&&req.method==='POST'){
  const data=await body(req);if(!Array.isArray(data.items)||data.items.length>6)throw Error('Invalid translations');
  const allowed=new Set((await pending(s)).map(item=>item.from+'|'+item.source));const replacements={};
  for(const item of data.items){if(!item||typeof item.source!=='string'||typeof item.from!=='string'||typeof item.target!=='string'||item.target.length>500||!allowed.has(item.from+'|'+item.source))throw Error('Invalid translation item');s.cache.set(item.from+'|'+item.source,item.target);replacements[item.source]=item.target}
  await s.page.evaluate(apply,replacements);json(res,200,{ok:true,count:data.items.length});return}
if(u.pathname==='/frame'&&req.method==='GET'){
  const capture=await s.cdp.send('Page.captureScreenshot',{format:'jpeg',quality:55,fromSurface:true,captureBeyondViewport:false});const image=Buffer.from(capture.data,'base64');res.writeHead(200,{'content-type':'image/jpeg','cache-control':'no-store','content-length':image.length,'x-page-url':encodeURIComponent(s.page.url()),'x-translation-warning':encodeURIComponent(s.translationError)});res.end(image);return}
if(u.pathname==='/action'&&req.method==='POST'){
  const data=await body(req);const op=data.op;switch(op){
    case 'click': if(!validNumber(data.x,0,1600)||!validNumber(data.y,0,1200))throw Error('Invalid coordinates');await s.page.mouse.click(Number(data.x),Number(data.y));break;
    case 'scroll': if(!validNumber(data.dy,-2500,2500))throw Error('Invalid scroll');await s.page.mouse.wheel(0,Number(data.dy));break;
    case 'text': if(typeof data.text!=='string'||data.text.length>500)throw Error('Invalid text');await s.page.keyboard.insertText(data.text);break;
    case 'key': if(!['Enter','Backspace','Tab','Escape','ArrowDown','ArrowUp'].includes(data.key))throw Error('Invalid key');await s.page.keyboard.press(data.key);break;
    case 'back':await s.page.goBack({waitUntil:'domcontentloaded',timeout:12000}).catch(()=>{});break;
    case 'forward':await s.page.goForward({waitUntil:'domcontentloaded',timeout:12000}).catch(()=>{});break;
    case 'reload':await s.page.reload({waitUntil:'domcontentloaded',timeout:12000}).catch(()=>{});break;
    case 'navigate':await publicUrl(safeTarget(data.url));await s.page.goto(safeTarget(data.url),{waitUntil:'domcontentloaded',timeout:25000});s.cache.clear();break;
    case 'search': {if(typeof data.query!=='string'||data.query.trim().length<1||data.query.length>150)throw Error('Ongeldige zoekopdracht');await s.page.goto('https://www.google.com/search?q='+encodeURIComponent(data.query.trim()),{waitUntil:'domcontentloaded',timeout:25000});s.cache.clear();break}
    case 'siteSearch': {if(typeof data.query!=='string'||!data.query.trim()||data.query.length>150)throw Error('Ongeldige zoekopdracht');const input=s.page.locator('input[type="search"]:visible,input[name="q"]:visible,input[name="search"]:visible,input[placeholder]:visible').first();if(!await input.count())throw Error('Geen zoekveld op deze pagina gevonden');await input.fill(data.query.trim(),{timeout:5000});await input.press('Enter',{timeout:5000});s.cache.clear();break}
    case 'language':if(!['nl','en'].includes(data.language))throw Error('Invalid language');s.language=data.language;s.cache.clear();await s.page.reload({waitUntil:'domcontentloaded',timeout:12000}).catch(()=>{});break;
    default:throw Error('Invalid action')}
  await s.page.evaluate(lang=>{window.__translationTarget=lang},s.language).catch(()=>{});json(res,200,{ok:true,url:s.page.url()});return}
if(u.pathname==='/stop'&&req.method==='POST'){await closeSession(id);json(res,200,{ok:true});return}error(res,404,'Not found');
}catch(e){const msg=String(e.message||e);const status=/Session expired/.test(msg)?410:/Ongeldig|Privé|Alleen|Invalid|Request too large/.test(msg)?400:503;error(res,status,msg.slice(0,180))}});
server.listen(port,'0.0.0.0',()=>console.log('Browser service listening on '+port));
process.on('SIGTERM',async()=>{server.close();await Promise.all([...sessions.keys()].map(closeSession));await chromiumInstance?.close().catch(()=>{});process.exit(0)});
