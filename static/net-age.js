(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const accountList = $('age-accounts');
  const dialog = $('age-confirm');
  const rows = new Map([...document.querySelectorAll('[data-age-group]')].map(row => [row.dataset.ageGroup, row]));
  const accountStates = {active:'已登录', login_required:'需重新登录', restoring:'正在恢复', retry:'等待重连'};
  let sharedAccounts = [], currentAccount, needsRelogin = false;
  let availableCounts = {};
  let model, sevenDay, selected, operation, queryVersion = 0, accountVersion = 0, submitting = false, drawTimer;
  const surpriseChecks = new Set();

  async function api(path, values) {
    const response = await fetch(path, values && {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(values)});
    if (response.status === 404 && path === '/api/seven-day/status') return null;
    const data = await response.json();
    if (!response.ok) throw Object.assign(new Error(data.error || '本地服务请求失败'), {code:data.code});
    return data;
  }

  function message(id, value) {
    $(id).textContent = value;
    $(id).hidden = !value;
  }

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text) element.textContent = text;
    return element;
  }

  function accountLabel(account) {
    return account.name && account.name !== account.phone_masked ? `${account.name} · ${account.phone_masked}` : account.phone_masked;
  }

  function renderAccounts() {
    $('age-account-count').textContent = sharedAccounts.length;
    message('age-account-empty', sharedAccounts.length ? '' : '请在账号管理中添加共享账号。');
    accountList.replaceChildren(...sharedAccounts.map(account => {
      const active = account.id === currentAccount?.id;
      const card = node('button', 'coupons-account' + (active ? ' selected' : ''));
      card.type = 'button';
      card.dataset.loginId = account.id;
      card.setAttribute('aria-pressed', String(active));
      const label = node('span', 'coupons-account-label');
      label.append(node('strong', '', account.name || account.phone_masked), node('span', 'coupons-account-reminder'),
        node('span', 'coupons-account-state ' + (account.status === 'active' ? 'active' : 'attention'),
          accountStates[account.status] || '暂不可用'));
      card.append(label, node('small', '', account.phone_masked));
      card.addEventListener('click', () => {
        currentAccount = account;
        for (const other of accountList.children) {
          const pressed = other === card;
          other.classList.toggle('selected', pressed);
          other.setAttribute('aria-pressed', String(pressed));
        }
        return loadStatus();
      });
      return card;
    }));
    renderAccountCounts();
  }

  function renderAccountCounts() {
    for (const card of accountList.children) {
      const count = availableCounts[card.dataset.loginId] || 0;
      const badge = card.querySelector('.coupons-account-reminder');
      badge.hidden = count === 0;
      badge.textContent = String(count);
      badge.title = `${count} 项网龄权益可领取`;
      badge.setAttribute('aria-label', badge.title);
    }
  }

  async function loadAccountCounts(version) {
    const queue = sharedAccounts.filter(account => account.status === 'active' && account.id !== currentAccount?.id);
    let changed = false;
    await Promise.all(Array.from({length:Math.min(3, queue.length)}, async () => {
      while (queue.length && version === accountVersion) {
        const {id} = queue.shift();
        if (id === currentAccount?.id) continue;
        try {
          const {count} = await api('/api/net-age/availability?id='+encodeURIComponent(id));
          if (version === accountVersion && id !== currentAccount?.id && count !== null && availableCounts[id] !== count) {
            availableCounts[id] = count;
            renderAccountCounts();
            changed = true;
          }
        } catch { /* A badge can wait for the next official confirmation. */ }
      }
    }));
    if (changed) window.dispatchEvent(new Event('net-age-counts-changed'));
  }

  function action(area, text, handler, disabled = false) {
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'secondary'; button.textContent = text; button.disabled = disabled;
    button.addEventListener('click', handler);
    area.append(button);
  }

  async function checkSurprise() {
    const {id} = currentAccount, version = queryVersion;
    if (surpriseChecks.has(id)) return;
    surpriseChecks.add(id);
    message('age-feedback', '正在核对官方抽奖次数和奖品记录…');
    render();
    try {
      const data = await api('/api/net-age/surprise?id=' + encodeURIComponent(id));
      if (version !== queryVersion) return;
      const row = model.rows.surprise;
      const prizes = data.prizes.slice(0, 3).map(prize => prize.name).join('、');
      row.status = `抽奖页剩余 ${data.remaining} 次`;
      row.description = [Number.isInteger(row.section_remaining) ? `专区显示 ${row.section_remaining} 次` : '',
        prizes ? `最近奖品：${prizes}` : '官方暂无奖品记录', '官方活动限 APP 内抽奖'].filter(Boolean).join(' · ');
      message('age-feedback', '');
    } catch (error) {
      if (version === queryVersion) message('age-feedback', error.message);
    } finally {
      surpriseChecks.delete(id);
      render();
    }
  }

  function confirm(group, offers, retry = false) {
    selected = {id:currentAccount.id, group};
    if (retry) selected.retry = true;
    operation = null;
    $('age-confirm-title').textContent = group === 'seven' ? '确认参加今日抽奖' : rows.get(group).querySelector('h3').textContent;
    $('age-confirm-account').textContent = accountLabel(currentAccount);
    const choices = $('age-options'); choices.replaceChildren();
    choices.hidden = false;
    $('age-verification').hidden = true;
    $('age-code').value = ''; $('age-code').required = false;
    $('age-send-sms').textContent = '获取验证码';
    $('age-slider-panel').hidden = true;
    $('age-challenge').disabled = false; $('age-slider-submit').disabled = false;
    offers.forEach((offer, index) => {
      const label = document.createElement('label'), input = document.createElement('input');
      input.type = 'radio'; input.name = 'age-choice'; input.value = offer.id; input.required = true; input.checked = index === 0;
      label.append(input, offer.name); choices.append(label);
    });
    $('age-submit').textContent = group === 'seven' ? '确认抽奖' : retry ? '确认重新准备' : '确认领取';
    message('age-confirm-note', retry ? '已查询官方本月订单。请确认上次未领取，再重新准备验证。' : '');
    dialog.showModal();
  }

  function render() {
    $('age-current-account').textContent = currentAccount ? accountLabel(currentAccount) : '尚未选择账号';
    message('age-profile', model ? [model.age, model.level, model.birthday && '网龄生日 '+model.birthday].filter(Boolean).join(' · ') : '');
    const relogin = $('age-relogin');
    relogin.hidden = !currentAccount || (currentAccount.status !== 'login_required' && !needsRelogin);
    relogin.dataset.accountRelogin = currentAccount?.id || '';
    const unavailable = !currentAccount ? '待选账号' : needsRelogin ? '需重新登录'
      : currentAccount.status !== 'active' ? accountStates[currentAccount.status] || '暂不可用' : '';
    for (const [group, row] of rows) {
      const status = row.querySelector('[data-age-status]');
      const detail = row.querySelector('[data-age-detail]');
      const area = row.querySelector('[data-age-actions]');
      area.replaceChildren();
      if (unavailable) {
        status.textContent = unavailable;
        detail.textContent = '';
        continue;
      }
      if (group === 'seven') {
        const account = sevenDay?.accounts.find(item => item.id === currentAccount.id);
        const value = account?.observation;
        if (sevenDay) {
          const link = document.createElement('a'); link.href = '/seven-day'; link.textContent = '打卡详情'; area.append(link);
        }
        status.textContent = sevenDay === undefined ? '待查询' : !sevenDay ? '活动未启用' : !sevenDay.campaign.active ? (sevenDay.campaign.ended ? '活动已结束' : '活动未开放') : account?.running ? '正在核对'
          : value?.chances === null || value?.chances === undefined ? '次数待查询' : value.chances ? '今日可抽 '+value.chances+' 次' : '今日无剩余次数';
        detail.textContent = account?.observation?.draw_prize || account?.message || '每个符合资格的账号每日一次';
        if (sevenDay?.campaign.active && account?.status === 'active') {
          action(area, '查询次数', () => sevenRun('refresh'), account.running || submitting);
          action(area, '抽奖', () => confirm(group, []), !value?.chances || account.running || submitting);
        }
        continue;
      }
      const value = model?.rows[group];
      const offers = value?.offers || [];
      const received = offers.filter(offer => offer.received).map(offer => offer.name);
      const choices = offers.filter(offer => !offer.received);
      status.textContent = value?.status || '待查询';
      detail.textContent = [value?.description, received.length ? '已领 '+received.join('、') : ''].filter(Boolean).join(' · ');
      if (group === 'surprise' && value) {
        const checking = surpriseChecks.has(currentAccount.id);
        action(area, checking ? '正在核对…' : '核对抽奖和奖品', checkSurprise, checking);
      }
      if (value?.recheck) action(area, '核对结果', loadStatus, submitting);
      if (value?.retry_available && choices.length) action(area, '重新准备', () => confirm(group, choices, true), submitting);
      if (value?.available && choices.length) action(area, '选择领取', () => confirm(group, choices));
    }
  }

  async function loadStatus() {
    const account = currentAccount, version = ++queryVersion;
    model = null; sevenDay = undefined; needsRelogin = false;
    clearTimeout(drawTimer);
    render();
    if (account?.status !== 'active') {
      message('age-feedback', '');
      return;
    }
    const {id} = account;
    message('age-feedback', '正在查询官方权益…');
    const campaignQuery = api('/api/seven-day/status').then(campaign => {
      if (version === queryVersion) {
        sevenDay = campaign;
        render();
        if (campaign?.campaign.active) {
          const account = campaign.accounts.find(item => item.id === id);
          if (account?.running) pollSeven(id, false);
          else if (account?.status === 'active') sevenRun('refresh', id, false);
        }
      }
    });
    const [age] = await Promise.allSettled([api('/api/net-age/status?id='+encodeURIComponent(id)), campaignQuery]);
    if (version !== queryVersion) return;
    model = age.status === 'fulfilled' ? age.value : null;
    if (model) {
      availableCounts[id] = model.available_count || 0;
      renderAccountCounts();
      window.dispatchEvent(new Event('net-age-counts-changed'));
    }
    needsRelogin = age.status === 'rejected' && ['APP_LOGIN_REQUIRED','ACTIVITY_LOGIN_REQUIRED'].includes(age.reason.code);
    message('age-feedback', age.status === 'rejected' ? age.reason.message : '');
    render();
  }

  async function loadAccounts() {
    const version = ++accountVersion;
    try {
      const data = await api('/api/net-age/accounts');
      if (version !== accountVersion) return;
      const previous = currentAccount?.id;
      sharedAccounts = data.accounts;
      availableCounts = data.available_counts || {};
      currentAccount = sharedAccounts.find(account => account.id === previous)
        || sharedAccounts.find(account => account.status === 'active') || sharedAccounts[0];
      renderAccounts();
      void loadAccountCounts(version);
      await loadStatus();
    } catch (error) {
      if (version !== accountVersion) return;
      if (!sharedAccounts.length) message('age-account-empty', '账号读取失败，请刷新重试。');
      message('age-feedback', error.message);
    }
  }

  async function pollSeven(id, announce = true, version = queryVersion) {
    if (version !== queryVersion || currentAccount?.id !== id) return;
    clearTimeout(drawTimer);
    try {
      const result = await api('/api/seven-day/status');
      if (version !== queryVersion || currentAccount?.id !== id) return;
      sevenDay = result; render();
      if (sevenDay?.accounts.find(account => account.id === id)?.running) {
        drawTimer = setTimeout(() => pollSeven(id, announce, version), 1200);
      }
    } catch (error) { if (announce && version === queryVersion) message('age-feedback', error.message); }
  }

  async function sevenRun(kind, id = currentAccount.id, announce = true) {
    const version = queryVersion;
    try {
      const data = await api('/api/seven-day/run', {id, kind});
      if (version !== queryVersion || currentAccount?.id !== id) return;
      if (announce) message('age-feedback', data.message);
      await pollSeven(id, announce, version);
    } catch (error) { if (announce && version === queryVersion) message('age-feedback', error.message); }
  }

  async function verify(action, value = {}) {
    return api('/api/net-age/verification/'+action, {id:selected.id, operation:operation.operation, ...value});
  }

  function setBusy(busy) {
    submitting = busy;
    $('age-submit').disabled = busy || !!(operation && !operation.sms);
    $('age-send-sms').disabled = busy || !!(operation?.needs_slider && !operation.slider);
  }

  function verificationNote(text) {
    message('age-confirm-note', [operation?.notice, text].filter(Boolean).join(' '));
  }

  async function verificationStep(run) {
    if (submitting) return;
    setBusy(true);
    verificationNote();
    try { await run(); }
    catch (error) { verificationNote(error.message); }
    finally { setBusy(false); }
  }

  async function sendSms() {
    verificationNote('正在发送验证码…');
    const data = await verify('sms');
    operation.sms = true;
    $('age-send-sms').textContent = '重新获取验证码';
    verificationNote(data.message);
    $('age-code').focus();
  }

  function showVerification(data) {
    operation = data;
    $('age-options').hidden = true;
    $('age-options').replaceChildren();
    $('age-confirm-title').textContent = data.offer;
    $('age-verification').hidden = false;
    $('age-slider-section').hidden = !data.needs_slider;
    $('age-code').required = true;
    $('age-submit').textContent = '确认领取';
    verificationNote();
  }

  $('age-challenge').addEventListener('click', () => verificationStep(async () => {
    const data = await verify('challenge');
    const ground = $('age-slider-ground'), piece = $('age-slider-piece');
    $('age-slider-position').value = '0'; $('age-slider-piece').style.left = '0';
    ground.onload = () => { piece.style.top = (data.ordinate / ground.naturalHeight * 100)+'%'; };
    piece.onload = () => {
      piece.style.width = (piece.naturalWidth / 360 * 100)+'%';
      $('age-slider-position').max = 360 - piece.naturalWidth;
    };
    ground.src = 'data:image/png;base64,'+data.ground;
    piece.src = 'data:image/png;base64,'+data.patch;
    $('age-slider-panel').hidden = false;
    $('age-slider-submit').disabled = false;
    operation.slider = false;
  }));
  $('age-slider-position').addEventListener('input', event => {
    $('age-slider-piece').style.left = (Number(event.target.value) / 360 * 100)+'%';
  });
  $('age-slider-submit').addEventListener('click', () => verificationStep(async () => {
    $('age-slider-submit').disabled = true;
    await verify('slider', {position:Number($('age-slider-position').value)});
    operation.slider = true;
    $('age-slider-panel').hidden = true;
    $('age-challenge').disabled = true;
    await sendSms();
  }));
  $('age-send-sms').addEventListener('click', () => verificationStep(sendSms));

  $('age-confirm-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (submitting) return;
    const group = selected.group;
    setBusy(true);
    try {
      if (group === 'seven') {
        dialog.close(); await sevenRun('draw', selected.id);
      } else {
        const data = operation ? await verify('submit', {code:$('age-code').value})
          : await api('/api/net-age/claim', {...selected, choice:new FormData(event.currentTarget).get('age-choice')});
        if (data.operation) {
          showVerification(data);
          if (!data.needs_slider) await sendSms();
          return;
        }
        operation = null;
        dialog.close();
        if (selected.id === currentAccount?.id) {
          await loadStatus();
          if (selected.id === currentAccount?.id) message('age-feedback', data.message);
        }
      }
    } catch (error) { verificationNote(error.message); }
    finally {
      if (group === 'member' || group === 'birthday') {
        window.dispatchEvent(new Event('net-age-counts-changed'));
      }
      setBusy(false); render();
    }
  });
  $('age-cancel').addEventListener('click', () => { if (!submitting) dialog.close(); });
  dialog.addEventListener('cancel', event => { if (submitting) event.preventDefault(); });
  dialog.addEventListener('close', () => {
    if (operation) verify('cancel').catch(error => message('age-feedback', error.message));
    operation = null;
  });
  $('age-refresh').addEventListener('click', loadAccounts);
  window.addEventListener('accounts-changed', loadAccounts);
  window.addEventListener('net-age-counts-updated', event => {
    availableCounts = event.detail;
    renderAccountCounts();
  });
  loadAccounts();
})();
