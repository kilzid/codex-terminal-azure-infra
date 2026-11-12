import assert from 'node:assert/strict';
import http from 'node:http';
import { createRequire } from 'node:module';
import { once } from 'node:events';
import { execFileSync } from 'node:child_process';
const WebSocket=createRequire((process.env.TERMINAL_ROOT || '/opt/codex-terminal') + '/package.json')('ws');
let probeStage='http';console.log('NATIVE_STEP='+probeStage);
try {
const host=process.env.PROBE_HOST || 'terminal.example.com';
const origin='https://'+host;
function request(path,method='GET',body,extra={}) {
 return new Promise((resolve,reject)=>{
  const data=body===undefined?undefined:JSON.stringify(body);
  const headers={host,origin,'x-forwarded-proto':'https',...extra};
  if(data!==undefined){headers['content-type']='application/json';headers['content-length']=Buffer.byteLength(data);}
  const req=http.request({hostname:'127.0.0.1',port:Number(process.env.PROBE_PORT || 7681),path,method,headers},res=>{
   const chunks=[];res.on('data',x=>chunks.push(x));
   res.on('end',()=>resolve({status:res.statusCode,text:Buffer.concat(chunks).toString()}));
  });
  req.setTimeout(10000,()=>req.destroy(Error('HTTP probe timed out')));
  req.on('error',reject);req.end(data);
 });
}
const sessions=await request('/api/sessions');
if(sessions.status===404) {
 // Remobi shares the live Herdr session. Never type into the user's active pane.
 assert.equal((await request('/')).status,200,'same-origin terminal page');
 probeStage='websocket';console.log('NATIVE_STEP='+probeStage);
 const ws=new WebSocket('ws://127.0.0.1:'+(process.env.PROBE_PORT || '7681')+'/ws',{headers:{host,origin}});
 let snapshot=false,pong=false,workspaceId;
 const timer=setTimeout(()=>ws.terminate(),30000);
 ws.on('message',raw=>{const m=JSON.parse(raw);if(m.type==='snapshot')snapshot=true;if(m.type==='pong')pong=true;});
 const herdr=(...args)=>execFileSync('herdr',['--session','main',...args],{env:{...process.env,XDG_CONFIG_HOME:(process.env.CODEX_HOME||'/home/node/.codex')+'/terminal-xdg'},encoding:'utf8',timeout:5000,stdio:['ignore','pipe','pipe']}).trim();
 const until=async(check,label)=>{
  for(let i=0;i<100;i++){if(check())return;await new Promise(resolve=>setTimeout(resolve,50));}
  throw Error(label);
 };
 try {
  await once(ws,'open');
  await until(()=>snapshot,'PTY snapshot timeout');
  ws.send(JSON.stringify({type:'ping'}));
  await until(()=>pong,'WebSocket protocol timeout');
  probeStage='origin';console.log('NATIVE_STEP='+probeStage);
  for(const foreignOrigin of ['https://foreign.example','null',undefined]) {
   const foreign=new WebSocket('ws://127.0.0.1:'+(process.env.PROBE_PORT || '7681')+'/ws',{headers:{host,...(foreignOrigin?{origin:foreignOrigin}:{})}});
   const timeout=setTimeout(()=>foreign.terminate(),5000);
   try {
    const status=await new Promise((resolve,reject)=>{
     foreign.once('unexpected-response',(_req,res)=>{res.resume();resolve(res.statusCode);});
     foreign.once('open',()=>resolve(101));foreign.once('error',reject);
    });
    assert.equal(status,401,'foreign/missing WebSocket origin rejected');
   } finally { clearTimeout(timeout);foreign.on('error',()=>{});foreign.terminate(); }
  }
  probeStage='isolated-pty';console.log('NATIVE_STEP='+probeStage);
  const created=JSON.parse(herdr('workspace','create','--cwd','/workspace','--label','deployment-verification','--no-focus')).result;
  workspaceId=created.workspace.workspace_id;
  const paneId=created.root_pane.pane_id;
  assert.match(paneId,/^w[A-Z0-9]+:p[A-Z0-9]+$/);
  herdr('pane','run',paneId,"printf 'DEPLOYMENT_%s\\n' OK");
  await until(()=>herdr('pane','read',paneId).includes('DEPLOYMENT_OK'),'isolated PTY command timeout');
 } finally {
  if(workspaceId)herdr('workspace','close',workspaceId);
  clearTimeout(timer);ws.terminate();
 }
 console.log('DEPLOYED_REMOBI_HTTP_WEBSOCKET_ORIGIN_PTY_OK');
} else {
assert.equal(sessions.status,200,'same-origin session API');
probeStage='origin';console.log('NATIVE_STEP='+probeStage);
assert.equal((await request('/api/sessions','GET',undefined,{origin:'https://foreign.example','sec-fetch-site':'cross-site'})).status,403,'cross-origin rejection');
probeStage='create-session';console.log('NATIVE_STEP='+probeStage);
const created=await request('/api/sessions','POST',{name:'deployment-verification',shell:'/bin/bash',cwd:'/workspace'});
assert.ok([200,201].includes(created.status),'temporary session creation');
const session=JSON.parse(created.text);
assert.equal(typeof session.id,'string','temporary session ID');
let ws;
let connectionTimer;
let failureStage;
try {
 probeStage='websocket';console.log('NATIVE_STEP='+probeStage);
 ws=new WebSocket('ws://127.0.0.1:'+(process.env.PROBE_PORT || '7681')+'/ws',{headers:{host,origin}});
 connectionTimer=setTimeout(()=>ws.terminate(),30000);
 await once(ws,'open');
 let output='';ws.on('message',raw=>{const m=JSON.parse(raw);if(['output','replay'].includes(m.type))output+=m.data;});
 const attached=new Promise(resolve=>ws.on('message',raw=>{if(JSON.parse(raw).type==='attached')resolve();}));
 probeStage='attach';console.log('NATIVE_STEP='+probeStage);
 ws.send(JSON.stringify({type:'attach',sessionId:session.id}));
 await Promise.race([attached,new Promise((_,reject)=>setTimeout(()=>reject(Error('PTY attach timeout')),10000).unref())]);
 ws.send(JSON.stringify({type:'resize',cols:100,rows:30}));
 probeStage='command';console.log('NATIVE_STEP='+probeStage);
 const command="printf 'DEPLOYMENT_%s"+String.fromCharCode(92)+"n' OK"+String.fromCharCode(13);
 ws.send(JSON.stringify({type:'input',data:command}));
 for(let i=0;i<300&&!output.includes('DEPLOYMENT_OK');i++)await new Promise(resolve=>setTimeout(resolve,50));
 assert.ok(output.includes('DEPLOYMENT_OK'),'PTY command output');
 clearTimeout(connectionTimer);probeStage='close';console.log('NATIVE_STEP='+probeStage);
 const closed=once(ws,'close');ws.close();
 await Promise.race([closed,new Promise(resolve=>setTimeout(()=>{ws.terminate();resolve();},5000).unref())]);
 console.log('DEPLOYED_TERMBEAM_HTTP_WEBSOCKET_ORIGIN_OK');
} catch(error) { failureStage=probeStage;throw error; } finally {
 clearTimeout(connectionTimer);
 if(ws&&ws.readyState!==WebSocket.CLOSED)ws.terminate();
 probeStage='delete-session';console.log('NATIVE_STEP='+probeStage);
 const removed=await request('/api/sessions/'+encodeURIComponent(session.id),'DELETE');
 assert.ok([200,204].includes(removed.status),'temporary session cleanup');
 if(failureStage)probeStage=failureStage;
}
}

} catch(error) {
 console.log('NATIVE_PROBE_FAILED:'+probeStage+':'+(typeof error.actual==='number'?error.actual:'error'));
 process.exitCode=1;
}
