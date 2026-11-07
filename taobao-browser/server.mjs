import http from 'node:http';
import crypto from 'node:crypto';
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
function safeTarget(value){let u;try{u=new URL(value)}catch{throw Error('Invalid URL')}if(u.protocol!=='https:'||!['taobao.com','tmall.com'].some(h=>u.hostname===h||u.hostname.endsWith('.'+h)))throw Error('Only Taobao and Tmall links are supported');return u.href}
function validNumber(n,min,max){return Number.isFinite(Number(n))&&Number(n)>=min&&Number(n)<=max}
async function translation(text,from,to){
  const google=new URL('https://translate.googleapis.com/translate_a/single');google.search=new URLSearchParams({client:'gtx',sl:from,tl:to,dt:'t',q:text}).toString();
  try {const response=await fetch(google,{signal:AbortSignal.timeout(8000)});if(!response.ok)throw Error('Google translation HTTP '+response.status);const data=await response.json();const result=data?.[0]?.map(part=>part?.[0]||'').join('');if(result)return result;throw Error('Empty translation')}
  catch (primaryError){const url=new URL('https://api.mymemory.translated.net/get');url.searchParams.set('q',text);url.searchParams.set('langpair',from+'|'+to);try{const response=await fetch(url,{signal:AbortSignal.timeout(8000)});if(!response.ok)throw Error('MyMemory HTTP '+response.status);const data=await response.json();if(Number(data.responseStatus)!==200||!data.responseData?.translatedText)throw Error('Translation quota or service error');return data.responseData.translatedText.replace(/&#(\d+);/g,(_,n)=>Number(n)<=0x10ffff?String.fromCodePoint(Number(n)):'').replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&')}catch(fallbackError){console.error('Translation failed:',primaryError.message,fallbackError.message);throw Error('Translation service unavailable')}}
}
const scan = () => {const chinese=/[\u3400-\u9fff]/;const visible=[],other=[],seen=new Set();const root=document.body;if(!root)return [];const walk=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);while(walk.nextNode()&&visible.length+other.length<250){const node=walk.currentNode,p=node.parentElement;if(!p||p.closest('script,style,noscript,textarea,input,select,option,[contenteditable],svg'))continue;const text=node.textContent.trim();if(!text||text.length>180||!chinese.test(text)||seen.has(text))continue;const style=getComputedStyle(p);if(style.display==='none'||style.visibility==='hidden')continue;seen.add(text);const rect=p.getBoundingClientRect();(rect.bottom>0&&rect.top<innerHeight?visible:other).push(text)}return visible.concat(other)};
const apply = map => {const walk=document.createTreeWalker(document.body||document.documentElement,NodeFilter.SHOW_TEXT);let changed=0;while(walk.nextNode()){const node=walk.currentNode,p=node.parentElement;if(!p||p.closest('script,style,noscript,textarea,input,select,option,[contenteditable],svg'))continue;const old=node.textContent.trim(),translated=map[old];if(translated){node.textContent=node.textContent.replace(old,translated);changed++}}return changed};
async function pending(s){const candidates=await s.page.evaluate(scan);return candidates.filter(t=>!s.cache.has(t)).slice(0,6)}
async function closeSession(id){const s=sessions.get(id);if(!s)return;s.closed=true;sessions.delete(id);clearInterval(s.timer);try{await s.context.close()}catch{}}
setInterval(()=>{for(const [id,s] of sessions)if(Date.now()-s.lastActive>IDLE_MS)closeSession(id)},30_000).unref();
const server=http.createServer(async(req,res)=>{try{const u=new URL(req.url,'http://localhost');if(u.pathname==='/health'){json(res,200,{ok:true,activeSessions:sessions.size});return}
const sent=req.headers['x-proxy-token'];const a=Buffer.from(String(sent||'')),b=Buffer.from(secret);if(a.length!==b.length||!crypto.timingSafeEqual(a,b)){error(res,401,'Unauthorized');return}
if(u.pathname==='/start'&&req.method==='POST'){
  const data=await body(req);const width=validNumber(data.width,320,1600)?Math.floor(data.width):390;const height=validNumber(data.height,480,1200)?Math.floor(data.height):760;const language=data.language==='en'?'en':'nl';if(sessions.size>=1){error(res,429,'Er is al een browsersessie actief. Probeer het later opnieuw.');return}
  const b=await browser();const context=await b.newContext({viewport:{width,height},deviceScaleFactor:1,isMobile:width<700,hasTouch:width<700,locale:'zh-CN',acceptDownloads:false});const page=await context.newPage();const cdp=await context.newCDPSession(page);const id=crypto.randomUUID();const s={id,context,page,cdp,language,cache:new Map(),translationError:'',lastActive:Date.now(),closed:false,translating:false,timer:null};sessions.set(id,s);
  try{await page.goto('https://www.taobao.com/',{waitUntil:'domcontentloaded',timeout:25000})}catch(e){if(!page.url().startsWith('https://')){await closeSession(id);throw Error('Taobao kon niet worden geopend')}}
  json(res,200,{id,url:page.url(),width,height});return}
const id=u.searchParams.get('id')||'';if(!id){error(res,400,'Session ID missing');return}const s=session(id);
if(u.pathname==='/pending'&&req.method==='GET'){json(res,200,{texts:await pending(s),language:s.language});return}
if(u.pathname==='/translations'&&req.method==='POST'){
  const data=await body(req);if(!Array.isArray(data.items)||data.items.length>6)throw Error('Invalid translations');
  const allowed=new Set(await pending(s));const replacements={};
  for(const item of data.items){if(!item||typeof item.source!=='string'||typeof item.target!=='string'||item.target.length>400||!allowed.has(item.source))throw Error('Invalid translation item');s.cache.set(item.source,item.target);replacements[item.source]=item.target}
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
    case 'navigate':await s.page.goto(safeTarget(data.url),{waitUntil:'domcontentloaded',timeout:25000});break;
    case 'search': {if(typeof data.query!=='string'||data.query.trim().length<1||data.query.length>150||!/[\u3400-\u9fff]/.test(data.query))throw Error('Zoekopdracht is nog niet naar Chinees vertaald');await s.page.goto('https://s.taobao.com/search?q='+encodeURIComponent(data.query.trim()),{waitUntil:'domcontentloaded',timeout:25000});break}
    case 'language':if(!['nl','en'].includes(data.language))throw Error('Invalid language');s.language=data.language;s.cache.clear();await s.page.reload({waitUntil:'domcontentloaded',timeout:12000}).catch(()=>{});break;
    default:throw Error('Invalid action')}
  json(res,200,{ok:true,url:s.page.url()});return}
if(u.pathname==='/stop'&&req.method==='POST'){await closeSession(id);json(res,200,{ok:true});return}error(res,404,'Not found');
}catch(e){const msg=String(e.message||e);const status=/Session expired/.test(msg)?410:/Invalid|Only Taobao|Request too large/.test(msg)?400:503;error(res,status,msg.slice(0,180))}});
server.listen(port,'0.0.0.0',()=>console.log('Browser service listening on '+port));
process.on('SIGTERM',async()=>{server.close();await Promise.all([...sessions.keys()].map(closeSession));await chromiumInstance?.close().catch(()=>{});process.exit(0)});
