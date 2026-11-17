import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

function widget({width=288,challenge='current-challenge'}={}){
  const nodes=Object.fromEntries(['status','verify','retry'].map(id=>[id,{
    hidden:true,textContent:'',handlers:{},addEventListener(name,fn){this.handlers[name]=fn;},replaceChildren(){this.cleared=true;},
  }]));
  let script,config,loadFail,reloaded=0;
  const messages=[],timers=[];
  vm.runInNewContext(fs.readFileSync(new URL('../static/login-captcha.js',import.meta.url),'utf8'),{
    URLSearchParams,location:{origin:'http://localhost:5500',search:challenge?'?challenge_id='+challenge:'',reload(){reloaded++;}},
    document:{getElementById:id=>nodes[id],documentElement:{clientWidth:width},createElement:()=>({}),head:{append(s){script=s;}}},
    parent:{postMessage(data,origin){messages.push({data,origin});}},setTimeout(fn){timers.push(fn);},
    window:{_zw:{Captcha(el,options){config=options;return {on(event,fn){loadFail=fn;}};}}},
  });
  return {nodes,messages,timers,get script(){return script;},get config(){return config;},get loadFail(){return loadFail;},get reloaded(){return reloaded;}};
}

test('login widget uses the APK login appId and fits narrow screens',()=>{
  const env=widget();env.script.onload();
  assert.match(env.script.src,/\/login-verification\/mgt\//);
  assert.equal(env.config.appId,'02aecbb71abe2113e42a602056797e09');
  assert.equal(env.config.width,272);
  assert.equal(env.config.isSaaS,false);
  assert.equal(env.config.showVoice,false);
});

test('human token returns once to same-origin parent and includes original challenge',()=>{
  const env=widget();env.script.onload();
  env.config.success('official-human-token');env.config.success('duplicate-token');env.config.fail();
  assert.equal(env.messages.length,1);
  assert.equal(env.messages[0].origin,'http://localhost:5500');
  assert.equal(env.messages[0].data.type,'app-login-captcha-success');
  assert.equal(env.messages[0].data.challenge_id,'current-challenge');
  assert.equal(env.messages[0].data.token,'official-human-token');
  assert.equal(env.nodes.retry.hidden,true);
});

test('challenge failure stops until user explicitly retries and ignores stale success',()=>{
  for(const mode of ['script','widget','verification']){
    const env=widget();env.script.onload();
    if(mode==='script')env.script.onerror();else if(mode==='widget')env.loadFail();else env.config.fail();
    env.config.success('stale-token');
    assert.equal(env.messages.length,0);
    assert.equal(env.nodes.retry.hidden,false);
    assert.equal(env.timers.length,0);
    assert.equal(env.reloaded,0);
    env.nodes.retry.handlers.click();assert.equal(env.reloaded,1);
  }
});

test('missing challenge and invalid success tokens never report verification success',()=>{
  assert.equal(widget({challenge:''}).script,undefined);
  for(const token of ['',123,'x'.repeat(4097)]){
    const env=widget();env.script.onload();env.config.success(token);
    assert.equal(env.messages.length,0);assert.equal(env.nodes.retry.hidden,false);
  }
});

class Element{
  constructor(tag='div'){this.tagName=tag;this.id='';this.value='';this.children=[];this.handlers={};this.disabled=false;this.hidden=false;
    this.classList={toggle(){}};this.contentWindow={};this.dataset={};}
  addEventListener(name,fn){this.handlers[name]=fn;}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  setAttribute(){}
  focus(){}
  scrollIntoView(){}
  reportValidity(){return true;}
  reset(){this.resetCalled=true;}
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
async function portal({embedded=false,relogin=false,failResume=false,withAccount=false,exchangeFail=false}={}){
  const ids=['storage-notice','pool-count','managed-accounts','send-sms','manager-login-button','code','login-slider-required',
    'login-slider-message','login-captcha','login-captcha-cancel','phone','login-name','login-identity','cancel-relogin',
    'login-panel-title','login-panel-description','manager-login-form','portal-feedback','refresh-accounts'];
  const nodes=new Map(ids.map(id=>[id,Object.assign(new Element(),{id})]));
  const get=id=>nodes.get(id),events={},calls=[],timers=[],dialog={open:true};
  const record={id:'account-a',phone_masked:'199****0003',name:'主号',status:'login_required',created_at:0};
  let blockRefresh=null,batch=0,elapsed=0,appReady=false;
  class BrowserDate extends Date{static now(){return Date.now()+elapsed;}}
  const root={querySelector(selector){return [...nodes.values()].find(n=>'#'+n.id===selector);},
    querySelectorAll(selector){return selector==='[id]'?[...nodes.values()]:[];},closest(){return dialog;},contains(){return false;}};
  const context=vm.createContext({URLSearchParams,Date:BrowserDate,location:{origin:'http://localhost:5500'},
    document:{createElement:tag=>new Element(tag),hidden:false},
    window:{addEventListener(name,fn){events[name]=fn;},publishAccountsChanged(){}},
    setInterval(fn,ms){timers.push({fn,ms});},
    async fetch(url,options){
      const body=options?.body?JSON.parse(options.body):undefined;calls.push({url,body});
      if(!body){if(blockRefresh){const wait=blockRefresh;blockRefresh=null;await wait;}return {ok:true,json:async()=>({accounts:relogin||withAccount?[record]:[]})};}
      if(url.endsWith('/rename'))return {ok:true,json:async()=>({message:'已保存'})};
      if(url.endsWith('/sms')){batch++;return {ok:true,json:async()=>({sms_request_id:'batch-'+batch,expires_in:600,retry_after:0,phone_masked:'199****0003'})};}
      if(body.risk_token){
        if(exchangeFail){appReady=true;return {ok:false,json:async()=>({error:'换票暂时失败',app_login_complete:true})};}
        return {ok:!failResume,json:async()=>failResume?{code:'LOGIN_RESULT_UNKNOWN',error:'结果未确认，已停止重复提交'}:{account:record,message:'已重新登录'}};
      }
      if(appReady)return {ok:true,json:async()=>({account:record,message:'已重新登录'})};
      return {ok:false,json:async()=>({code:'214100',challenge:{id:'challenge-'+batch,app_id:'02aecbb71abe2113e42a602056797e09'}})};
    },
  });
  const source=fs.readFileSync(new URL('../static/portal-controller.js',import.meta.url),'utf8').replace('export function mountPortal','function mountPortal');
  vm.runInContext(source,context);
  const manager=context.mountPortal(root,{manager:true,embedded});await flush();
  get('phone').value='19900000003';get('login-name').value='主号';
  if(relogin){await manager.relogin(record.id);await flush();}
  else{get('send-sms').handlers.click();await flush();}
  get('code').value='123456';
  get('manager-login-form').handlers.submit({preventDefault(){}});await flush();
  const frame=()=>get('login-captcha').children[0];
  const message=(overrides={})=>events.message({source:frame()?.contentWindow,origin:'http://localhost:5500',
    data:{type:'app-login-captcha-success',challenge_id:'challenge-'+batch,token:'human-token'},...overrides});
  return {get,calls,timers,events,frame,message,dialog,advance(ms){elapsed+=ms;timers.find(t=>t.ms===1000).fn();},
    holdRefresh(){let release;blockRefresh=new Promise(resolve=>{release=resolve;});return release;}};
}

test('standalone and embedded relogin complete via human slider without sending another SMS',async()=>{
  for(const embedded of [false,true])for(const relogin of [false,true]){
    const env=await portal({embedded,relogin});
    assert.ok(env.frame(),`embedded=${embedded}, relogin=${relogin}: ${env.get('portal-feedback').textContent}`);assert.match(env.frame().src,/challenge_id=challenge-1/);
    assert.equal(env.get('manager-login-button').disabled,true);
    env.message();await flush();
    const requests=env.calls.filter(c=>c.body);
    assert.equal(requests.length,3);assert.equal(requests.filter(c=>c.url.endsWith('/sms')).length,1);
    const submitted=requests.at(-1).body;
    assert.equal(submitted.risk_token,'human-token');assert.equal(submitted.code,'');
    assert.equal(submitted.sms_request_id,'batch-1');assert.equal(submitted.challenge_id,'challenge-1');
    assert.equal(relogin?submitted.id:submitted.phone,relogin?'account-a':'19900000003');
    assert.equal(env.get('login-slider-required').hidden,true);assert.equal(env.frame(),undefined);
  }
});

test('spoofed callbacks and callbacks from previous accounts or SMS batches cannot submit',async()=>{
  const env=await portal(),original=env.frame();
  env.message({origin:'https://other.example'});env.message({source:{}});
  env.message({data:{type:'coupon-captcha-success',challenge_id:'challenge-1',token:'x'}});
  env.message({data:{type:'app-login-captcha-success',challenge_id:'old-challenge',token:'x'}});
  env.get('phone').value='19900000004';env.get('phone').handlers.input();
  env.message({source:original.contentWindow});await flush();
  assert.equal(env.calls.filter(c=>c.body).length,2);
  assert.equal(env.frame(),undefined);
});

test('cancel and continue keep the original SMS, and discard the removed iframe callback',async()=>{
  const env=await portal(),old=env.frame();
  env.get('login-captcha-cancel').handlers.click();
  assert.equal(env.frame(),undefined);assert.equal(env.get('manager-login-button').disabled,false);
  env.get('manager-login-form').handlers.submit({preventDefault(){}});
  assert.notEqual(env.frame(),old);
  env.message({source:old.contentWindow});await flush();
  assert.equal(env.calls.filter(c=>c.body).length,2);
  env.message();await flush();assert.equal(env.calls.filter(c=>c.body).length,3);
});

test('verified login with uncertain result stops without another slider, login, or SMS',async()=>{
  const env=await portal({failResume:true});env.message();await flush();
  assert.equal(env.calls.filter(c=>c.body).length,3);
  assert.equal(env.frame(),undefined);assert.equal(env.get('manager-login-button').disabled,true);
  assert.match(env.get('login-slider-message').textContent,/结果未确认/);
  env.get('manager-login-form').handlers.submit({preventDefault(){}});await flush();
  assert.equal(env.calls.filter(c=>c.body).length,3);
});

test('a human callback received while another operation is busy resumes when idle',async()=>{
  const env=await portal({withAccount:true}),release=env.holdRefresh();
  const actions=env.get('managed-accounts').children[0].children[4].children[0].children;
  actions.find(button=>button.textContent==='保存备注').handlers.click();await flush();
  env.message();await flush();
  release();await flush();await flush();
  assert.equal(env.calls.filter(c=>c.body?.risk_token).length,1);
});

test('closing an embedded dialog discards the old verification result',async()=>{
  const env=await portal({embedded:true});env.dialog.open=false;env.message();await flush();
  assert.equal(env.frame(),undefined);assert.equal(env.calls.filter(c=>c.body).length,2);
});

test('resending SMS or expiry discards callbacks from the original challenge',async()=>{
  for(const mode of ['resend','expire']){
    const env=await portal(),old=env.frame();
    if(mode==='resend'){env.get('send-sms').handlers.click();await flush();}else env.advance(600001);
    env.message({source:old.contentWindow});await flush();
    assert.equal(env.calls.filter(c=>c.body?.risk_token).length,0);
    assert.equal(env.frame(),undefined);
  }
});

test('temporary Lingxi exchange failure can continue with completed APP login and no new SMS',async()=>{
  const env=await portal({exchangeFail:true});env.message();await flush();
  assert.equal(env.frame(),undefined);assert.equal(env.get('manager-login-button').disabled,false);
  assert.equal(env.get('manager-login-button').textContent,'继续完成登录');
  assert.equal(env.get('code').disabled,true);
  env.get('manager-login-form').handlers.submit({preventDefault(){}});await flush();
  const submitted=env.calls.filter(c=>c.body);
  assert.equal(submitted.filter(c=>c.url.endsWith('/sms')).length,1);
  assert.equal(submitted.filter(c=>c.body.risk_token).length,1);
  assert.equal(submitted.at(-1).body.code,'');
  assert.equal(submitted.at(-1).body.risk_token,undefined);
});
