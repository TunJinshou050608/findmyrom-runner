import {readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
let source=await readFile(new URL('./worker.mjs',import.meta.url),'utf8');
source=source.replace("import {connect} from 'cloudflare:sockets';","const connect=()=>{throw Error('Unexpected network call');};")
 .replace("import {createCodecs} from './codecs.mjs';","const createCodecs=()=>{throw Error('Unexpected decoder call');};")
 .replace("import {samsungSource,samsungPlan} from './samsung.mjs';","const samsungSource=()=>{throw Error('Unexpected FUS call');},samsungPlan=samsungSource;");
const {default:worker}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
for(const [env,bearer] of [[{},'undefined'],[{LAB_TOKEN:'a'.repeat(40)},'a'.repeat(40)],
 [{LAB_TOKEN:'a'.repeat(40),LAB_EXPIRES:'0'},'a'.repeat(40)],
 [{LAB_TOKEN:'a'.repeat(40),LAB_EXPIRES:String(Date.now()+60000)},'wrong']]){
 const r=await worker.fetch(new Request('https://example.invalid/capabilities',{headers:{Authorization:'Bearer '+bearer}}),env);
 assert.equal(r.status,404);
}
const token='a'.repeat(40),r=await worker.fetch(new Request('https://example.invalid/capabilities',{headers:{Authorization:'Bearer '+token}}),{LAB_TOKEN:token,LAB_EXPIRES:String(Date.now()+60000)});
assert.equal(r.status,200);console.log('Missing/expired/incorrect lab credentials fail closed; valid token accepted');
