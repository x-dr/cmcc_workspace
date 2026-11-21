import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as M from '../recharge/static/task-model.js';

class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.handlers={};this.dataset={};this.attrs={};this.hidden=false;this.disabled=false;this.checked=false;this.value='';this.classList={toggle(){}};}
  addEventListener(name,fn){this.handlers[name]=fn;}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this.children=items;}
  setAttribute(name,value){this.attrs[name]=value;}
  showModal(){this.open=true;}
  close(){this.open=false;}
  click(){if(!this.disabled)return this.handlers.click?.();}
}
function find(node,text){if(node.textContent===text)return node;for(const child of node.children||[]){const found=find(child,text);if(found)return found;}return null;}
async function fixture({running=false,checkin,serverTime='2026-10-26T10:00:00+08:00'}={}){
  const elements=new Map(),calls=[],listeners={},get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const filters=['all','scheduled','running','attention','ended'].map(v=>{const b=new Element('button');b.dataset.wedFilter=v;return b;});
  const pool=[{id:'a',name:'主号',phone_masked:'138****0001',status:'active'},{id:'b',name:'备用',phone_masked:'138****0002',status:'active'},{id:'c',name:'待登录',phone_masked:'138****0003',status:'login_required'}];
  const accounts=pool.map(a=>({id:'activity-'+a.id,login_id:a.id,name:a.name,phone:a.phone_masked,authenticated:false,activity:{}}));
  const jobs=Object.fromEntries(pool.slice(0,2).map(a=>['activity-'+a.id,{account_id:'activity-'+a.id,managed:true,enabled:true,state:running?'armed':'waiting',weekly:true,next_run_at:'2026-10-28T08:00:00+08:00',run_at:'2026-10-28T08:00:00+08:00',max_attempts:20,attempts:0,submissions:0,message:'每周三自动执行',history:[],updated_at:'2026-10-26T10:00:00+08:00'}]));
  let url='http://localhost:5500/recharge/';
  const fetch=async(path,options)=>{const body=options?.body?JSON.parse(options.body):undefined;calls.push({path,body});let result;
    if(path==='/recharge/api/accounts')result={accounts,logins:pool,jobs,checkin,server_time:serverTime};
    else if(path==='/recharge/api/schedules/run-now'){
      for(const id of body.account_ids)jobs[id].state='preparing';
      result={started:body.account_ids,skipped:[]};
    }
    else if(path==='/recharge/api/checkin/run'){
      const record=checkin.records[body.login_id]||{};
      if(body.submit){
        record.progress={date:'2026-10-26',month:'2026-10',action:'done',today_signed:true,status:'今日已打卡',message:'今日打卡已确认'};
        checkin.records[body.login_id]=record;
      }
      result=record.progress||{action:'sign',message:'今日待打卡'};
    }
    else if(path==='/recharge/api/login/attach')result={account_id:'activity-'+body.login_id};
    else if(path.startsWith('/recharge/api/prizes?'))result={prizes:[{prizeName:'fixture',sendStatus:'UNUSED'}]};
    else if(path==='/recharge/api/schedules/cancel'){for(const id of body.account_ids)jobs[id]={...jobs[id],skip_week:'2026-10-28',next_run_at:'2026-11-04T08:00:00+08:00'};result={message:'已停止'};}
    else throw new Error('Unexpected request '+path);
    return {ok:true,json:async()=>structuredClone(result)};
  };
  const source=fs.readFileSync(new URL('../recharge/static/workspace.js',import.meta.url),'utf8').replace(/^import \* as M from .+;\r?\n/,'');
  vm.runInNewContext(source,{M,document:{getElementById:get,createElement:tag=>new Element(tag),documentElement:{dataset:{apiBase:'/recharge'}},querySelector:()=>({content:'fixture-csrf'}),querySelectorAll:selector=>selector==='[data-wed-filter]'?filters:[],hidden:false},window:{addEventListener:(n,fn)=>listeners[n]=fn},fetch,URL,URLSearchParams,Intl,Date,location:{href:url,search:''},history:{replaceState(_state,_title,next){url=String(next);}},setInterval(){}});
  await new Promise(resolve=>setImmediate(resolve));
  return {get,calls,filters,accounts,jobs,url:()=>url};
}

