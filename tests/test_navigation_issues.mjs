import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(key) { this.dataset={issueKey:key};this.children=[];this.attributes={};this.hidden=false; }
  setAttribute(key,value) { this.attributes[key]=value; }
  querySelector(selector) { return this.children.find(child=>'.'+child.className===selector)||null; }
  insertBefore(child,before) { const index=this.children.indexOf(before);if(index<0)this.children.push(child);else this.children.splice(index,0,child); }
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
async function fixture(initial={'/seven-day':3},couponTotal=0,memberPage=false,netAgePage=false) {
  const items=[new Element('/seven-day'),new Element('/lingxi/tasks'),new Element('/lingxi/tasks'),new Element('accounts'),new Element('/coupons'),new Element('/member-benefits'),new Element('/net-age')];
  const events={},timers=[],calls=[],dispatched=[];
  const document={hidden:false,getElementById:id=>id==='workspace-navigation'?{querySelectorAll:()=>items}:
    (id==='member-accounts'&&memberPage||id==='age-accounts'&&netAgePage)?{}:null,
    createElement:()=>new Element(),addEventListener:(name,fn)=>{events[name]=fn;}};
  const control={counts:initial,couponTotal,memberCounts:{},netAgeCounts:{},ok:true,wait:null};
  const fetch=async(url,options)=>{
    calls.push({url,options});const counts={...control.counts},wait=control.wait;control.wait=null;
    if(wait)await wait;
    return {ok:control.ok,json:async()=>({counts,coupon_reminders:{total:control.couponTotal},member_benefit_counts:control.memberCounts,net_age_counts:control.netAgeCounts})};
  };
  vm.runInNewContext(fs.readFileSync(new URL('../static/navigation-issues.js',import.meta.url),'utf8'),{
    document,window:{addEventListener:(name,fn)=>{events[name]=fn;},dispatchEvent:event=>dispatched.push(event)},
    CustomEvent:class{constructor(type,{detail}){this.type=type;this.detail=detail;}},fetch,setInterval:fn=>timers.push(fn),
  });
  await flush();return {items,events,timers,calls,control,document,dispatched};
}
const badge=item=>item.querySelector('.workspace-issue-badge');

test('the membership menu shows the available claim count and hides it at zero',async()=>{
  const {items,control,events}=await fixture({'/member-benefits':4});
  const benefits=items[5],count=benefits.querySelector('.workspace-coupon-badge');
  assert.equal(count.hidden,false);
  assert.equal(count.textContent,'4');
  assert.match(count.attributes['aria-label'],/4 次会员权益可领取/);
  assert.equal(badge(benefits),null);
  control.counts={'/member-benefits':0};await events['member-benefits-changed']();
  assert.equal(count.hidden,true);
});

test('the membership sidebar read also publishes per-account counts on the membership page',async()=>{
  const {control,events,dispatched,calls}=await fixture({},0,true);
  control.memberCounts={a:2,b:1};
  await events['member-benefits-changed']();
  const update=dispatched.at(-1);
  assert.equal(update.type,'member-benefit-counts-updated');
  assert.deepEqual(update.detail,{a:2,b:1});
  assert.ok(calls.every(call=>call.url==='/api/navigation/issues'));
});

test('the net-age menu shows an available claim badge and publishes per-account counts',async()=>{
  const {items,control,events,dispatched,calls}=await fixture({'/net-age':3},0,false,true);
  const count=items[6].querySelector('.workspace-coupon-badge');
  assert.equal(count.textContent,'3');
  assert.equal(count.hidden,false);
  assert.match(count.attributes['aria-label'],/3 次网龄权益可领取/);
  assert.equal(badge(items[6]),null);
  control.counts={'/net-age':1};control.netAgeCounts={a:0,b:1};
  await events['net-age-counts-changed']();
  assert.equal(count.textContent,'1');
  assert.equal(dispatched.at(-1).type,'net-age-counts-updated');
  assert.deepEqual(dispatched.at(-1).detail,{a:0,b:1});
  control.counts={'/net-age':0};await events['net-age-counts-changed']();
  assert.equal(count.hidden,true);
  assert.ok(calls.every(call=>call.url==='/api/navigation/issues'));
});

test('wallet useful coupon total updates independently of failure badges through a local-only read',async()=>{
  const {items,control,events,calls}=await fixture({'/coupons':1},14);
  const wallet=items[4],coupons=wallet.querySelector('.workspace-coupon-badge');
  assert.equal(coupons.hidden,false);assert.equal(coupons.textContent,'14');
  assert.match(coupons.attributes['aria-label'],/14 张可用优惠话费券/);
  assert.equal(badge(wallet).textContent,'1','the failure count keeps its original meaning');
  control.couponTotal=0;await events['coupon-reminders-changed']();
  assert.equal(coupons.hidden,true);assert.equal(wallet.children.length,2);
  assert.ok(calls.every(call=>call.url==='/api/navigation/issues'));
});

test('sidebar shows the exact failure count with an accessible alert and hides zero counts',async()=>{
  const {items,calls}=await fixture({'/seven-day':123,'/lingxi/tasks':2});
  assert.equal(badge(items[0]).textContent,'123');
  assert.equal(badge(items[0]).hidden,false);
  assert.equal(badge(items[0]).attributes['aria-label'],'123 个失败或待处理问题');
  assert.equal(badge(items[1]).textContent,'2');
  assert.equal(badge(items[2]).textContent,'2');
  assert.equal(badge(items[3]).hidden,true);
  assert.deepEqual(calls,[{url:'/api/navigation/issues',options:undefined}]);
});

test('periodic local updates decrease counts and hide a recovered problem without duplicating badges',async()=>{
  const {items,control,timers}=await fixture();
  control.counts={'/seven-day':1};await timers[0]();
  assert.equal(badge(items[0]).textContent,'1');
  control.counts={'/seven-day':0};await timers[0]();
  assert.equal(badge(items[0]).hidden,true);
  assert.equal(items[0].children.length,1);
});

test('a dynamically added campaign gets the last confirmed count without another request',async()=>{
  const {items,events,calls}=await fixture({'/new-campaign':4});
  const added=new Element('/new-campaign');items.push(added);
  events['workspace-navigation-changed']();
  assert.equal(badge(added).textContent,'4');
  assert.equal(badge(added).hidden,false);
  assert.equal(calls.length,1);
});

test('hidden pages stop polling and a temporary local failure preserves the previous number',async()=>{
  const {document,items,control,timers,calls,events}=await fixture();
  document.hidden=true;await timers[0]();assert.equal(calls.length,1);
  document.hidden=false;control.ok=false;await events.visibilitychange();
  assert.equal(badge(items[0]).textContent,'3');
  control.ok=true;control.counts={'/seven-day':0};await events.focus();
  assert.equal(badge(items[0]).hidden,true);
});

test('an account login notification during an in-flight refresh is coalesced then processed',async()=>{
  const {items,control,events,calls}=await fixture();
  let release;control.wait=new Promise(resolve=>{release=resolve;});
  const first=events.focus();await flush();
  control.counts={'/seven-day':0};events['accounts-changed']();events['accounts-changed']();
  release();await first;await flush();
  assert.equal(calls.length,3);
  assert.equal(badge(items[0]).hidden,true);
});
