import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tag='div') {
    Object.assign(this, {tag, children: [], dataset: {}, attributes: {}, value: '', textContent: '',
      className: '', hidden: false, disabled: false, selectedIndex: 0, parent: null});
  }
  append(...children) { for (const child of children) { child.parent=this; this.children.push(child); } }
  replaceChildren(...children) { this.children=[]; this.append(...children); }
  setAttribute(key,value) { this.attributes[key]=value; }
  addEventListener(type, callback) { this[`on${type}`]=callback; }
  focus() { this.focused=true; }
  remove() { if (this.parent) this.parent.children=this.parent.children.filter(child=>child!==this); }
  querySelector(selector) {
    const tag=selector.split('[')[0],type=/type="([^"]+)"/.exec(selector)?.[1];
    return descendants(this).find(node=>node.tag===tag&&(!type||node.type===type));
  }
}

const descendants=node=>[node,...node.children.flatMap(descendants)];
const flush=()=>new Promise(resolve=>setImmediate(resolve));

async function fixture({flow='discount', benefitState={}, firstSmsError, claimError, members, products=[]}={}) {
  const nodes=new Map(),calls=[],events={};
  const get=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  get('member-products-title').textContent='全网随心系';
  get('member-stbk-template').content={firstElementChild:{cloneNode(){
    const form=new Element('form');
    form.append(new Element('label'),Object.assign(new Element('input'),{disabled:true}),
      Object.assign(new Element('button'),{type:'button',textContent:'获取验证码'}),
      Object.assign(new Element('button'),{type:'submit',textContent:'确认领取',disabled:true}));
    return form;
  }}};
  const offer={id:'2191',name:'咪咕视频钻石会员月卡',price_cents:1500};
  const benefit=flow==='discount' ? {
    key:'discount',kind:'五折购',name:'五折购1款',remaining:1,url:'https://dev.coc.10086.cn/halfprize',
    offers:[offer],selection:{offers:[offer],can_check:true,url:'https://dev.coc.10086.cn/halfprize'},
  } : {
    key:'campus',kind:'福袋',name:'权益福袋',claim_flow:'campus',remaining:1,
    selection:{offers:[{...offer,price_cents:0,can_claim:true}]},
  };
  const prefix=`/api/member-benefits/${flow}`;
  async function fetch(url,init) {
    const endpoint=url.split('?')[0],body=init?.body&&JSON.parse(init.body);calls.push({endpoint,body});
    let data;
    if(endpoint==='/api/logins')data={accounts:[{id:'account',status:'active'},{id:'no-products',status:'active'}]};
    else if(endpoint==='/api/member-benefits/status'){
      const accountId=new URL(url,'http://localhost').searchParams.get('login_id');
      data={products:accountId==='account' ? products : [],members:(members || [
        flow==='discount' ? {id:'1447',name:'黔心选铂金会员'} : {id:'1055',name:'铂金会员（校园版）'},
      ]).map(member=>({...member,benefits:member.benefits ?? [{...benefit,...benefitState}]}))};
    }
    else if(endpoint==='/api/member-benefits/suixin/prepare')data={
      operation_id:'operation',name:'样例月卡',agreement:'免费发放至本机号码。',
    };
    else if(endpoint===`${prefix}/prepare`)data=flow==='discount' ? {
      preparation_id:`preparation-${body.member_id}`,url:'https://dev.coc.10086.cn/halfprize',
      eligibility:{eligible:true,message:'官方资格核对通过。'},purchase_preview:{price_cents:1500,
        agreement_name:'业务受理说明',agreement:'按次计费，订购后立即生效。'},
    } : {preparation_id:'preparation',agreement_name:'业务受理说明',agreement:'免费领取。',binding:'权益将发放至：绑定账号'};
    else if(endpoint===`${prefix}/sms`){
      if(firstSmsError){
        const error=firstSmsError;
        firstSmsError=undefined;
        return {ok:false,json:async()=>error};
      }
      data={operation_id:'operation',message:'验证码已发送'};
    }
    else if(endpoint===`${prefix}/verification-sms`)data={message:'官方已发送验证短信'};
    else if(endpoint===`${prefix}/claim`||endpoint===`${prefix}/resume`||endpoint===`${prefix}/verification-claim`){
      if(endpoint===`${prefix}/claim`&&claimError) return {ok:false,json:async()=>claimError};
      data=flow==='discount'
        ? {state:'claimed',claimed:true,status_label:'已订购',message:'官方已确认订购成功。'}
        : {state:'claimed',claimed:true,status_label:'已领取',message:'官方已确认领取成功。'};
    }
    else throw new Error('Unexpected '+endpoint);
    return {ok:true,json:async()=>data};
  }
  vm.runInNewContext(fs.readFileSync(new URL('../static/member-benefits.js',import.meta.url),'utf8'),{
    document:{getElementById:get,createElement:tag=>new Element(tag)},
    window:{addEventListener(type,callback){events[type]=callback;},dispatchEvent(){}},
    Event,URLSearchParams,fetch,
  });
  await flush();
  return {get,calls,events};
}

