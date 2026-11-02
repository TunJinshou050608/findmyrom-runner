import * as zlib from 'node:zlib';
import {createCipheriv,createDecipheriv,createHash,timingSafeEqual} from 'node:crypto';
import {Readable} from 'node:stream';
import {connect} from 'cloudflare:sockets';
import {createCodecs} from './codecs.mjs';
import {samsungSource,samsungPlan} from './samsung.mjs';

const CHUNK=65536;
const LIMIT=250*1024*1024;
const SOURCES={
 google:'https://dl.google.com/dl/android/aosp/akita-ud2a.231203.054-factory-b3a902d4.zip',
 vivo:'https://asia-sysupwsdl.vivoglobal.com/upgrade/official/officialFiles/PD2357F_EX_A_14.0.16.1.W30.V000L1-update-full_1739959098.zip',
 oplus:'https://gauss-componentotacostmanual-cn.allawnfs.com/remove-f3beae86ff23858de919c5c568891ca7/component-ota/24/10/09/233fa60caa38462e97b29bfa8bc0c0e2.zip',
};
const u16=(b,p)=>b.readUInt16LE(p),u32=(b,p)=>b.readUInt32LE(p);
function u64(b,p){const n=b.readBigUInt64LE(p);if(n>BigInt(Number.MAX_SAFE_INTEGER))throw Error('Unsafe ZIP offset');return Number(n);}
async function collect(body,max){
 const reader=body.getReader();let n=0;const buffers=[];
 try{while(true){const {value,done}=await reader.read();if(done)break;n+=value.length;if(n>max)throw Error('Body bound exceeded');buffers.push(Buffer.from(value));}}
 finally{await reader.cancel().catch(()=>{});reader.releaseLock();}
 return Buffer.concat(buffers,n);
}
async function googleRange(url,headers,signal,method='GET',data=''){
 const u=new URL(url),socket=connect({hostname:u.hostname,port:u.protocol==='https:'?443:80},{secureTransport:u.protocol==='https:'?'on':'off'});
 const reader=socket.readable.getReader(),writer=socket.writable.getWriter();
 const close=()=>{void socket.close().catch(()=>{});};signal?.addEventListener('abort',close,{once:true});
 try{
  const requestHeaders={...headers,'Accept-Encoding':'identity',Host:u.hostname,Connection:'close',...(method==='POST'?{'Content-Length':String(new TextEncoder().encode(data).length)}:{})};
  await writer.write(new TextEncoder().encode(`${method} ${u.pathname+u.search} HTTP/1.1\r\n`+Object.entries(requestHeaders).map(([k,v])=>k+': '+v+'\r\n').join('')+'\r\n'+data));
  let buf=Buffer.alloc(0),end=-1;
  while((end=buf.indexOf('\r\n\r\n'))<0){const r=await reader.read();if(r.done)throw Error('Truncated HTTP headers');buf=Buffer.concat([buf,Buffer.from(r.value)]);if(buf.length>128*1024)throw Error('HTTP header bound');}
  const lines=buf.toString('latin1',0,end).split('\r\n'),status=Number(lines.shift().split(' ')[1]),responseHeaders=new Headers();
  for(const line of lines){const p=line.indexOf(':');if(p<1)throw Error('Invalid HTTP header');responseHeaders.append(line.slice(0,p),line.slice(p+1).trim());}
  buf=buf.subarray(end+4);
  if(status!==206&&!(method==='POST'&&status===200)){close();return new Response(null,{status,headers:responseHeaders});}
  const length=Number(responseHeaders.get('Content-Length')),
        chunked=(responseHeaders.get('Transfer-Encoding')||'').toLowerCase()==='chunked';
  async function more(){const r=await reader.read();if(r.done)throw Error('Truncated HTTP body');buf=buf.length?Buffer.concat([buf,Buffer.from(r.value)]):Buffer.from(r.value);}
  async function line(){let e;while((e=buf.indexOf('\r\n'))<0){if(buf.length>8192)throw Error('Invalid HTTP chunk header');await more();}const s=buf.toString('ascii',0,e);buf=buf.subarray(e+2);return s;}
  let remaining=length,chunkRemaining=0,done=false;
  const body=new ReadableStream({async pull(c){try{
   if(done){close();c.close();return;}
   if(chunked&&chunkRemaining===0){const h=await line();if(!/^[a-f0-9]+(?:;.*)?$/i.test(h))throw Error('Invalid HTTP chunk');chunkRemaining=parseInt(h,16);if(chunkRemaining===0){done=true;close();c.close();return;}}
   if(!chunked&&(!Number.isSafeInteger(remaining)||remaining<0))throw Error('Unsupported HTTP body framing');
   if(!chunked&&remaining===0){done=true;close();c.close();return;}
   while(!buf.length)await more();const n=Math.min(buf.length,chunked?chunkRemaining:remaining);c.enqueue(buf.subarray(0,n));buf=buf.subarray(n);
   if(chunked){chunkRemaining-=n;if(chunkRemaining===0&&await line()!=='')throw Error('Invalid chunk terminator');}else remaining-=n;
  }catch(e){close();c.error(e);}},cancel(){close();}},{highWaterMark:0});
  responseHeaders.delete('Transfer-Encoding');
  return new Response(body,{status,headers:responseHeaders});
 }catch(e){close();throw e;}
}
class Source{
 constructor(url,signal){this.url=url;this.signal=signal;this.bytes=0;this.requests=0;this.size=null;this.etag=null;}
 async response(start,length,retried=false){
  if(!Number.isSafeInteger(start)||!Number.isSafeInteger(length)||start<0||length<=0||this.bytes+length>LIMIT||++this.requests>48)throw Error('Upstream budget exceeded');
  const headers={...(this.extraHeaders?.()||{}),'Range':`bytes=${start}-${start+length-1}`,'Accept-Encoding':'identity'};
  if(this.etag)headers['If-Match']=this.etag;
  const r=new URL(this.url).hostname==='dl.google.com'
   ?await googleRange(this.url,headers,this.signal)
   :await fetch(this.url,{headers,redirect:'manual',signal:this.signal,cache:'no-store'});
  if(r.status===401&&this.refresh&&!retried){await r.body?.cancel();await this.refresh();return this.response(start,length,true);}
  const m=/^bytes (\d+)-(\d+)\/(\d+)$/.exec(r.headers.get('Content-Range')||'');
  const encoding=(r.headers.get('Content-Encoding')||'identity').toLowerCase();
  const oplus=/\.allawn(?:o)?fs\.com$/.test(new URL(this.url).hostname);
  if(r.status!==206||!m||Number(m[1])!==start||Number(m[2])!==start+length-1||Number(m[3])<=start+length-1||
    (this.size!==null&&this.size!==Number(m[3]))||(encoding!=='identity'&&!(oplus&&encoding==='utf-8'))){await r.body?.cancel();throw Error(`Invalid upstream range HTTP ${r.status} at ${start}+${length}, encoding ${encoding}, content-range ${r.headers.get('Content-Range')}`);}
  const tag=r.headers.get('ETag');if(this.etag&&tag&&this.etag!==tag){await r.body.cancel();throw Error('Source changed');}
  if(tag&&!tag.startsWith('W/'))this.etag=tag;this.size=Number(m[3]);
  return r;
 }
 async *stream(start,length){const r=await this.response(start,length),reader=r.body.getReader();let read=0;
  try{while(true){const {done,value}=await reader.read();if(done)break;read+=value.length;this.bytes+=value.length;if(read>length||this.bytes>LIMIT)throw Error('Upstream overflow');yield Buffer.from(value);}if(read!==length)throw Error('Truncated upstream');}
  finally{await reader.cancel().catch(()=>{});reader.releaseLock();}
 }
 async get(start,length){if(length>32*1024*1024)throw Error('Metadata/input buffer exceeds 32MiB');const result=Buffer.alloc(length);let pos=0;for await(const b of this.stream(start,length)){b.copy(result,pos);pos+=b.length;}return result;}
 async init(){await this.get(0,1);return this;}
}
async function zipDirectory(source,base=0,size=source.size){
 const n=Math.min(size,65557),tail=await source.get(base+size-n,n);let p=tail.length-22;
 for(;p>=0;p--)if(u32(tail,p)===0x06054b50&&p+22+u16(tail,p+20)<=tail.length)break;
 if(p<0)throw Error('ZIP EOCD missing '+JSON.stringify({base,size,tailLength:tail.length,magic:tail.subarray(-32).toString('hex'),signature:tail.lastIndexOf(Buffer.from('504b0506','hex'))}));
 let count=u16(tail,p+10),length=u32(tail,p+12),offset=u32(tail,p+16);
 if(count===65535||length===0xffffffff||offset===0xffffffff){
  if(p<20||u32(tail,p-20)!==0x07064b50)throw Error('Zip64 locator missing');
  const z=await source.get(base+u64(tail,p-12),56);
  if(u32(z,0)!==0x06064b50)throw Error('Zip64 header missing');
  count=u64(z,32);length=u64(z,40);offset=u64(z,48);
 }
 if(length>4*1024*1024||offset+length>size||count>20000)throw Error('ZIP directory bound exceeded');
 const cd=await source.get(base+offset,length),entries=[];let pos=0;
 for(let i=0;i<count;i++){
  if(pos+46>cd.length||u32(cd,pos)!==0x02014b50)throw Error('Invalid ZIP directory');
  const nameLength=u16(cd,pos+28),extraLength=u16(cd,pos+30),comment=u16(cd,pos+32);
  const end=pos+46+nameLength+extraLength+comment;if(end>cd.length)throw Error('Truncated ZIP entry');
  let compressed=u32(cd,pos+20),uncompressed=u32(cd,pos+24),local=u32(cd,pos+42);
  const extra=cd.subarray(pos+46+nameLength,pos+46+nameLength+extraLength);
  for(let e=0;e+4<=extra.length;){const tag=u16(extra,e),len=u16(extra,e+2);e+=4;if(e+len>extra.length)throw Error('Truncated ZIP extra');if(tag===1){let j=e;if(uncompressed===0xffffffff){uncompressed=u64(extra,j);j+=8;}if(compressed===0xffffffff){compressed=u64(extra,j);j+=8;}if(local===0xffffffff)local=u64(extra,j);}e+=len;}
  const flags=u16(cd,pos+8),method=u16(cd,pos+10);if(flags&1)throw Error('Encrypted ZIP member');
  entries.push({name:cd.toString('utf8',pos+46,pos+46+nameLength),compressed,size:uncompressed,offset:base+local,method,crc:u32(cd,pos+16)});pos=end;
 }
 return entries;
}
async function memberBase(source,item){const h=await source.get(item.offset,30);if(u32(h,0)!==0x04034b50)throw Error('Invalid local ZIP header');const base=item.offset+30+u16(h,26)+u16(h,28);if(base+item.compressed>source.size)throw Error('ZIP member outside source');return base;}
const crcTable=Uint32Array.from({length:256},(_,n)=>{for(let k=0;k<8;k++)n=n&1?0xedb88320^(n>>>1):n>>>1;return n>>>0;});
function crc32(data,previous=0){if(typeof zlib.crc32==='function')return zlib.crc32(data,previous);let c=previous^0xffffffff;for(const b of data)c=crcTable[(c^b)&255]^(c>>>8);return (c^0xffffffff)>>>0;}
async function* decode(iter,factory){
 const input=Readable.from(iter,{objectMode:false});const decoder=factory();input.on('error',e=>decoder.destroy(e));input.pipe(decoder);
 try{for await(const chunk of decoder)yield Buffer.from(chunk);}finally{input.destroy();decoder.destroy();}
}
async function googlePlan(source,target){
 const outer=await zipDirectory(source);const nested=outer.filter(i=>/^image-[^/]+\.zip$/.test(i.name.split('/').at(-1)));
 if(nested.length!==1||nested[0].method!==0||nested[0].compressed!==nested[0].size)throw Error('Nested ZIP not seekable');
 const base=await memberBase(source,nested[0]);const inner=await zipDirectory(source,base,nested[0].size);const images=inner.filter(i=>i.name.split('/').at(-1)===target+'.img');
 if(images.length!==1)throw Error('Target absent or duplicated');const item=images[0];
 if(![0,8].includes(item.method)||item.size<65536||item.size>256*1024*1024)throw Error('Unsupported image');
 const offset=await memberBase(source,item);
 return {size:item.size,integrity:'ZIP CRC32',async *chunks(){let crc=0,written=0;const raw=source.stream(offset,item.compressed);const chunks=item.method===8?decode(raw,()=>zlib.createInflateRaw({chunkSize:CHUNK})):raw;
  for await(const b of chunks){written+=b.length;if(written>item.size)throw Error('Image overflow');crc=crc32(b,crc);yield b;}
  if(written!==item.size||crc!==item.crc)throw Error('ZIP image integrity mismatch');
 }};
}
function protobuf(data){let p=0;const fields=new Map();function v(){let n=0,m=1;for(let i=0;i<10;i++){if(p>=data.length)throw Error('Truncated varint');const b=data[p++];n+=(b&127)*m;if(!Number.isSafeInteger(n))throw Error('Unsafe varint');if(!(b&128))return n;m*=128;}throw Error('Invalid varint');}
 while(p<data.length){const tag=v(),key=Math.floor(tag/8),wire=tag&7;let value;if(!key)throw Error('Invalid protobuf field');if(wire===0)value=v();else if([1,2,5].includes(wire)){const n=wire===2?v():wire===1?8:4;if(p+n>data.length)throw Error('Truncated protobuf');value=data.subarray(p,p+n);p+=n;}else throw Error('Unsupported wire');if(!fields.has(key))fields.set(key,[]);fields.get(key).push(value);}return fields;}
