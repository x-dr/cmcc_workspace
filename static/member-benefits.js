(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  const accountsNode = byId('member-accounts');
  const message = byId('member-message');
  const manageLogin = byId('member-manage-login');
  const refresh = byId('member-refresh');
  const membersNode = byId('member-vips');
  const productsPanel = byId('member-products-panel');
  const products = byId('member-products');
  const productsTitle = byId('member-products-title').textContent;
  const starbucksTemplate = byId('member-stbk-template');
  let accounts = [], current = '', requestId = 0;
  let availableCounts = {};
  const accountBadges = new Map();

  function element(tag, text, className = '') {
    const node = document.createElement(tag);
    node.textContent = text;
    node.className = className;
    return node;
  }

  async function get(path, options) {
    const response = await fetch(path, options);
    const data = await response.json();
    if (!response.ok) throw Object.assign(new Error(data.error || '查询未完成，请稍后刷新。'), {
      code: data.code, verificationUrl: data.verification_url, resumeAvailable: data.resume_available,
      verificationSms: data.verification_sms,
    });
    return data;
  }

  function officialLink(url, label) {
    const link = element('a', label, 'secondary');
    link.href = url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    return link;
  }

  function renderAccounts() {
    accountBadges.clear();
    accountsNode.replaceChildren(...accounts.map(account => {
      const label = account.name || account.phone_masked;
      const button = element('button', label, 'secondary member-account');
      button.type = 'button';
      button.setAttribute('aria-pressed', String(account.id === current));
      if (account.name) button.append(element('small', account.phone_masked));
      const badge = element('span', '', 'member-account-count');
      accountBadges.set(account.id, badge);
      button.append(badge);
      button.addEventListener('click', () => selectAccount(account.id));
      return button;
    }));
    renderAccountCounts();
  }

  function renderAccountCounts() {
    for (const [id, badge] of accountBadges) {
      const count = availableCounts[id] || 0;
      badge.hidden = count === 0;
      badge.textContent = String(count);
      badge.title = `${count} 次会员权益可领取（按最近查询统计）`;
      badge.setAttribute('aria-label', badge.title);
    }
  }

  function offerPrice(offer) {
    return offer.price_cents == null ? '' : `标示价 ¥${(offer.price_cents / 100).toFixed(2)}`;
  }

  function feedback(row) {
    const status = element('p', '', 'member-action-status');
    status.setAttribute('role', 'status');
    row.append(status);
    return (text, state = '') => {
      status.textContent = text;
      status.dataset.state = state;
    };
  }

  function bindSmsClaim(member, benefit, row, statusNode, {flow = 'starbucks', skuId = '',
    preparationId = '', smsLabel, onResult, onStale, onBusy, pendingVerification} = {}) {
    const form = starbucksTemplate.content.firstElementChild.cloneNode(true);
    const code = form.querySelector('input');
    const sms = form.querySelector('button[type="button"]');
    const submit = form.querySelector('button[type="submit"]');
    code.id = `member-stbk-code-${member.id}-${benefit.key}`;
    const label = form.querySelector('label');
    label.htmlFor = code.id;
    if (flow === 'campus') {
      label.textContent = `${member.name}福袋领取验证码`;
      sms.textContent = '同意并获取验证码';
    } else if (flow === 'discount') {
      label.textContent = '五折购订购验证码';
      sms.textContent = smsLabel;
      submit.textContent = '确认订购';
    } else if (flow === 'suixin') {
      label.textContent = '随心系领取验证码';
      code.maxLength = 4;
      code.pattern = '[0-9]{4}';
      sms.textContent = '同意并获取验证码';
    }
    row.append(form);
    const showStatus = feedback(row);
    const verification = element('div', '', 'member-inline-actions');
    let verificationUrl = '';
    const loginId = current;
    const version = requestId;
    const active = () => version === requestId;
    const post = (action, data) => get(`/api/member-benefits/${flow}/${action}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({login_id: loginId, ...data}),
    });
    let operationId = pendingVerification?.operation_id || '';
    let verificationSms = Boolean(pendingVerification?.sms);
    let verificationSmsSent = Boolean(pendingVerification?.sms_sent);
    function showSmsVerification(detail) {
      verificationSms = true;
      form.hidden = false;
      label.textContent = '随心系二次验证（6 位短信验证码）';
      code.maxLength = 6;
      code.pattern = '[0-9]{6}';
      sms.textContent = '获取 6 位验证短信';
      submit.textContent = '验证并领取';
      sms.disabled = false;
      code.disabled = submit.disabled = !verificationSmsSent;
      showStatus(detail);
    }
    sms.onclick = async () => {
      sms.disabled = code.disabled = submit.disabled = true;
      onBusy?.(true);
      showStatus('正在发送验证码…');
      try {
        const result = await post(verificationSms ? 'verification-sms' : 'sms', verificationSms ? {operation_id: operationId}
          : flow === 'suixin' ? {operation_id: preparationId, agreed: true}
          : skuId ? {sku_id: skuId, preparation_id: preparationId, agreed: true} : {});
        if (!active()) return;
        if (verificationSms) { verificationSmsSent = true; code.value = ''; }
        else {
          if (operationId !== result.operation_id) code.value = '';
          operationId = result.operation_id;
          sms.textContent = '重新获取验证码';
        }
        showStatus(result.message);
      } catch (error) {
        if (error.code === 'MEMBER_CLAIM_PENDING') window.dispatchEvent(new Event('member-benefits-changed'));
        if (active()) {
          if (error.code === 'MEMBER_CLAIM_PENDING' && onResult) onResult({message: error.message});
          else if (onStale && ['MEMBER_PRICE_CHANGED', 'MEMBER_CLAIM_STALE'].includes(error.code)) onStale(error.message);
          else showStatus(error.message, 'error');
        }
      } finally {
        if (active()) {
          sms.disabled = false;
          code.disabled = submit.disabled = verificationSms ? !verificationSmsSent : !operationId;
          if (!code.disabled) code.focus();
        }
        onBusy?.(false);
      }
    };
    async function submitClaim(resume = false) {
      sms.disabled = code.disabled = submit.disabled = true;
      onBusy?.(true);
      verification.remove();
      showStatus(flow === 'discount' ? '正在提交订购…' : '正在提交领取…');
      try {
        const result = await post(resume ? 'resume' : verificationSms ? 'verification-claim' : 'claim', resume
          ? {operation_id: operationId} : {operation_id: operationId, sms_code: code.value});
        window.dispatchEvent(new Event('member-benefits-changed'));
        if (!active()) return;
        form.remove();
        if (onResult) onResult(result);
        else if (result.claimed) renderClaimed(row, benefit.name, (result.claimed_products || []).map(name => ({name})));
        else {
          statusNode.textContent = result.status_label;
          showStatus(result.message);
        }
      } catch (error) {
        if (error.code === 'MEMBER_CLAIM_PENDING') window.dispatchEvent(new Event('member-benefits-changed'));
        if (!active()) return;
        let detail = error.message;
        if (error.code === 'MEMBER_CLAIM_PENDING') {
          form.remove();
          if (onResult) {
            onResult({message: detail});
            return;
          }
        } else if (['MEMBER_PRICE_CHANGED', 'MEMBER_CLAIM_STALE'].includes(error.code) && onStale) {
          onStale(detail);
          return;
        } else if (error.verificationSms) {
          if (!verificationSms) verificationSmsSent = false;
          showSmsVerification(detail);
          return;
        } else if (error.verificationUrl || error.resumeAvailable) {
          showVerification(error.verificationUrl || verificationUrl, detail, Boolean(error.resumeAvailable));
          return;
        } else if (error.code === 'MEMBER_CAMPUS_DEVICE_REFUSED') {
          form.remove();
          detail += flow === 'discount' ? '可点击刷新核对订购状态。' : '可点击刷新核对领取状态。';
        } else if (flow === 'suixin' && error.code === 'MEMBER_VERIFICATION') {
          form.remove();
          verification.replaceChildren(officialLink('https://dev.coc.10086.cn/coc3/gr/static-2c/rightsget/#/rightsbaw?sourceid=056002',
            '打开中国移动随心系领取页'));
          row.append(verification);
        } else {
          form.hidden = false;
          sms.disabled = false;
          if (['MEMBER_SMS_FORMAT', 'MEMBER_SMS_INVALID', 'MEMBER_CAMPUS_DEVICE_UNAVAILABLE'].includes(error.code)) {
            code.disabled = submit.disabled = false;
          } else operationId = '';
        }
        showStatus(detail, 'error');
      } finally {
        code.value = '';
        onBusy?.(false);
      }
    }
    form.onsubmit = event => {
      event.preventDefault();
      if (operationId && !submit.disabled) return submitClaim();
    };
    function showVerification(url, detail, resumable = true) {
      form.hidden = true;
      verificationUrl = url;
      const label = url.includes('#/voiceCaptcha?') ? '打开官方语音验证' : '打开官方验证';
      const resumeButton = element('button', '已完成验证，继续提交', 'primary');
      resumeButton.type = 'button';
      resumeButton.onclick = () => {
        if (resumeButton.disabled) return;
        resumeButton.disabled = true;
        return submitClaim(true);
      };
      verification.replaceChildren(officialLink(url, label), ...(resumable ? [resumeButton] : []));
      row.append(verification);
      showStatus(`${detail} ${resumable ? '请在官方页面完成验证，再点击「已完成验证，继续提交」。'
        : '请打开官方验证，完成后刷新核对领取状态。'}`, 'error');
    }
    if (pendingVerification?.sms) showSmsVerification(`正在领取：${pendingVerification.name}。请完成 6 位短信验证。`);
    else if (pendingVerification) showVerification(pendingVerification.url, `正在领取：${pendingVerification.name}。`);
  }

  function renderClaimSelection(member, benefit, row, statusNode, flow) {
    if (benefit.claim_pending) { feedback(row)(benefit.status_message); return; }
    const orderNotice = benefit.local_order_notice;
    if (orderNotice && benefit.remaining !== 0) feedback(row)(orderNotice);
    if (benefit.claimed || !benefit.remaining) return;
    const discount = flow === 'discount';
    const offers = (discount && !orderNotice && !benefit.selection?.can_check ? [] : benefit.selection?.offers) || [];
    if (!offers.length) {
      if (!orderNotice) feedback(row)(benefit.selection_error || (discount ? '官方暂无可订购的五折购商品。' : '官方暂无可领取的福袋商品。'));
      return;
    }
    const verb = orderNotice ? '查看' : '选择';
    const prompt = discount ? `${verb}五折购商品（${offers.length} 项）` : `${verb}福袋商品`;
    const select = element('select', '', 'member-offer-select');
    select.setAttribute('aria-label', `${member.name} ${prompt}`);
    const placeholder = element('option', prompt);
    placeholder.value = '';
    select.append(placeholder);
    for (const offer of offers) {
      const option = element('option', discount
        ? [offer.name, offerPrice(offer)].filter(Boolean).join(' · ')
        : offer.name + (offer.can_claim ? '' : ' · 暂不可领取'));
      option.value = offer.id;
      option.disabled = !orderNotice && (discount ? !offer.id : !offer.can_claim);
      select.append(option);
    }
    row.append(select);
    if (orderNotice) return;
    const action = element('div', '', 'member-campus-action');
    row.append(action);
    const accountVersion = requestId;
    const loginId = current;
    const active = () => accountVersion === requestId;
    const showResult = (result, offerName) => {
      if (result.claimed) {
        renderClaimed(row, discount ? '五折购' : benefit.name, [{name: offerName}]);
        return;
      }
      statusNode.textContent = result.status_label || (discount ? '订购状态待核对' : '领取状态待核对');
      action.replaceChildren();
      feedback(action)(result.message);
      select.remove();
    };
    select.onchange = async () => {
      const skuId = select.value;
      action.replaceChildren();
      if (!skuId) return;
      const showStatus = feedback(action);
      showStatus(discount ? '正在核对官方资格和商品价格…' : '正在核对官方领取资格…');
      select.disabled = true;
      try {
        const body = {login_id: loginId, sku_id: skuId, member_id: member.id};
        if (discount) body.benefit_key = benefit.key;
        const detail = await get(`/api/member-benefits/${flow}/prepare`, {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body),
        });
        if (!active()) return;
        const preview = discount ? detail.purchase_preview : detail;
        action.replaceChildren();
        let smsLabel;
        if (discount) {
          const price = `¥${(preview.price_cents / 100).toFixed(2)}`;
          action.append(element('p', `官方商品价 ${price}，实际应付以结算为准。`, 'member-action-status'));
          smsLabel = `确认 ${price} 并获取验证码`;
        } else if (detail.binding) {
          action.append(element('p', detail.binding, 'member-action-status'));
        }
        const terms = element('details', '', 'member-campus-terms');
        terms.append(element('summary', preview.agreement_name), element('p', preview.agreement));
        action.append(terms);
        bindSmsClaim(member, benefit, action, statusNode, {flow, skuId, smsLabel,
          preparationId: detail.preparation_id,
          onBusy: busy => { select.disabled = busy; },
          onResult: result => showResult(result, offers.find(offer => offer.id === skuId)?.name),
          onStale: message => {
            action.replaceChildren();
            feedback(action)(message, 'error');
            select.value = '';
          }});
      } catch (error) {
        if (active()) {
          if (error.code === 'MEMBER_CLAIM_PENDING') showResult({message: error.message});
          else {
            select.value = '';
            showStatus(error.message, 'error');
          }
        }
      } finally {
        if (active()) select.disabled = false;
      }
    };
  }

  function renderSelection(member, benefit, row) {
    const selection = benefit.selection;
    const offers = selection?.offers.length ? selection.offers : benefit.offers;
    const url = selection?.url || benefit.url;
    const checkableOffers = selection?.can_check ? offers.filter(offer => offer.id) : [];
    const actions = element('div', '', 'member-inline-actions');
    const link = officialLink(url, '官方详情');
    if (offers.length) {
      const select = element('select', '', 'member-offer-select');
      select.setAttribute('aria-label', `${member.name} ${benefit.name}商品`);
      if (offers.length > 1) {
        const placeholder = element('option', `选择商品（${offers.length} 项）`);
        placeholder.value = '';
        placeholder.disabled = true;
        select.append(placeholder);
      }
      offers.forEach((offer, index) => {
        const option = element('option', [offer.name, offerPrice(offer)].filter(Boolean).join(' · '));
        option.value = checkableOffers.length ? offer.id || '' : String(index + 1);
        option.disabled = Boolean(checkableOffers.length && !offer.id);
        select.append(option);
      });
      select.selectedIndex = 0;
      if (checkableOffers.length === 1) select.value = checkableOffers[0].id;
      row.append(select);
      if (checkableOffers.length) {
        const check = element('button', '核对领取资格', 'secondary');
        check.type = 'button';
        actions.append(check);
        check.disabled = !select.value;
        link.hidden = true;
        const showStatus = feedback(row);
        let version = 0;
        select.onchange = () => {
          ++version;
          check.disabled = !select.value;
          link.hidden = true;
          showStatus('');
        };
        const accountVersion = requestId;
        const params = new URLSearchParams({login_id: current, member_id: member.id, benefit_key: benefit.key});
        check.onclick = async () => {
          const attempt = ++version;
          const active = () => accountVersion === requestId && attempt === version;
          params.set('sku_id', select.value);
          check.disabled = link.hidden = true;
          showStatus('正在核对官方资格…');
          try {
            const checked = await get('/api/member-benefits/offers?' + params);
            if (!active()) return;
            const {eligible, message} = checked.eligibility;
            showStatus(message, eligible ? '' : 'error');
            link.href = checked.url || url;
            link.textContent = eligible ? '到官方确认' : '官方详情';
            link.hidden = false;
          } catch (error) {
            if (active()) showStatus(error.message, 'error');
          } finally {
            if (active()) check.disabled = false;
          }
        };
      }
    }
    if (benefit.selection_error) feedback(row)(benefit.selection_error, 'error');
    actions.append(link);
    row.append(actions);
  }

  function productStatus(product) {
    const {claim_status: state, claim_label: label} = product;
    const status = label || (state === 'available' ? '可领取' : state === 'claimed' ? '已领取' : '领取状态未核实');
    const badge = element('small', status, 'member-product-status');
    badge.dataset.state = state || 'unknown';
    return badge;
  }

  function renderClaimed(row, title, products) {
    row.dataset.state = 'claimed';
    const heading = element('div', '', 'member-benefit-heading');
    if (title) heading.append(element('strong', title));
    for (const {name, detail} of products.length ? products : [{}]) {
      const line = element('div', '', 'member-claimed-product');
      line.append(element('span', name || '商品名称无法核对'), productStatus({claim_status: 'claimed'}));
      heading.append(line);
      if (detail && detail !== name && detail !== '权益领取') heading.append(element('p', detail, 'member-muted'));
    }
    row.replaceChildren(heading);
  }

  function benefitRow(member, benefit) {
    const row = element('div', '', 'member-benefit');
    const heading = element('div', '', 'member-benefit-heading');
    const discount = benefit.kind === '五折购';
    if (benefit.claimed) {
      renderClaimed(row, discount ? '五折购' : benefit.name, (benefit.claimed_products || []).map(name => ({name})));
      return row;
    }
    const status = benefit.kind === '礼包' ? benefit.status_label || '领取状态查询失败'
      : benefit.claim_pending ? (discount ? '订购状态待核对' : '领取状态待核对')
      : benefit.remaining === null ? (benefit.claim_hint ? '领取状态无法核对' : '剩余次数待核对')
      : `本月剩余 ${benefit.remaining} 次`;
    const statusNode = productStatus({claim_status: 'unknown', claim_label: status});
    heading.append(element('strong', benefit.name), statusNode);
    row.append(heading);
    if (benefit.kind === '礼包') {
      if (benefit.can_claim) bindSmsClaim(member, benefit, row, statusNode);
      else if (benefit.status_message) feedback(row)(benefit.status_message);
    } else if (discount || benefit.claim_flow === 'campus') {
      renderClaimSelection(member, benefit, row, statusNode, discount ? 'discount' : 'campus');
    } else if (benefit.claim_hint) {
      if (benefit.remaining !== 0) feedback(row)(benefit.claim_hint);
    } else {
      renderSelection(member, benefit, row);
    }
    return row;
  }

  function renderSuixinClaim(row, product, statusNode) {
    const action = element('div', '', 'member-campus-action');
    const loginId = current, version = requestId;
    const identity = {id: 'suixin'};
    const onResult = result => {
      if (result.claimed || result.state === 'submitted') return selectAccount(loginId);
      statusNode.textContent = '领取状态待核对';
      action.replaceChildren();
      feedback(action)(result.message);
      select?.remove();
    };
    let select;
    if (product.verification) {
      row.append(action);
      bindSmsClaim(identity, product, action, statusNode, {flow: 'suixin', onResult,
        pendingVerification: product.verification});
      return;
    }
    select = element('select', '', 'member-offer-select');
    select.setAttribute('aria-label', '选择随心系权益');
    const placeholder = element('option', '选择一项随心系权益');
    placeholder.value = '';
    select.append(placeholder);
    for (const offer of product.options.filter(option => option.can_claim)) {
      const option = element('option', offer.name);
      option.value = offer.id;
      select.append(option);
    }
    row.append(select, action);
    select.onchange = async () => {
      action.replaceChildren();
      if (!select.value) return;
      const report = feedback(action);
      report('正在核对官方领取说明…');
      select.disabled = true;
      try {
        const result = await get('/api/member-benefits/suixin/prepare', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({login_id: loginId, product_key: product.key, offer_id: select.value}),
        });
        if (version !== requestId) return;
        const terms = element('details', '', 'member-campus-terms');
        terms.append(element('summary', '领取说明'), element('p', result.agreement));
        action.replaceChildren(element('strong', result.name), terms);
        bindSmsClaim(identity, product, action, statusNode, {flow: 'suixin', preparationId: result.operation_id,
          onResult, onBusy: busy => { select.disabled = busy; }, onStale: message => {
            action.replaceChildren();
            feedback(action)(message, 'error');
            select.value = '';
          }});
      } catch (error) {
        if (version === requestId) {
          if (error.code === 'MEMBER_CLAIM_PENDING') onResult({message: error.message});
          else { select.value = ''; report(error.message, 'error'); }
        }
      } finally {
        if (version === requestId) select.disabled = false;
      }
    };
  }

  function render(data) {
    const cards = data.members.map(member => {
      const card = element('section', '', 'pool-panel member-panel');
      const benefits = [...member.benefits].sort((first, second) => (first.kind === '五折购') - (second.kind === '五折购'));
      card.append(element('h2', member.name), ...benefits.map(benefit => benefitRow(member, benefit)));
      if (!member.benefits.length) {
        const row = element('div', '', 'member-benefit');
        row.append(element('span', '查看该会员的其他权益'), officialLink(member.url, '官方详情'));
        card.append(row);
      }
      return card;
    });
    products.replaceChildren(...data.products.map(product => {
      const row = element('div', '', 'member-benefit');
      const repeatsTitle = product.name === productsTitle;
      const options = product.options || [];
      if (product.claim_status === 'claimed') {
        const claimed = options.filter(option => option.claimed);
        renderClaimed(row, repeatsTitle ? '' : product.name, claimed);
        return row;
      }
      row.dataset.state = product.claim_status;
      const heading = element('div', '', 'member-benefit-heading');
      if (!repeatsTitle) heading.append(element('strong', product.name));
      const statusNode = productStatus(product);
      heading.append(statusNode);
      if (product.detail && product.detail !== product.name) {
        heading.append(element('p', product.detail, 'member-muted'));
      }
      row.append(heading);
      if (product.can_claim || product.verification) {
        renderSuixinClaim(row, product, statusNode);
        return row;
      }
      if (options.length) {
        const items = element('ul', '', 'member-product-options');
        for (const option of options) {
          const item = element('li', option.name);
          if (product.catalogue_kind === 'account' && (option.claimed || option.available)) {
            item.append(productStatus({
              claim_status: option.claimed ? 'claimed' : 'available',
            }));
          }
          if (option.detail && option.detail !== option.name && option.detail !== '权益领取') {
            item.append(element('p', option.detail, 'member-muted'));
          }
          items.append(item);
        }
        row.append(items);
      }
      if (product.message) feedback(row)(product.message);
      return row;
    }));
    if (data.products.length) {
      productsPanel.hidden = false;
      cards.unshift(productsPanel);
    }
    membersNode.replaceChildren(...cards);
    membersNode.hidden = false;
    message.textContent = cards.length ? `已查到 ${cards.length} 项会员。` : '本次未查到该账号的会员资料。';
    if (data.product_note) message.textContent += ` 全网随心系查询失败。${data.product_note}`;
  }

  async function selectAccount(id) {
    const version = ++requestId;
    current = id;
    renderAccounts();
    membersNode.hidden = true;
    manageLogin.hidden = true;
    refresh.disabled = true;
    delete message.dataset.state;
    message.textContent = '正在查询该账号的会员权益…';
    try {
      const data = await get('/api/member-benefits/status?' + new URLSearchParams({login_id: id}));
      window.dispatchEvent(new Event('member-benefits-changed'));
      if (version === requestId) render(data);
    } catch (error) {
      if (version === requestId) {
        message.dataset.state = 'error';
        message.textContent = error.message;
        manageLogin.hidden = !['MEMBER_SESSION_EXPIRED', 'MEMBER_LOGIN_REQUIRED'].includes(error.code);
      }
    } finally {
      if (version === requestId) refresh.disabled = false;
    }
  }

  async function loadAccounts() {
    try {
      const data = await get('/api/logins');
      accounts = data.accounts;
      const selected = accounts.find(account => account.id === current)
        || accounts.find(account => account.status === 'active') || accounts[0];
      if (selected) await selectAccount(selected.id);
      else {
        ++requestId;
        current = '';
        renderAccounts();
        membersNode.hidden = true;
        refresh.disabled = true;
        manageLogin.hidden = true;
        delete message.dataset.state;
        message.textContent = '请先在账号管理登录中国移动账号。';
      }
    } catch (error) {
      message.textContent = error.message;
    }
  }

  refresh.addEventListener('click', () => selectAccount(current));
  manageLogin.addEventListener('click', () => {
    window.dispatchEvent(new CustomEvent('open-account-manager', {detail: {loginId: current}}));
  });
  window.addEventListener('accounts-changed', loadAccounts);
  window.addEventListener('member-benefit-counts-updated', event => {
    availableCounts = event.detail;
    renderAccountCounts();
  });
  loadAccounts();
})();