test('a backend order notice keeps offers visible without exposing an inactive submit control', async () => {
  const notice = '安卓版暂未接入该权益的订单验证，请在电脑版工具台处理。';
  for (const flow of ['campus', 'discount']) {
    const {get, calls} = await fixture({flow, benefitState: {local_order_notice: notice}});
    const panel = get('member-vips');
    const select = panel.querySelector('select');
    assert.ok(select);
    assert.match(select.children[0].textContent, /^查看/);
    assert.equal(select.children[1].textContent,
      flow === 'discount' ? '咪咕视频钻石会员月卡 · 标示价 ¥15.00' : '咪咕视频钻石会员月卡');
    assert.ok(descendants(panel).some(node => node.textContent === notice));
    assert.equal(select.onchange, undefined);
    assert.equal(panel.querySelector('form'), undefined);
    assert.ok(calls.every(call => !call.body));
  }
});

test('suixin requires a selected offer and explicit SMS and claim confirmations',async()=>{
  const {get,calls}=await fixture({flow:'suixin',products:[{
    key:'s1',name:'全网随心系',claim_status:'available',can_claim:true,options:[
      {id:'offer-1',name:'样例月卡',can_claim:true}, {id:'other',name:'另需绑定',can_claim:false},
    ],
  }]});
  const panel=get('member-products');
  const select=panel.querySelector('select');
  assert.deepEqual(select.children.map(node=>node.value),['','offer-1']);
  select.value='offer-1';
  await select.onchange();
  assert.deepEqual(calls.at(-1),{endpoint:'/api/member-benefits/suixin/prepare',body:{
    login_id:'account',product_key:'s1',offer_id:'offer-1',
  }});
  assert.ok(descendants(panel).some(node=>node.textContent==='免费发放至本机号码。'));
  const form=panel.querySelector('form');
  const code=form.querySelector('input');
  assert.equal(code.maxLength,4);
  await form.querySelector('button[type="button"]').onclick();
  assert.deepEqual(calls.at(-1),{endpoint:'/api/member-benefits/suixin/sms',body:{
    login_id:'account',operation_id:'operation',agreed:true,
  }});
  code.value='0123';
  await form.onsubmit({preventDefault(){}});
  assert.deepEqual(calls.find(call=>call.endpoint==='/api/member-benefits/suixin/claim').body,
    {login_id:'account',operation_id:'operation',sms_code:'0123'});
  assert.equal(calls.at(-1).endpoint,'/api/member-benefits/status');
});

