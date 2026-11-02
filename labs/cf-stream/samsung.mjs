// FUS protocol adaptation follows topjohnwu/samloader-rs (Apache-2.0).
// Attribution/license already in docs/licenses/samloader-rs.txt.
import {createCipheriv,createDecipheriv,createHash} from 'node:crypto';
const HOST='https://neofussvr.sslcs.cdngc.net/';
const KEY=Buffer.from('422e73733617ae2b198940fd4e32b0a5','hex');
const BUILD='S928BXXS6EZJ1/S928BOXM6EZJ1/S928BXXS6EZJ1/S928BXXS6EZJ1';
const EXPECTED={boot:'54688aff1c242f6fdd92f80b8d4b96917d7abcef89c6bc0210ce803a6f780e4d',init_boot:'ef1c6c13fad89b25730b8cfcbab5ad319c53287b6484e8d0dbbec3829f06f0e9'};
const logic=(s,n)=>[...n].map(c=>s[c.charCodeAt(0)&15]||'.').join('');
const escape=s=>s.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
function xml(values,inform=false){return '<FUSMsg><FUSHdr><ProtoVer>1.0</ProtoVer><SessionID>0</SessionID><MsgID>1</MsgID></FUSHdr><FUSBody><Put>'+(inform?'<CmdID>1</CmdID>':'')+Object.entries(values).map(([k,v])=>`<${k}><Data>${escape(v)}</Data></${k}>`).join('')+'</Put>'+(inform?'<Get><CmdID>2</CmdID><BINARY_SW_VERSION/></Get>':'')+'</FUSBody></FUSMsg>';}
async function text(response){if(!response.ok)throw Error('FUS HTTP '+response.status);let n=0,out='';const reader=response.body.getReader(),decoder=new TextDecoder();try{while(true){const {done,value}=await reader.read();if(done)break;n+=value.length;if(n>1048576)throw Error('FUS metadata cap');out+=decoder.decode(value,{stream:true});}}finally{await reader.cancel().catch(()=>{});}if(/<!DOCTYPE|<!ENTITY/i.test(out))throw Error('Unsafe XML');return out;}
export async function samsungSource(Source,signal,tlsRequest){
 let nonce='',signature='';const cookies=new Map();
 const headers=()=>({'User-Agent':'SMART 2.0','Accept':'application/xml','Content-Type':'application/xml','Authorization':`FUS nonce="${nonce}", signature="${signature}", nc="", type="", realm="", newauth="1"`,...(cookies.size?{Cookie:[...cookies].map(([k,v])=>k+'='+v).join('; ')}:{})});
 async function request(path,body=''){
  const r=await tlsRequest(HOST+path,headers(),signal,'POST',body);
  const next=r.headers.get('nonce');if(next){nonce=next;const c=createCipheriv('aes-128-ecb',KEY,null);c.setAutoPadding(false);signature=Buffer.concat([c.update(Buffer.from(nonce.slice(0,16).padEnd(16,'0'))),c.final()]).toString('hex');}
  const sessions=r.headers.getSetCookie?.()||[r.headers.get('Set-Cookie')].filter(Boolean);
  for(const s of sessions){const pair=s.split(';')[0],p=pair.indexOf('=');if(p>0)cookies.set(pair.slice(0,p),pair.slice(p+1));}return text(r);
 }
 await request('NF_SmartDownloadGenerateNonce.do');
 const body=await request('NF_SmartDownloadBinaryInform.do',xml({ACCESS_MODE:'1',BINARY_NATURE:'1',REQUEST_TYPE:'2',LOGIC_CHECK:logic(BUILD,nonce),BINARY_SW_VERSION:BUILD,BINARY_LOCAL_CODE:'EUX',BINARY_MODEL_NAME:'SM-S928B'},true));
 const status=/<Status>([^<]+)<\/Status>/.exec(body)?.[1];if(!['200','S00'].includes(status))throw Error('FUS inform status '+status);
 const values={};for(const m of body.matchAll(/<([A-Z_]+)>\s*<Data>([^<]*)<\/Data>\s*<\/\1>/g))values[m[1]]=m[2];
 const filename=values.BINARY_NAME,path=values.MODEL_PATH,version=values.BINARY_SW_VERSION||values.LATEST_FW_VERSION;
 if(version!==BUILD||!/^SM-S928B_[A-Za-z0-9_.-]+\.enc4$/.test(filename)||!/^\/[A-Za-z0-9_/-]+\/$/.test(path))throw Error('Unexpected fixed Samsung sample');
 const seed=logic(version,values.LOGIC_VALUE_FACTORY||values.LOGIC_VALUE_HOME||'');if(!seed)throw Error('Missing decryption seed');const key=createHash('md5').update(seed).digest();
 async function init(){await request('NF_SmartDownloadBinaryInitForMass.do',xml({BINARY_NAME:filename,BINARY_SW_VERSION:version,DEVICE_LOCAL_CODE:'EUX',DEVICE_MODEL_TYPE:values.DEVICE_MODEL_TYPE,LOGIC_CHECK:logic(filename.slice(-25,-9),nonce)}));}
 await init();const source=new Source(`http://cloud-neofussvr.samsungmobile.com/NF_SmartDownloadBinaryForMass.do?file=${path}${filename}`,signal);
 source.extraHeaders=headers;source.refresh=async()=>{await request('NF_SmartDownloadGenerateNonce.do');await init();};await source.init();
 function decrypt(data){const d=createDecipheriv('aes-128-ecb',key,null);d.setAutoPadding(false);return Buffer.concat([d.update(data),d.final()]);}
 if(source.size%16)throw Error('AES input alignment');const tail=decrypt(await source.get(source.size-16,16)),padding=tail.at(-1);
 if(padding<1||padding>16||!tail.subarray(-padding).equals(Buffer.alloc(padding,padding)))throw Error('Invalid AES key/padding');
 const size=source.size-padding;
 return {parent:source,size,async get(start,length){if(start<0||length<=0||start+length>size)throw Error('AES range bound');const a=Math.floor(start/16)*16,b=Math.ceil((start+length)/16)*16;return decrypt(await source.get(a,b-a)).subarray(start-a,start-a+length);},
  async *stream(start,length){const a=Math.floor(start/16)*16,b=Math.ceil((start+length)/16)*16,d=createDecipheriv('aes-128-ecb',key,null);d.setAutoPadding(false);let skip=start-a,left=length;
   for await(const raw of source.stream(a,b-a)){let plain=d.update(raw);if(skip){const n=Math.min(skip,plain.length);plain=plain.subarray(n);skip-=n;}if(plain.length){const n=Math.min(left,plain.length);if(n)yield plain.subarray(0,n);left-=n;}}
   const rest=d.final();if(rest.length&&left){const n=Math.min(left,rest.length);yield rest.subarray(0,n);left-=n;}if(left)throw Error('Truncated AES stream');
  }};
}
class Cursor{
 constructor(iter){this.iter=(iter[Symbol.asyncIterator]||iter[Symbol.iterator]).call(iter);this.buffer=Buffer.alloc(0);}
 async fill(){if(this.buffer.length)return;const r=await this.iter.next();if(r.done)throw Error('Truncated TAR/LZ4');this.buffer=Buffer.from(r.value);}
 async read(n){const out=Buffer.alloc(n);let p=0;while(p<n){await this.fill();const take=Math.min(n-p,this.buffer.length);this.buffer.copy(out,p,0,take);this.buffer=this.buffer.subarray(take);p+=take;}return out;}
 async skip(n){while(n){await this.fill();const take=Math.min(n,this.buffer.length);this.buffer=this.buffer.subarray(take);n-=take;}}
 async *take(n){while(n){await this.fill();const take=Math.min(n,this.buffer.length);yield this.buffer.subarray(0,take);this.buffer=this.buffer.subarray(take);n-=take;}}
 async close(){await this.iter.return?.();}
}
function block(input,history,maximum){
 const out=Buffer.alloc(maximum);let p=0,o=0;
 function extra(n){if(n===15){let b;do{if(p>=input.length)throw Error('Truncated LZ4 length');b=input[p++];n+=b;}while(b===255);}return n;}
 while(p<input.length){const token=input[p++],literal=extra(token>>>4);if(p+literal>input.length||o+literal>maximum)throw Error('LZ4 literal bound');input.copy(out,o,p,p+literal);p+=literal;o+=literal;if(p===input.length)break;
  if(p+2>input.length)throw Error('Truncated LZ4 match');const distance=input.readUInt16LE(p);p+=2;const n=extra(token&15)+4;if(!distance||distance>o+history.length||o+n>maximum)throw Error('LZ4 match bound');
  for(let i=0;i<n;i++){const from=o-distance;out[o++]=from<0?history[history.length+from]:out[from];}
 }
 return out.subarray(0,o);
}
export async function* lz4(input){
 const cursor=new Cursor(input);let history=Buffer.alloc(0);
 try{
  const h=await cursor.read(6);if(h.readUInt32LE(0)!==0x184d2204||(h[4]>>>6)!==1)throw Error('Unsupported LZ4 frame');const flags=h[4],maximum={4:65536,5:262144,6:1048576,7:4194304}[(h[5]>>>4)&7];
  if(!maximum||flags&1)throw Error('Unsupported LZ4 dictionary');let expected=null;if(flags&8){const s=await cursor.read(8);const n=s.readBigUInt64LE(0);if(n>268435456n)throw Error('LZ4 output bound');expected=Number(n);}await cursor.read(1);
  let total=0;
  while(true){const length=(await cursor.read(4)).readUInt32LE(0);if(!length)break;const size=length&0x7fffffff;if(size>maximum||!size)throw Error('LZ4 block bound');const raw=await cursor.read(size);
   const data=length>>>31?raw:block(raw,flags&32?Buffer.alloc(0):history,maximum);if(flags&16)await cursor.read(4);total+=data.length;if(total>268435456||(expected!==null&&total>expected))throw Error('LZ4 output overflow');
   history=data.length>=65536?data.subarray(-65536):Buffer.concat([history,data]).subarray(-65536);yield data;
  }
  if(flags&4)await cursor.read(4);if(expected!==null&&total!==expected)throw Error('LZ4 size mismatch');
 }finally{await cursor.close();}
}
export async function samsungPlan(source,target,zipDirectory,memberBase,decode,zlib,budget){
 const entries=await zipDirectory(source),aps=entries.filter(i=>/^AP_[^/]+\.tar(?:\.md5)?$/.test(i.name.split('/').at(-1)));
 if(aps.length!==1||![0,8].includes(aps[0].method))throw Error('Unsupported Samsung AP');const item=aps[0],offset=await memberBase(source,item);
 return {size:null,integrity:'Pinned sample SHA256; whole AP CRC not read',async *chunks(){
  const left=budget-source.parent.bytes-32;if(left<=0)throw Error('Input budget exceeded');const raw=source.stream(offset,Math.min(item.compressed,left)),tar=new Cursor(item.method===8?decode(raw,()=>zlib.createInflateRaw({chunkSize:65536})):raw);
  try{for(let i=0;i<4096;i++){const h=await tar.read(512);if(h.equals(Buffer.alloc(512)))throw Error('Target absent');const octal=(a,b)=>parseInt(h.toString('ascii',a,b).replace(/\0/g,'').trim()||'0',8);
    const stored=octal(148,156);let checksum=0;for(let p=0;p<512;p++)checksum+=p>=148&&p<156?32:h[p];if(checksum!==stored)throw Error('TAR checksum mismatch');
    const name=h.subarray(0,100).toString().split('\0')[0].split('/').at(-1),size=octal(124,136);if(!Number.isSafeInteger(size)||size<0)throw Error('Invalid TAR size');
    if([target+'.img',target+'.img.lz4'].includes(name)){
     if(size>268435456||![0,48].includes(h[156]))throw Error('Invalid TAR target');const digest=createHash('sha256');let written=0;
     const data=tar.take(size),chunks=name.endsWith('.lz4')?lz4(data):data;for await(const b of chunks){written+=b.length;if(written>268435456)throw Error('Image bound exceeded');digest.update(b);yield b;}
     if(written<65536||digest.digest('hex')!==EXPECTED[target])throw Error('Pinned Samsung sample SHA256 mismatch');return;
    }await tar.skip(Math.ceil(size/512)*512);
   }throw Error('TAR member count exceeded');
  }finally{await tar.close();}
 }};
}
