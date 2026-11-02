import * as zlib from 'node:zlib';
import {createCipheriv,createDecipheriv,createHash,timingSafeEqual} from 'node:crypto';
import {Readable} from 'node:stream';

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
class Source{
 constructor(url,signal){this.url=url;this.signal=signal;this.bytes=0;this.requests=0;this.size=null;this.etag=null;}
 async response(start,length){
  if(!Number.isSafeInteger(start)||!Number.isSafeInteger(length)||start<0||length<=0||this.bytes+length>LIMIT||++this.requests>48)throw Error('Upstream budget exceeded');
  const headers={'Range':`bytes=${start}-${start+length-1}`,'Accept-Encoding':'identity'};
  if(this.etag)headers['If-Match']=this.etag;
  const r=await fetch(this.url,{headers,redirect:'manual',signal:this.signal});
  const m=/^bytes (\d+)-(\d+)\/(\d+)$/.exec(r.headers.get('Content-Range')||'');
  const encoding=(r.headers.get('Content-Encoding')||'identity').toLowerCase();
  const oplus=/\.allawn(?:o)?fs\.com$/.test(new URL(this.url).hostname);
  if(r.status!==206||!m||Number(m[1])!==start||Number(m[2])!==start+length-1||Number(m[3])<=start+length-1||
    (this.size!==null&&this.size!==Number(m[3]))||(encoding!=='identity'&&!(oplus&&encoding==='utf-8'))){await r.body?.cancel();throw Error(`Invalid upstream range HTTP ${r.status}`);}
  const tag=r.headers.get('ETag');if(this.etag&&tag&&this.etag!==tag){await r.body.cancel();throw Error('Source changed');}
  if(tag&&!tag.startsWith('W/'))this.etag=tag;this.size=Number(m[3]);
  return r;
 }
 async *stream(start,length){const r=await this.response(start,length),reader=r.body.getReader();let read=0;
  try{while(true){const {done,value}=await reader.read();if(done)break;read+=value.length;this.bytes+=value.length;if(read>length||this.bytes>LIMIT)throw Error('Upstream overflow');yield Buffer.from(value);}if(read!==length)throw Error('Truncated upstream');}
  finally{await reader.cancel().catch(()=>{});reader.releaseLock();}
 }
 async get(start,length){if(length>16*1024*1024)throw Error('Metadata/input buffer exceeds 16MiB');const list=[];for await(const b of this.stream(start,length))list.push(b);return Buffer.concat(list,length);}
 async init(){await this.get(0,1);return this;}
}
async function zipDirectory(source,base=0,size=source.size){
 const n=Math.min(size,65557),tail=await source.get(base+size-n,n);let p=tail.length-22;
 for(;p>=0;p--)if(u32(tail,p)===0x06054b50&&p+22+u16(tail,p+20)===tail.length)break;
 if(p<0)throw Error('ZIP EOCD missing');
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
  const offset=base+24+ms+ss+one(op,2,0),length=one(op,3,0);if(offset<base||offset+length>base+items[0].size||length>16*1024*1024)throw Error('Operation bound exceeded');
  return {kind,start:ranges[0].start,size:end-ranges[0].start,offset,length,hash:one(op,8)};
 }).sort((a,b)=>a.start-b.start);
 let cursor=0;for(const op of ops){if(op.start!==cursor)throw Error('Overlapping/incomplete partition');cursor+=op.size;}if(cursor!==size)throw Error('Incomplete partition');
 const types=[...new Set(ops.map(o=>o.kind))];
 if(types.some(t=>![0,6,14].includes(t)))throw Error('Codec required: '+types.join(','));
 if(types.includes(14)&&typeof zlib.createZstdDecompress!=='function')throw Error('Native ZSTD unavailable');
 return {size,types,integrity:'Operation + partition SHA256',async *chunks(){const digest=createHash('sha256');for(const op of ops){let chunks;
   if(op.kind===6)chunks=(async function*(){let n=op.size;while(n){let amount=Math.min(n,CHUNK);yield Buffer.alloc(amount);n-=amount;}})();
   else{const input=await source.get(op.offset,op.length);if(op.hash?.length!==32||!timingSafeEqual(createHash('sha256').update(input).digest(),op.hash))throw Error('Operation hash mismatch');
    chunks=op.kind===14?decode([input],()=>zlib.createZstdDecompress({chunkSize:CHUNK})): [input];}
   let n=0;for await(const b of chunks){n+=b.length;if(n>op.size)throw Error('Operation overflow');digest.update(b);yield b;}if(n!==op.size)throw Error('Truncated operation');
  }if(!timingSafeEqual(digest.digest(),hash))throw Error('Partition SHA256 mismatch');
 }};
}
async function* checked(plan){let pending=null,n=0;for await(const chunk of plan.chunks()){
 if(!n&&chunk.subarray(0,8).toString('ascii')!=='ANDROID!')throw Error('Invalid image magic');n+=chunk.length;
 if(n>plan.size)throw Error('Output overflow');if(pending)yield pending;pending=chunk;
 }if(n!==plan.size||!pending)throw Error('Incomplete output');yield pending;}
async function download(request,brand,target){
 if(!SOURCES[brand]||!['boot','init_boot'].includes(target))return new Response('Unknown fixed test case',{status:404});
 const controller=new AbortController();request.signal.addEventListener('abort',()=>controller.abort(),{once:true});
 const source=await new Source(SOURCES[brand],controller.signal).init();
 const plan=brand==='google'?await googlePlan(source,target):await payloadPlan(source,target);
 const iterator=checked(plan),first=await iterator.next();let initial=first.value;
 const body=new ReadableStream({async pull(c){try{if(initial){c.enqueue(initial);initial=null;return;}const {done,value}=await iterator.next();if(done){console.log(JSON.stringify({brand,target,status:'complete',upstreamBytes:source.bytes,requests:source.requests}));c.close();}else c.enqueue(value);}catch(e){controller.abort();console.log(JSON.stringify({brand,target,status:'failed',error:String(e),upstreamBytes:source.bytes}));c.error(e);}},async cancel(){controller.abort();await iterator.return();console.log(JSON.stringify({brand,target,status:'cancelled',upstreamBytes:source.bytes}));}},{highWaterMark:0});
 return new Response(body,{headers:{'Content-Type':'application/octet-stream','Content-Disposition':`attachment; filename="${target}.img"`,'Content-Length':String(plan.size),'Cache-Control':'no-store','X-Robots-Tag':'noindex','X-Fmr-Integrity':plan.integrity,'X-Fmr-Source':'cloudflare-only'}});
}

export default {
  async fetch(request,env) {
    const token=request.headers.get('Authorization')||'';
    const expected='Bearer '+env.LAB_TOKEN;
    if(Date.now()>Number(env.LAB_EXPIRES)||token.length!==expected.length||
       !timingSafeEqual(Buffer.from(token),Buffer.from(expected)))return new Response('Not found',{status:404});
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