test('suixin offers a separate six-digit form when the official verification stays on the claim page',async()=>{
  const {get,calls}=await fixture({flow:'suixin',claimError:{code:'MEMBER_VERIFICATION',error:'需要验证',verification_sms:true},
    products:[{key:'s1',name:'全网随心系',claim_status:'available',can_claim:true,
      options:[{id:'offer-1',name:'样例月卡',can_claim:true}]}]});
  const panel=get('member-products');
  const select=panel.querySelector('select');
  select.value='offer-1';
  await select.onchange();
  const form=panel.querySelector('form'),sms=form.querySelector('button[type="button"]'),code=form.querySelector('input');
  await sms.onclick();
  code.value='0123';
  await form.onsubmit({preventDefault(){}});
  assert.equal(code.maxLength,6);
  assert.equal(code.disabled,true);
  assert.equal(sms.textContent,'获取 6 位验证短信');
  assert.equal(calls.some(call=>call.endpoint.endsWith('/verification-sms')),false);
  await sms.onclick();
  assert.equal(code.disabled,false);
  code.value='001234';
  await form.onsubmit({preventDefault(){}});
  assert.deepEqual(calls.find(call=>call.endpoint.endsWith('/verification-claim')).body,
    {login_id:'account',operation_id:'operation',sms_code:'001234'});
});

test('shared account badges show only their own positive counts and refresh in place',async()=>{
  const {get,calls,events}=await fixture();
  const [first,second]=get('member-accounts').children;
  const badge=button=>button.children.find(node=>node.className==='member-account-count');
  assert.equal(badge(first).hidden,true);
  events['member-benefit-counts-updated']({detail:{account:2,'no-products':1}});
  assert.equal(badge(first).textContent,'2');
  assert.equal(badge(first).hidden,false);
  assert.equal(badge(second).textContent,'1');
  events['member-benefit-counts-updated']({detail:{account:0,'no-products':1}});
  assert.equal(badge(first).hidden,true);
  assert.equal(badge(second).hidden,false);
  assert.equal(get('member-accounts').children[0],first);
  assert.equal(calls.filter(call=>call.endpoint==='/api/member-benefits/status').length,1);
});

test('claimed products hide the generic claim label and keep specific details',async()=>{
  const {get}=await fixture({products:[{
    name:'全网随心系',claim_status:'claimed',catalogue_kind:'account',options:[
      {name:'和包出行20元体验会员',detail:'权益领取',claimed:true},
      {name:'样例月卡',detail:'随心看',claimed:true},
    ],
  }]});
  const products=descendants(get('member-products')).map(node=>node.textContent);
  assert.ok(products.includes('和包出行20元体验会员'));
  assert.ok(products.includes('已领取'));
  assert.ok(products.includes('随心看'));
  assert.ok(!products.includes('权益领取'));
});

test('switching accounts removes the products card when the account has no products',async()=>{
  const {get}=await fixture({products:[{name:'全网随心系',claim_status:'claimed',remaining:0}]});
  const cards=get('member-vips');
  const productsPanel=get('member-products-panel');
  const [withProducts,withoutProducts]=get('member-accounts').children;
  assert.equal(cards.children[0],productsPanel);
  assert.equal(get('member-message').textContent,'已查到 2 项会员。');
  await withoutProducts.onclick();
  assert.equal(cards.children.length,1);
  assert.equal(get('member-message').textContent,'已查到 1 项会员。');
  assert.equal(cards.children[0].querySelector('h2').textContent,'黔心选铂金会员');
  await withProducts.onclick();
  assert.equal(cards.children[0],productsPanel);
  assert.equal(get('member-products').querySelector('small').textContent,'已领取');
});