test('opening Wednesday uses automatic backend tasks and never creates a manual schedule',async()=>{
  const {get,calls}=await fixture();
  assert.equal(get('wed-accounts').children.length,4);
  assert.equal(get('wed-status-rows').children.length,3);
  assert.equal(get('wed-task-rows').children.length,2);
  assert.equal(calls.length,1);assert.equal(calls[0].path,'/recharge/api/accounts');
  assert.equal(calls.some(c=>c.body),false);
});
test('activity and tasks are tabs on one page with no navigation or extra account login',async()=>{
  const {get,calls,url}=await fixture();
  get('wed-tab-tasks').click();
  assert.equal(get('wed-tasks').hidden,false);assert.equal(get('wed-activity').hidden,true);
  assert.equal(url(),'http://localhost:5500/recharge/?tab=tasks');
  get('wed-tab-activity').click();assert.equal(get('wed-activity').hidden,false);
  assert.equal(calls.length,1);
});
test('saved prizes appear beside each account and progress counts submissions',async()=>{
  const {get,calls,jobs}=await fixture();
  Object.assign(jobs['activity-a'],{state:'success',message:'success',attempts:23,submissions:8,
    result:{success:true,prizes:[{shortName:'5元',prizeName:'充值立减券',subTitle:'满30元及以上可用'}]}});
  Object.assign(jobs['activity-b'],{state:'exhausted',message:'已达 20 次提交上限',attempts:32,submissions:20});
  await get('wed-refresh').click();
  const activityRows=get('wed-status-rows').children;
  assert.ok(find(activityRows[0],'5元 充值立减券'));
  assert.ok(find(activityRows[0],'满30元及以上可用'));
  assert.ok(find(activityRows[1],'已达 20 次提交上限'));
  get('wed-tab-tasks').click();
  const progress=get('wed-task-rows').children[0].children[4];
  assert.equal(progress.children[1].value,8);
  assert.equal(progress.children[1].max,20);
  assert.ok(find(progress,'检查 23 轮'));
  assert.ok(calls.every(call=>call.path==='/recharge/api/accounts'));
});
test('account cards scope both tables and show the selected account prizes',async()=>{
  const {get,calls}=await fixture();
  get('wed-accounts').children[1].click();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(get('wed-status-rows').children.length,1);
  assert.equal(get('wed-task-rows').children.length,1);
  assert.match(get('wed-prize-owner').textContent,/主号/);
  assert.equal(get('wed-prizes').children.length,1);
  assert.equal(calls.filter(c=>c.path==='/recharge/api/login/attach').length,1);
  assert.equal(calls.some(c=>c.path==='/recharge/api/schedules'||c.path==='/recharge/api/claim'),false);
});
test('expired account is shown with relogin and does not attempt another business login or claim',async()=>{
  const {get,calls}=await fixture();
  get('wed-accounts').children[3].click();
  assert.equal(calls.length,1);
  const relogin=find(get('wed-status-rows'),'重新登录');
  assert.ok(relogin);assert.equal(relogin.dataset.accountRelogin,'c');
});
test('batch stop applies only to checked rows and preserves automatic service for next week',async()=>{
  const {get,calls}=await fixture();
  get('wed-tab-tasks').click();get('wed-select-all').checked=true;get('wed-select-all').handlers.change();
  assert.equal(get('wed-selected-count').textContent,'已选 2 项');
  get('wed-stop-selected').click();assert.equal(get('wed-stop').open,true);
  await get('wed-stop-form').handlers.submit({preventDefault(){}});
  const stop=calls.find(c=>c.path==='/recharge/api/schedules/cancel');assert.deepEqual(stop.body.account_ids.sort(),['activity-a','activity-b']);
  assert.equal(get('wed-stop').open,false);assert.match(get('wed-feedback').textContent,/下周三会自动恢复/);
  assert.equal(get('wed-select-all').disabled,true);
  assert.equal(calls.some(c=>c.path==='/recharge/api/claim'),false);
});
test('manual status queries stay disabled while automatic preparation is armed',async()=>{
  const {get,calls}=await fixture({running:true});
  const read=find(get('wed-status-rows'),'查询资格');assert.equal(read.disabled,true);read.click();
  assert.equal(calls.length,1);
});

