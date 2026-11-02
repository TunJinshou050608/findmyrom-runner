// Request-scoped decoder instances; imported modules are immutable compiled Wasm.
// XZ context ABI: httptoolkit/xz-decompress 0.2.3 (MIT), Steve Sanderson / Tim Perry.
// BZip2 wrapper: @foxglove/wasm-bz2 0.3.0 (MIT), see bz2-LICENSE.txt.
import xzModule from './xz.wasm';
import bz2Module from './bz2.wasm';
import BzipFactory from './bz2-factory.mjs';

export async function createCodecs(){
 let bz2,xz;
 return {
  async *bzip(input,size){
   if(size>8*1024*1024)throw Error('BZip2 operation exceeds 8MiB staging cap');
   bz2??=await BzipFactory({instantiateWasm(imports,success){const i=new WebAssembly.Instance(bz2Module,imports);success(i,bz2Module);return i.exports;},print(){},printErr(){}});
   const src=bz2._malloc(input.length),dst=bz2._malloc(size);
   try{bz2.HEAPU8.set(input,src);const result=bz2.decompress(dst,size,src,input.length,0);
    if(result.code!==0||result.buffer?.length!==size)throw Error('BZip2 decompression failed '+result.code);
    for(let p=0;p<size;p+=65536)yield Buffer.from(bz2.HEAPU8.subarray(dst+p,dst+Math.min(size,p+65536)));
   }finally{bz2._free(src);bz2._free(dst);}
  },
  async *xz(input,size){
   const e=(xz??=new WebAssembly.Instance(xzModule,{})).exports,ptr=e.create_context();
   if(!ptr)throw Error('XZ context allocation failed');
   let pos=0,written=0,stalled=0;
   try{
    for(let steps=0;steps<100000;steps++){
     let words=new Uint32Array(e.memory.buffer,ptr),mem=new Uint8Array(e.memory.buffer,ptr);
     if(words[2]===words[3]){const n=Math.min(words[0],input.length-pos);
      mem.set(input.subarray(pos,pos+n),words[1]-ptr);pos+=n;e.supply_input(ptr,n);}
     const code=e.get_next_output(ptr);words=new Uint32Array(e.memory.buffer,ptr);
     if(e.memory.buffer.byteLength>80*1024*1024)throw Error('XZ memory cap exceeded');
     if(code!==0&&code!==1)throw Error('XZ decoder error '+code);
     const n=words[5];if(n){written+=n;if(written>size)throw Error('XZ output exceeds extents');yield Buffer.from(new Uint8Array(e.memory.buffer,words[4],n));words[5]=0;}
     if(pos===input.length&&n===0&&code===0){if(++stalled>2)throw Error('Truncated XZ stream');}else stalled=0;
     if(code===1){if(written!==size||pos!==input.length||words[2]!==words[3])throw Error('XZ length or trailing data mismatch');return;}
    }
    throw Error('XZ decoder step limit');
   }finally{e.destroy_context(ptr);}
  }
 };
}
