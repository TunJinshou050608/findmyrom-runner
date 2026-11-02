import {readFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
import {lz4} from './samsung.mjs';

const fixture=JSON.parse(await readFile(new URL('./codec-fixtures.json',import.meta.url),'utf8'));
let source=await readFile(new URL('./codecs.mjs',import.meta.url),'utf8');
for(const codec of ['xz','bz2']){
 const bytes=await readFile(new URL('./'+codec+'.wasm.b64',import.meta.url),'utf8');
 source=source.replace(`import ${codec}Module from './${codec}.wasm';`,
   `const ${codec}Module=new WebAssembly.Module(Buffer.from(${JSON.stringify(bytes.trim())},'base64'));`);
}
source=source.replace("'./bz2-factory.mjs'",JSON.stringify(new URL('./bz2-factory.mjs',import.meta.url).href));
const {createCodecs}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const codecs=await createCodecs();
for(const [key,decode] of [['bz2',i=>codecs.bzip(i,fixture.size)],['xz',i=>codecs.xz(i,fixture.size)],['lz4',i=>lz4([i])]]){
 const input=Buffer.from(fixture[key],'base64'),hash=createHash('sha256');let n=0;
 for await(const block of decode(input)){hash.update(block);n+=block.length;}
 assert.equal(n,fixture.size);assert.equal(hash.digest('hex'),fixture.sha256);
 console.log(key+': 2MiB synthetic round-trip SHA256 verified');
}
await assert.rejects(async()=>{for await(const _ of codecs.xz(Buffer.from(fixture.xz,'base64').subarray(0,-8),fixture.size)){};});
console.log('Truncated XZ rejected');
