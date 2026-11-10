import assert from 'node:assert/strict';
import http from 'node:http';
import { createRequire } from 'node:module';
import { once } from 'node:events';
const WebSocket=createRequire('/opt/codex-terminal/package.json')('ws');
const host=process.env.PROBE_HOST || 'terminal.example.com';
const origin='https://'+host;
function request(path,method='GET',body,extra={}) {
 return new Promise((resolve,reject)=>{
  const data=body===undefined?undefined:JSON.stringify(body);
  const headers={host,origin,'x-forwarded-proto':'https',...extra};
  if(data!==undefined){headers['content-type']='application/json';headers['content-length']=Buffer.byteLength(data);}
  const req=http.request({hostname:'127.0.0.1',port:7681,path,method,headers},res=>{
   const chunks=[];res.on('data',x=>chunks.push(x));
   res.on('end',()=>resolve({status:res.statusCode,text:Buffer.concat(chunks).toString()}));
  });
  req.setTimeout(10000,()=>req.destroy(Error('HTTP probe timed out')));
  req.on('error',reject);req.end(data);
 });
}
assert.equal((await request('/api/sessions')).status,200,'same-origin session API');
assert.equal((await request('/api/sessions','GET',undefined,{origin:'https://foreign.example','sec-fetch-site':'cross-site'})).status,403,'cross-origin rejection');
const created=await request('/api/sessions','POST',{name:'deployment-verification',shell:'/bin/bash',cwd:'/workspace'});
assert.ok([200,201].includes(created.status),'temporary session creation');
const session=JSON.parse(created.text);
assert.equal(typeof session.id,'string','temporary session ID');
let ws;
try {
 ws=new WebSocket('ws://127.0.0.1:7681/ws',{headers:{host,origin}});
 const timeout=setTimeout(()=>ws.terminate(),30000);
 await once(ws,'open');
 let output='';ws.on('message',raw=>{const m=JSON.parse(raw);if(['output','replay'].includes(m.type))output+=m.data;});
 const attached=new Promise(resolve=>ws.on('message',raw=>{if(JSON.parse(raw).type==='attached')resolve();}));
 ws.send(JSON.stringify({type:'attach',sessionId:session.id}));
 await Promise.race([attached,new Promise((_,reject)=>setTimeout(()=>reject(Error('PTY attach timeout')),10000).unref())]);
 ws.send(JSON.stringify({type:'resize',cols:100,rows:30}));
 const command="printf 'DEPLOYMENT_%s"+String.fromCharCode(92)+"n' OK"+String.fromCharCode(13);
 ws.send(JSON.stringify({type:'input',data:command}));
 for(let i=0;i<300&&!output.includes('DEPLOYMENT_OK');i++)await new Promise(resolve=>setTimeout(resolve,50));
 assert.ok(output.includes('DEPLOYMENT_OK'),'PTY command output');
 clearTimeout(timeout);ws.close();await once(ws,'close');
 console.log('DEPLOYED_TERMBEAM_HTTP_WEBSOCKET_ORIGIN_OK');
} finally {
 if(ws&&ws.readyState!==WebSocket.CLOSED)ws.terminate();
 const removed=await request('/api/sessions/'+encodeURIComponent(session.id),'DELETE');
 assert.ok([200,204].includes(removed.status),'temporary session cleanup');
}