const one=(f,k,d)=>f.get(k)?.at(-1)??d;
async function payloadPlan(source,target){
 const entries=await zipDirectory(source),items=entries.filter(i=>i.name==='payload.bin');
 if(items.length!==1||items[0].method!==0||items[0].size!==items[0].compressed)throw Error('Payload not seekable');
 const base=await memberBase(source,items[0]),header=await source.get(base,24);
 if(header.toString('ascii',0,4)!=='CrAU'||header.readBigUInt64BE(4)!==2n)throw Error('Unsupported payload');
 const ms=Number(header.readBigUInt64BE(12)),ss=header.readUInt32BE(20);if(ms>4*1024*1024||24+ms+ss>items[0].size)throw Error('Manifest bound exceeded');
 const manifest=protobuf(await source.get(base+24,ms)),block=one(manifest,3,4096);
 if(block!==4096)throw Error('Invalid block size');const parts=(manifest.get(13)||[]).map(protobuf).filter(p=>one(p,1,Buffer.alloc(0)).toString()===target);
 if(parts.length!==1)throw Error('Partition absent or duplicated');const part=parts[0],info=protobuf(one(part,7,Buffer.alloc(0))),size=one(info,1,0),hash=one(info,2);
 if(size<65536||size>256*1024*1024||hash?.length!==32)throw Error('Invalid partition metadata');
 const ops=(part.get(8)||[]).map(raw=>{const op=protobuf(raw),kind=one(op,1),ranges=(op.get(6)||[]).map(e=>{const f=protobuf(e);return {start:one(f,1,-1)*block,length:one(f,2,0)*block};});if(!ranges.length)throw Error('Empty extents');let end=ranges[0].start;
  for(const r of ranges){if(r.start!==end||r.start<0||r.length<=0||r.start+r.length>size)throw Error('Noncontiguous/invalid extents');end+=r.length;}
  const offset=base+24+ms+ss+one(op,2,0),length=one(op,3,0);if(offset<base||offset+length>base+items[0].size||length>32*1024*1024)throw Error('Operation bound exceeded');
  return {kind,start:ranges[0].start,size:end-ranges[0].start,offset,length,hash:one(op,8)};
 }).sort((a,b)=>a.start-b.start);
 let cursor=0;for(const op of ops){if(op.start!==cursor)throw Error('Overlapping/incomplete partition');cursor+=op.size;}if(cursor!==size)throw Error('Incomplete partition');
 const types=[...new Set(ops.map(o=>o.kind))];
 if(types.some(t=>![0,1,6,8,14].includes(t)))throw Error('Source image required: '+types.join(','));
 if(types.includes(14)&&typeof zlib.createZstdDecompress!=='function')throw Error('Native ZSTD unavailable');
 return {size,types,integrity:'Operation + partition SHA256',async *chunks(){const digest=createHash('sha256'),codecs=await createCodecs();for(const op of ops){let chunks;
   if(op.kind===6)chunks=(async function*(){let n=op.size;while(n){let amount=Math.min(n,CHUNK);yield Buffer.alloc(amount);n-=amount;}})();
   else{const input=await source.get(op.offset,op.length);if(op.hash?.length!==32||!timingSafeEqual(createHash('sha256').update(input).digest(),op.hash))throw Error('Operation hash mismatch');
    chunks=op.kind===14?decode([input],()=>zlib.createZstdDecompress({chunkSize:CHUNK,params:{[zlib.constants.ZSTD_d_windowLogMax]:26}})):
       op.kind===1?codecs.bzip(input,op.size):op.kind===8?codecs.xz(input,op.size):[input];}
   let n=0;for await(const b of chunks){n+=b.length;if(n>op.size)throw Error('Operation overflow');digest.update(b);yield b;}if(n!==op.size)throw Error('Truncated operation');
  }if(!timingSafeEqual(digest.digest(),hash))throw Error('Partition SHA256 mismatch');
 }};
}
async function* checked(plan){let pending=null,n=0;for await(const chunk of plan.chunks()){
 if(!n&&chunk.subarray(0,8).toString('ascii')!=='ANDROID!')throw Error('Invalid image magic');n+=chunk.length;
 if(n>(plan.size??256*1024*1024))throw Error('Output overflow');if(pending)yield pending;pending=chunk;
 }if((plan.size!==null&&n!==plan.size)||!pending)throw Error('Incomplete output');yield pending;}