test('device authentication failures show their cause and release the manual claim button',async()=>{
  const {get,jobs}=await fixture({running:true,serverTime:'2026-10-28T09:00:00+08:00'});
  assert.equal(get('wed-claim-now').disabled,true);
  assert.match(get('wed-opening').textContent,/没有空闲的已登录账号/);

  Object.assign(jobs['activity-a'],{state:'failed',enabled:false,failure_code:'DEVICE_AUTH_FAILED',
    message:'官方设备认证未完成，请稍后主动刷新'});
  await get('wed-refresh').click();
  assert.equal(get('wed-claim-now').disabled,false);
  assert.ok(find(get('wed-status-rows'),'设备认证未完成'));
});

test('weekly claim button starts the selected accounts only during the open round',async()=>{
  const closed=await fixture();
  assert.equal(closed.get('wed-claim-now').disabled,true);
  const {get,calls}=await fixture({serverTime:'2026-10-28T08:00:00+08:00'});
  get('wed-accounts').children[1].click();
  await new Promise(resolve=>setImmediate(resolve));
  await get('wed-claim-now').click();
  assert.deepEqual(calls.filter(call=>call.path.endsWith('/schedules/run-now')).map(call=>call.body),[{account_ids:['activity-a']}]);
  assert.equal(get('wed-claim-now').disabled,true);
  get('wed-accounts').children[0].click();
  await get('wed-claim-now').click();
  assert.deepEqual(calls.filter(call=>call.path.endsWith('/schedules/run-now')).map(call=>call.body),[
    {account_ids:['activity-a']},{account_ids:['activity-b']},
  ]);
});

test('manual check-in targets only eligible accounts even with automatic check-in disabled',async()=>{
  const checkin={settings:{enabled:false,time:'00:01'},records:{
    a:{progress:{date:'2026-10-26',month:'2026-10',action:'done',today_signed:true}},
  },active:[]};
  const {get,calls}=await fixture({checkin});
  get('wed-tab-checkin').click();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls.some(call=>call.body?.submit),false);
  assert.equal(get('wed-checkin-sign').disabled,false);
  await get('wed-checkin-sign').click();
  assert.deepEqual(calls.filter(call=>call.body?.submit).map(call=>call.body),[{login_id:'b',submit:true}]);
  assert.equal(get('wed-checkin-sign').disabled,true);
  assert.equal(get('wed-feedback').textContent,'今日打卡已确认');
});

test('the toolbar sign-in follows the selected account',async()=>{
  const {get,calls}=await fixture({checkin:{settings:{enabled:true,time:'00:01'},records:{},active:[]}});
  get('wed-tab-checkin').click();
  await new Promise(resolve=>setImmediate(resolve));
  get('wed-accounts').children[1].click();
  await new Promise(resolve=>setImmediate(resolve));
  await get('wed-checkin-sign').click();
  assert.deepEqual(calls.filter(call=>call.body?.submit).map(call=>call.body),[{login_id:'a',submit:true}]);
});

test('only today’s check-in failure shows the daily retry limit',async()=>{
  const failure={state:'failed',attempts:5,message:'官方活动登录响应格式无效'};
  const checkin={settings:{enabled:true,time:'00:01'},records:{
    a:{...failure,attempt_date:'2026-10-26',attempted_at:'2026-10-26T00:23:00+08:00'},
    b:{...failure,attempt_date:'2026-10-25'},
  },active:[]};
  const {get}=await fixture({checkin});
  const [today,previous]=get('wed-checkin-rows').children;
  assert.ok(find(today,failure.message));
  assert.ok(find(today,'今日失败已达5次，明日再检查'));
  assert.match(today.children[4].children[1].textContent,/^失败时间.*00:23:00$/);
  assert.ok(find(previous,'今日尚未查询'));
  assert.equal(find(previous,failure.message),null);
  assert.equal(find(previous,'今日失败已达5次，明日再检查'),null);
});
