import * as zlib from 'node:zlib';
import {createCipheriv,createDecipheriv,createHash,timingSafeEqual} from 'node:crypto';
import {Readable} from 'node:stream';

export default {
  async fetch(request,env) {
    const token=request.headers.get('Authorization')||'';
    const expected='Bearer '+env.LAB_TOKEN;
    if(Date.now()>Number(env.LAB_EXPIRES)||token.length!==expected.length||
       !timingSafeEqual(Buffer.from(token),Buffer.from(expected)))return new Response('Not found',{status:404});
    return Response.json({
      zlib:Object.keys(zlib).filter(k=>/Decompress|Inflate|crc32/.test(k)),
      aesECB:(()=>{try{let cipher=createCipheriv('aes-128-ecb',Buffer.alloc(16),null);cipher.setAutoPadding(false);return cipher.update(Buffer.alloc(16)).length===16;}catch(e){return String(e);}})(),
      sha256:createHash('sha256').update('test').digest('hex'),
      streams:typeof Readable.toWeb,
      colo:request.cf?.colo,
    },{headers:{'Cache-Control':'no-store','X-Robots-Tag':'noindex'}});
  }
};