test('selecting a discount offer checks its price, then explicit confirmation sends SMS',async()=>{
  const env=await fixture(),members=env.get('member-vips');
  const select=descendants(members).find(node=>node.tag==='select');
  assert.equal(select.children[0].textContent,'选择五折购商品（1 项）');
  assert.equal(select.children[0].value,'');
  assert.ok(!descendants(members).some(node=>node.tag==='button'));
  assert.ok(!env.calls.some(call=>call.endpoint.endsWith('/prepare')));
  select.value='2191';
  const preparing=select.onchange();
  assert.equal(select.disabled,true);
  await preparing;
  assert.equal(select.disabled,false);
  assert.deepEqual(env.calls.at(-1),{endpoint:'/api/member-benefits/discount/prepare',body:{
    login_id:'account',member_id:'1447',benefit_key:'discount',sku_id:'2191'}});
  assert.ok(descendants(members).some(node=>node.textContent==='按次计费，订购后立即生效。'));
  assert.ok(!env.calls.some(call=>call.endpoint.endsWith('/sms')));
  const sms=descendants(members).find(node=>node.tag==='button'&&node.type==='button'&&node.textContent.includes('15.00'));
  assert.equal(sms.textContent,'确认 ¥15.00 并获取验证码');
  await sms.onclick();
  assert.deepEqual(env.calls.at(-1),{endpoint:'/api/member-benefits/discount/sms',body:{
    login_id:'account',sku_id:'2191',preparation_id:'preparation-1447',agreed:true}});
  const form=descendants(members).find(node=>node.tag==='form'),code=form.querySelector('input');
  assert.equal(form.querySelector('button[type="submit"]').textContent,'确认订购');
  code.value='0123';await form.onsubmit({preventDefault(){}});
  assert.deepEqual(env.calls.at(-1),{endpoint:'/api/member-benefits/discount/claim',body:{
    login_id:'account',operation_id:'operation',sms_code:'0123'}});
  assert.equal(env.calls.filter(call=>call.endpoint.endsWith('/claim')).length,1);
  assert.deepEqual(descendants(members).find(node=>node.className==='member-claimed-product').children.map(node=>node.textContent),
    ['咪咕视频钻石会员月卡','已领取']);
});

test('a manual challenge resumes the original order only on explicit confirmation',async()=>{
  const url='https://dev.coc.10086.cn/coc3/gr/static-2c/risk-verify/#/voiceCaptcha?riskActionLogId=fixture%2Bvalue';
  const {get,calls}=await fixture({claimError:{code:'MEMBER_VERIFICATION',error:'需要验证',verification_url:url,resume_available:true}});
  const card=get('member-vips').children[0];
  const select=card.querySelector('select');
  select.value='2191';
  await select.onchange();
  const form=card.querySelector('form');
  await form.querySelector('button[type="button"]').onclick();
  form.querySelector('input').value='0123';
  await form.onsubmit({preventDefault(){}});
  const link=card.querySelector('a');
  assert.equal(link.textContent,'打开官方语音验证');
  assert.equal(link.href,url);
  assert.equal(link.target,'_blank');
  assert.equal(link.rel,'noopener noreferrer');
  assert.equal(form.hidden,true);
  assert.equal(form.querySelector('input').value,'');
  assert.equal(calls.filter(call=>call.endpoint.endsWith('/claim')).length,1);
  assert.equal(calls.filter(call=>call.endpoint.endsWith('/resume')).length,0);
  const resume=descendants(card).find(node=>node.tag==='button'&&node.textContent==='已完成验证，继续提交');
  await Promise.all([resume.onclick(),resume.onclick()]);
  assert.deepEqual(calls.filter(call=>call.endpoint.endsWith('/resume')),[{
    endpoint:'/api/member-benefits/discount/resume',body:{login_id:'account',operation_id:'operation'},
  }]);
  assert.equal(calls.filter(call=>call.endpoint.endsWith('/sms')).length,1);
  assert.ok(descendants(card).some(node=>node.textContent==='已领取'));
});

test('campus selection retains bound-account details and its own claim endpoints',async()=>{
  const {get,calls}=await fixture({flow:'campus'}),members=get('member-vips');
  const select=descendants(members).find(node=>node.tag==='select');
  select.value='2191';
  await select.onchange();
  assert.deepEqual(calls.at(-1),{endpoint:'/api/member-benefits/campus/prepare',body:{
    login_id:'account',member_id:'1055',sku_id:'2191'}});
  assert.ok(descendants(members).some(node=>node.textContent==='权益将发放至：绑定账号'));
  const form=descendants(members).find(node=>node.tag==='form');
  const sms=form.querySelector('button[type="button"]');
  assert.equal(sms.textContent,'同意并获取验证码');
  await sms.onclick();
  assert.deepEqual(calls.at(-1),{endpoint:'/api/member-benefits/campus/sms',body:{
    login_id:'account',sku_id:'2191',preparation_id:'preparation',agreed:true}});
  form.querySelector('input').value='0123';
  await form.onsubmit({preventDefault(){}});
  assert.deepEqual(calls.at(-1),{endpoint:'/api/member-benefits/campus/claim',body:{
    login_id:'account',operation_id:'operation',sms_code:'0123'}});
  assert.deepEqual(descendants(members).find(node=>node.className==='member-claimed-product').children.map(node=>node.textContent),
    ['咪咕视频钻石会员月卡','已领取']);
});

