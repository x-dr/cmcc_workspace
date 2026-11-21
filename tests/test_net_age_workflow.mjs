import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.handlers = {};
    this.dataset = {};
    this.style = {};
    this.classList = {toggle() {}};
  }
  addEventListener(event, handler) { this.handlers[event] = handler; }
  setAttribute() {}
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  querySelector(selector) {
    return this.children.find(item => item.tag === selector || '.' + item.className === selector)
      || this.children.map(item => item.querySelector(selector)).find(Boolean);
  }
  showModal() { this.open = true; }
  focus() { this.focused = true; }
  trigger(event = 'click') {
    return this.handlers[event]?.({preventDefault() {}, currentTarget:this, target:this});
  }
}

const flush = () => new Promise(resolve => setImmediate(resolve));

async function fixture({needsSlider = false, smsResponses = [], sliderFails = false} = {}) {
  const elements = new Map(), calls = [];
  const get = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const group = needsSlider ? 'member' : 'flow';
  const choice = needsSlider ? 'bilibili' : 'monthly';
  const row = new Element('li');
  row.dataset.ageGroup = group;
  const title = new Element('h3'); title.textContent = '待领取权益';
  const actions = new Element('[data-age-actions]');
  row.append(title, new Element('[data-age-status]'), new Element('[data-age-detail]'), actions);
  const fetch = async (path, options) => {
    const endpoint = path.split('?')[0];
    const body = options?.body && JSON.parse(options.body);
    calls.push({endpoint, body});
    const response = (data, status = 200) => ({ok:status === 200, status, json:async () => data});
    switch (endpoint) {
      case '/api/net-age/accounts': return response({accounts:[{id:'account-a',status:'active',phone_masked:'138****0314'}]});
      case '/api/net-age/status': return response({rows:{[group]:{available:true,offers:[{id:choice,name:'待领取权益'}]}}});
      case '/api/seven-day/status': return response({},404);
      case '/api/net-age/claim': return response({operation:'operation-a',offer:'待领取权益',needs_slider:needsSlider});
      case '/api/net-age/verification/challenge': return response({ground:'fixture',patch:'fixture',ordinate:20});
      case '/api/net-age/verification/slider': return sliderFails ? response({error:'官方滑块校验未通过'},400) : response({});
      case '/api/net-age/verification/sms': {
        const next = smsResponses.shift();
        return next ? await next(response) : response({message:'官方已发送验证码'});
      }
      default: throw new Error(`Unexpected request ${path}`);
    }
  };
  vm.runInNewContext(fs.readFileSync(new URL('../static/net-age.js', import.meta.url), 'utf8'), {
    document:{getElementById:get,querySelectorAll:() => [row],createElement:tag => new Element(tag)},
    window:{addEventListener() {},dispatchEvent() {}},
    fetch, Event, setTimeout, clearTimeout, FormData:class { get() { return choice; } },
  });
  await flush();
  const smsCalls = () => calls.filter(call => call.endpoint === '/api/net-age/verification/sms');
  const open = () => actions.children.find(item => item.textContent === '选择领取').trigger();
  const confirm = () => get('age-confirm-form').trigger('submit');
  return {get,calls,smsCalls,open,confirm};
}

test('an approved claim opens the form and requests one code without another click', async () => {
  let release;
  const waiting = new Promise(resolve => { release = resolve; });
  const {get,smsCalls,open,confirm} = await fixture({smsResponses:[async response => {
    await waiting; return response({message:'官方已发送验证码'});
  }]});
  assert.equal(smsCalls().length, 0);
  open();
  assert.equal(smsCalls().length, 0);
  const submitting = confirm();
  await flush();
  await confirm();
  await get('age-send-sms').trigger();
  assert.deepEqual(smsCalls().map(call => call.body), [{id:'account-a',operation:'operation-a'}]);
  assert.equal(get('age-submit').disabled, true);
  release();
  await submitting;
  assert.equal(get('age-code').focused, true);
  assert.equal(get('age-submit').disabled, false);
  assert.equal(get('age-send-sms').textContent, '重新获取验证码');
});

for (const sliderFails of [false, true]) {
  test(`SMS waits for a manual slider result (accepted: ${!sliderFails})`, async () => {
    const {get,calls,smsCalls,open,confirm} = await fixture({needsSlider:true,sliderFails});
    open(); await confirm();
    assert.equal(smsCalls().length, 0);
    assert.equal(get('age-send-sms').disabled, true);
    await get('age-challenge').trigger();
    assert.equal(smsCalls().length, 0);
    await get('age-slider-submit').trigger();
    assert.deepEqual(calls.filter(call => call.endpoint.startsWith('/api/net-age/verification/')).map(call=>call.endpoint.split('/').at(-1)),
      sliderFails ? ['challenge','slider'] : ['challenge','slider','sms']);
    assert.equal(get('age-submit').disabled, sliderFails);
  });
}

test('a failed automatic send leaves an explicit manual retry', async () => {
  const {get,smsCalls,open,confirm} = await fixture({smsResponses:[response=>response({error:'短信接口暂不可用'},502)]});
  open(); await confirm(); await flush();
  assert.equal(smsCalls().length, 1);
  assert.equal(get('age-confirm-note').textContent, '短信接口暂不可用');
  assert.equal(get('age-submit').disabled, true);
  assert.equal(get('age-send-sms').disabled, false);
  await get('age-send-sms').trigger();
  assert.equal(smsCalls().length, 2);
  assert.equal(get('age-submit').disabled, false);
});