async function download(request,brand,target){
 if((!SOURCES[brand]&&brand!=='samsung')||!['boot','init_boot'].includes(target))return new Response('Unknown fixed test case',{status:404});
 const controller=new AbortController();request.signal.addEventListener('abort',()=>controller.abort(),{once:true});
 const source=brand==='samsung'?await samsungSource(Source,controller.signal,googleRange):await new Source(SOURCES[brand],controller.signal).init();
 const stats=source.parent||source;
 const plan=brand==='samsung'?await samsungPlan(source,target,zipDirectory,memberBase,decode,zlib,LIMIT):brand==='google'?await googlePlan(source,target):await payloadPlan(source,target);
 if(new URL(request.url).searchParams.get('inspect')==='1')return Response.json({brand,target,size:plan.size,types:plan.types,requests:stats.requests,upstreamBytes:stats.bytes});
 const iterator=checked(plan),first=await iterator.next();let initial=first.value;
 const body=new ReadableStream({async pull(c){try{if(initial){c.enqueue(initial);initial=null;return;}const {done,value}=await iterator.next();if(done){console.log(JSON.stringify({brand,target,status:'complete',upstreamBytes:stats.bytes,requests:stats.requests}));c.close();}else c.enqueue(value);}catch(e){controller.abort();console.log(JSON.stringify({brand,target,status:'failed',error:String(e),upstreamBytes:stats.bytes}));c.error(e);}},async cancel(){controller.abort();await iterator.return();console.log(JSON.stringify({brand,target,status:'cancelled',upstreamBytes:stats.bytes}));}},{highWaterMark:0});
 return new Response(body,{headers:{'Content-Type':'application/octet-stream','Content-Disposition':`attachment; filename="${target}.img"`,...(plan.size!==null?{'Content-Length':String(plan.size)}:{}),'Cache-Control':'no-store','X-Robots-Tag':'noindex','X-Fmr-Integrity':plan.integrity,'X-Fmr-Source':'cloudflare-only'}});
}