test('platinum gift bag uses the local picker and hides actions once claimed',async()=>{
  const members=[{id:'1',name:'铂金会员'}];
  const {get,calls}=await fixture({flow:'campus',members});
  const [card]=get('member-vips').children;
  const select=card.querySelector('select');
  select.value='2191';
  await select.onchange();
  assert.deepEqual(calls.at(-1),{endpoint:'/api/member-benefits/campus/prepare',body:{
    login_id:'account',member_id:'1',sku_id:'2191'}});
  assert.equal(card.querySelector('button[type="button"]').textContent,'同意并获取验证码');
  assert.ok(!calls.some(call=>call.endpoint.endsWith('/sms')));

  const claimed=await fixture({flow:'campus',members,benefitState:{claimed:true,remaining:0}});
  const [claimedCard]=claimed.get('member-vips').children;
  assert.ok(descendants(claimedCard).some(node=>node.textContent==='已领取'));
  assert.equal(claimedCard.querySelector('select'),undefined);
  assert.equal(claimedCard.querySelector('a'),undefined);
});

test('a changed discount price requires selecting and confirming the offer again',async()=>{
  const message='商品价格或受理说明已更新，请重新核对。';
  const {get,calls}=await fixture({firstSmsError:{code:'MEMBER_PRICE_CHANGED',error:message}}),members=get('member-vips');
  const select=descendants(members).find(node=>node.tag==='select');
  select.value='2191';
  await select.onchange();
  await members.querySelector('button[type="button"]').onclick();
  assert.equal(select.value,'');
  assert.equal(select.disabled,false);
  assert.equal(members.querySelector('form'),undefined);
  assert.ok(descendants(members).some(node=>node.textContent===message));
  select.value='2191';
  await select.onchange();
  assert.equal(calls.filter(call=>call.endpoint.endsWith('/prepare')).length,2);
  assert.equal(calls.filter(call=>call.endpoint.endsWith('/sms')).length,1);
  assert.equal(members.querySelector('button[type="button"]').textContent,'确认 ¥15.00 并获取验证码');
});

test('unclaimed supermarket gift bag displays the mini program hint in place of the action',async()=>{
  const hint='请去黔心选小程序领取该权益';
  const gift={key:'gift',kind:'福袋',name:'权益福袋',remaining:1,claim_hint:hint};
  const members=[{id:'1452',name:'黔心选商超会员',benefits:[gift]}];
  const {get}=await fixture({members});
  const card=get('member-vips').children[0];
  assert.ok(descendants(card).some(node=>node.textContent===hint));
  assert.equal(card.querySelector('a'),undefined);
  assert.equal(card.querySelector('select'),undefined);

  gift.claimed=true;
  gift.remaining=0;
  const claimed=await fixture({members});
  const claimedCard=claimed.get('member-vips').children[0];
  assert.ok(descendants(claimedCard).some(node=>node.textContent==='已领取'));
  assert.ok(!descendants(claimedCard).some(node=>node.textContent===hint));
});

test('Guizhou platinum shows the mini program hint with unverified gift bag status',async()=>{
  const hint='请去黔心选小程序领取该权益';
  const {get}=await fixture({members:[{id:'1447',name:'黔心选铂金会员',benefits:[
    {key:'gift',kind:'福袋',name:'权益福袋',remaining:null,claim_hint:hint},
  ]}]});
  const card=get('member-vips').children[0];
  const labels=descendants(card).map(node=>node.textContent);
  assert.ok(labels.includes('权益福袋'));
  assert.ok(labels.includes('领取状态无法核对'));
  assert.ok(labels.includes(hint));
  assert.equal(card.querySelector('a'),undefined);
  assert.equal(card.querySelector('select'),undefined);
});

test('claimed gift bag precedes discount with the same product and status layout',async()=>{
  const {get}=await fixture({members:[{id:'1447',name:'黔心选铂金会员',benefits:[
    {key:'discount',kind:'五折购',name:'五折购1款',claimed:true,claimed_products:['和包出行15元体验会员']},
    {key:'gift',kind:'福袋',name:'权益福袋',claimed:true,claimed_products:['和包出行20元体验会员']},
  ]}]});
  const [,gift,discount]=get('member-vips').children[0].children;
  assert.deepEqual([gift.querySelector('strong').textContent,discount.querySelector('strong').textContent],['权益福袋','五折购']);
  const products=[gift,discount].map(row=>descendants(row).find(node=>node.className==='member-claimed-product'));
  assert.deepEqual(products.map(line=>line.children.map(node=>node.textContent)),[
    ['和包出行20元体验会员','已领取'],['和包出行15元体验会员','已领取'],
  ]);
});

test('pending discount orders show the status and do not offer another purchase',async()=>{
  const message='已有订购提交记录，请刷新核对官方状态。';
  const {get}=await fixture({benefitState:{claim_pending:true,status_message:message}}),members=get('member-vips');
  assert.equal(members.querySelector('select'),undefined);
  assert.ok(descendants(members).some(node=>node.textContent==='订购状态待核对'));
  assert.ok(descendants(members).some(node=>node.textContent===message));
});

test('both Guizhou discount cards keep their own preparation and order status for the same SKU',async()=>{
  const {get,calls}=await fixture({members:[
    {id:'1447',name:'黔心选铂金会员'}, {id:'1452',name:'黔心选商超会员'},
  ]});
  const [platinum,supermarket]=get('member-vips').children;
  for(const [card,memberId,name] of [[platinum,'1447','黔心选铂金会员'],[supermarket,'1452','黔心选商超会员']]){
    const select=card.querySelector('select');
    assert.equal(select.attributes['aria-label'],`${name} 选择五折购商品（1 项）`);
    select.value='2191';
    await select.onchange();
    assert.deepEqual(calls.at(-1),{endpoint:'/api/member-benefits/discount/prepare',body:{
      login_id:'account',member_id:memberId,benefit_key:'discount',sku_id:'2191'}});
  }
  assert.ok(!calls.some(call=>call.endpoint.endsWith('/sms')));
  await supermarket.querySelector('button[type="button"]').onclick();
  assert.equal(calls.at(-1).body.preparation_id,'preparation-1452');
  const form=supermarket.querySelector('form');
  form.querySelector('input').value='0123';
  await form.onsubmit({preventDefault(){}});
  assert.ok(descendants(supermarket).some(node=>node.textContent==='已领取'));
  assert.ok(platinum.querySelector('form'));
  assert.ok(descendants(platinum).some(node=>node.textContent==='本月剩余 1 次'));
});

test('an already submitted order reported during SMS confirmation closes only its own card controls',async()=>{
  const message='已有订购提交记录，请刷新核对官方状态。';
  const {get,calls}=await fixture({
    members:[{id:'1447',name:'黔心选铂金会员'}, {id:'1452',name:'黔心选商超会员'}],
    firstSmsError:{code:'MEMBER_CLAIM_PENDING',error:message},
  });
  const [platinum,supermarket]=get('member-vips').children;
  const select=supermarket.querySelector('select');
  select.value='2191';
  await select.onchange();
  await supermarket.querySelector('button[type="button"]').onclick();
  assert.equal(supermarket.querySelector('select'),undefined);
  assert.equal(supermarket.querySelector('form'),undefined);
  assert.ok(descendants(supermarket).some(node=>node.textContent==='订购状态待核对'));
  assert.ok(descendants(supermarket).some(node=>node.textContent===message));
  assert.ok(platinum.querySelector('select'));
  assert.ok(!calls.some(call=>call.endpoint.endsWith('/claim')));
});