export default {
  async fetch(request,env) {
    const token=Buffer.from(request.headers.get('Authorization')||'');
    const expected=Buffer.from('Bearer '+(env.LAB_TOKEN||'')),expires=Number(env.LAB_EXPIRES);
    if(typeof env.LAB_TOKEN!=='string'||env.LAB_TOKEN.length<32||!Number.isFinite(expires)||
       Date.now()>expires||token.length!==expected.length||
       !timingSafeEqual(token,expected))return new Response('Not found',{status:404});
    const path=new URL(request.url).pathname.split('/');
    if(path.at(-3)==='download'){try{return await download(request,path.at(-2),path.at(-1));}catch(e){console.log(JSON.stringify({stage:'prepare',error:String(e)}));return Response.json({error:String(e)},{status:422});}}
    return Response.json({
      zlib:Object.keys(zlib).filter(k=>/Decompress|Inflate|crc32/.test(k)),
      aesECB:(()=>{try{let cipher=createCipheriv('aes-128-ecb',Buffer.alloc(16),null);cipher.setAutoPadding(false);return cipher.update(Buffer.alloc(16)).length===16;}catch(e){return String(e);}})(),
      sha256:createHash('sha256').update('test').digest('hex'),
      streams:typeof Readable.toWeb,
      colo:request.cf?.colo,
    },{headers:{'Cache-Control':'no-store','X-Robots-Tag':'noindex'}});
  }
};
