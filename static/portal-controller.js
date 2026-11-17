export function mountPortal(root, {manager = false, embedded = false, onLogin, onReloginCancel} = {}) {
const isManager = manager;
const prefix = embedded ? 'modal-' : '';
if (embedded) {
  root.querySelectorAll('[id]').forEach(item => { item.id = prefix + item.id; });
  root.querySelectorAll('label[for]').forEach(item => { item.htmlFor = prefix + item.htmlFor; });
}
const byId = id => root.querySelector('#' + prefix + id);
let working = false, reloginId = '';
let poolRecords = [];
let focusedLoginId = '';
let loginCaptcha = null;
const smsAttempts = new Map();
const smsKey = () => reloginId ? 'id:' + reloginId : byId('phone').value.trim();
async function portalApi(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw Object.assign(new Error(data.error || '操作失败，请重试。'), {
    code:data.code, sms:data.sms, retryAfter:data.retry_after, challenge:data.challenge, appLoginComplete:data.app_login_complete,
  });
  return data;
}
function portalFeedback(text, success = false) {
  byId('portal-feedback').textContent = text;
  byId('portal-feedback').classList.toggle('success', success);
}
function node(tag, className, text) {
  const item = document.createElement(tag); item.className = className || '';
  if (text !== undefined) item.textContent = text;
  return item;
}
async function refreshPool() {
  const data = await portalApi('/api/logins');
  poolRecords = data.accounts;
  let storageNotice = byId('storage-notice');
  if (!storageNotice) {
    storageNotice = node('p','portal-feedback'); storageNotice.id = prefix + 'storage-notice'; storageNotice.setAttribute('role','status');
    root.querySelector('.portal-header').after(storageNotice);
  }
  storageNotice.textContent = data.warning || ''; storageNotice.hidden = !data.warning;
  byId('pool-count').textContent = data.accounts.length;
  if (!isManager) {
    byId('overview-account-count').textContent = data.accounts.length;
    byId('overview-login-required').textContent = data.accounts.filter(record => record.status === 'login_required').length;
  }
  const host = byId(isManager ? 'managed-accounts' : 'home-accounts');
  const rows = data.accounts.map(record => {
    if (!isManager) {
      const pill = node('div', 'account-pill');
      const title = node('span', '', record.name || record.phone_masked);
      pill.append(node('i'), title);
      pill.classList.toggle('needs-attention', !!record.status && record.status !== 'active');
      if (record.name) pill.append(node('small', '', record.phone_masked));
      if (record.status && record.status !== 'active') pill.append(node('small','',loginStatus(record)));
      return pill;
    }
    const row = node('tr', 'managed-account ' + (record.status === 'active' ? 'account-ready' : record.status === 'login_required' ? 'account-expired' : 'account-pending'));
    row.dataset.loginId = record.id; row.dataset.status = record.status;
    row.classList.toggle('account-focused', record.id === focusedLoginId);
    const identity = node('td', 'account-identity');
    identity.append(node('strong', '', record.phone_masked));
    const stamp = new Date(record.created_at * 1000).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai', hour12:false});
    identity.append(node('small', 'account-time', stamp));
    const name = node('input'); name.value = record.name || ''; name.placeholder = '添加账号备注'; name.maxLength = 40;
    name.setAttribute('aria-label', record.phone_masked + '的账号备注');
    const nameCell = node('td', 'account-name'); nameCell.append(name);
    const stateCell = node('td', 'account-state-cell');
    stateCell.append(node('span', 'account-state', loginStatus(record)));
    const message = node('td', 'session-message', record.message || '—');
    const actions = node('div', 'row-actions');
    const relogin = node('button', 'portal-secondary', '重新登录'); relogin.type = 'button';
    relogin.addEventListener('click', () => managerAction(() => reloginAccount(record)));
    actions.append(relogin);
    for (const [label, endpoint, style] of [['保存备注','rename','portal-quiet'],['校验并重连','check','portal-quiet'],['退出账号','logout','logout-action']]) {
      const button = node('button', style, label); button.type = 'button';
      button.addEventListener('click', () => managerAction(async () => {
        const result = await portalApi('/api/logins/' + endpoint, {id: record.id, name: name.value});
        portalFeedback(result.message || (endpoint === 'logout' ? '该账号已退出，两个功能中的对应会话已清除。' : '已保存。'), true);
        window.publishAccountsChanged(); await refreshPool();
      }));
      actions.append(button);
    }
    const actionCell = node('td', 'account-actions'); actionCell.append(actions);
    row.append(identity, nameCell, stateCell, message, actionCell); return row;
  });
  if (!rows.length) {
    if (isManager) {
      const row = node('tr'), cell = node('td', 'account-table-empty', '账号池还是空的，在登录区添加第一个账号。');
      cell.colSpan = 5; row.append(cell); rows.push(row);
    } else rows.push(node('p','muted', '尚无登录账号，打开账号管理即可添加。'));
  }
  host.replaceChildren(...rows);
}
function loginStatus(record) {
  return {active:'● 已登录 · 自动维护', restoring:'◌ 正在自动恢复', retry:'◌ 等待自动重试', login_required:'○ 需要短信验证'}[record.status] || '● 已登录';
}
function managerBusy(value) {
  working = value;
  root.querySelectorAll('form input, button[type="submit"], .row-actions button, #'+prefix+'cancel-relogin').forEach(item => item.disabled = value);
  smsCountdown();
}
async function managerAction(action) {
  if (working) return;
  managerBusy(true);
  try { await action(); } catch(error) { portalFeedback(error.message); }
  finally { managerBusy(false); }
}
function smsCountdown() {
  const button = byId('send-sms');
  const attempt = smsAttempts.get(smsKey());
  const wait = Math.max(0,Math.ceil(((attempt?.readyAt || 0) - Date.now())/1000));
  button.textContent = wait ? wait + ' 秒后重发' : (reloginId || attempt?.sliderRequired ? '重新发送验证码' : '获取验证码'); button.disabled = working || wait > 0;
  const expired=!(attempt?.expiresAt > Date.now()),blocked=attempt?.sliderRequired;
  const canContinue=blocked&&attempt.challenge&&!attempt.captchaConsumed&&attempt.paused;
  byId('manager-login-button').disabled = working || expired || (blocked&&!canContinue);
  byId('manager-login-button').textContent=attempt?.loginComplete?'继续完成登录':blocked&&attempt.challenge?'继续滑块验证':reloginId?'确认重新登录':'登录并加入账号池';
  byId('code').disabled = working || Boolean(attempt?.sliderRequired) || Boolean(attempt?.loginComplete);
  byId('login-slider-required').hidden = !blocked||expired;
  byId('login-slider-message').textContent=attempt?.sliderMessage||(attempt?.paused?'已取消验证。点击“继续滑块验证”可继续本次登录。':attempt?.challenge?'请在下方完成官方滑块，通过后自动继续登录，无需重发短信。':'本次验证已停止，请稍后主动重新获取验证码。');
  byId('login-captcha-cancel').hidden=!blocked||!attempt.challenge||attempt.paused||attempt.captchaConsumed;
  byId('login-captcha-cancel').disabled=working;
  const visible=!embedded||root.closest('dialog').open;
  if(expired||!blocked||!attempt.challenge||attempt.paused||attempt.captchaConsumed||!visible
      ||loginCaptcha&&(loginCaptcha.key!==smsKey()||loginCaptcha.requestId!==attempt.requestId||loginCaptcha.challengeId!==attempt.challenge.id))clearLoginCaptcha();
  if(!working&&!expired&&blocked&&attempt.challenge&&!attempt.paused&&!attempt.captchaConsumed&&visible&&!loginCaptcha){
    const frame=node('iframe');frame.title='中国移动官方登录滑块';
    frame.src='/static/login-captcha.html?'+new URLSearchParams({challenge_id:attempt.challenge.id});
    loginCaptcha={key:smsKey(),requestId:attempt.requestId,challengeId:attempt.challenge.id,frame};
    byId('login-captcha').replaceChildren(frame);
  }
  if(!working&&loginCaptcha?.token){
    const captcha={risk_token:loginCaptcha.token,challenge_id:loginCaptcha.challengeId};
    attempt.captchaConsumed=true;clearLoginCaptcha();
    managerAction(()=>submitManagerLogin(captcha));
  }
}
function clearLoginCaptcha(){
  if(!loginCaptcha)return;
  loginCaptcha=null;byId('login-captcha').replaceChildren();
}
function setRelogin(record = null) {
  reloginId = record ? record.id : '';
  byId('login-identity').hidden = !!reloginId;
  byId('login-identity').disabled = !!reloginId;
  byId('cancel-relogin').hidden = !reloginId;
  byId('login-panel-title').textContent = record ? '重新登录 · ' + (record.name || record.phone_masked) : '添加登录账号';
  byId('login-panel-description').textContent = record ? '验证码将发送至 ' + record.phone_masked + '，只需填写验证码。' : '已有登录会直接复用，不再重复发送验证码。';
  byId('manager-login-button').textContent = record ? '确认重新登录' : '登录并加入账号池';
  byId('code').value = '';
  smsCountdown();
}
async function reloginAccount(record) {
  setRelogin(record);
  byId('manager-login-form').scrollIntoView({block:'nearest', behavior:'smooth'});
  await sendManagerSms(); byId('code').focus();
}
async function sendManagerSms(resend = false) {
  const key = smsKey(), previous = smsAttempts.get(key);
  const identity = reloginId ? {id:reloginId, force:true} : {phone:key};
  portalFeedback('正在获取当前账号的登录验证码…');
  let result;
  try {
    result = await portalApi(reloginId ? '/api/logins/relogin/sms' : '/api/logins/sms',
      {...identity, resend, sms_request_id:previous?.requestId});
  } catch(error) {
    if(error.code==='SMS_COOLDOWN'){
      smsAttempts.set(key,{...previous,readyAt:Date.now()+error.retryAfter*1000});
    }else if(resend){
      smsAttempts.delete(key);
      byId('code').value='';
    }
    throw error;
  }
  if (result.reused) {
    const relogging = !!reloginId;
    smsAttempts.delete(key); byId('code').value = '';
    if (relogging) setRelogin();
    portalFeedback(relogging ? '该账号登录已恢复，无需再次短信验证。' : '该手机号已在账号池中，已复用现有登录，无需再次验证码。', true);
    window.publishAccountsChanged(); onLogin?.(result.account); await refreshPool(); return;
  }
  acceptSms(key,result);
  if (result.slider_required) { portalFeedback(''); return; }
  portalFeedback(result.sms_reused ? '已有有效验证码，请填写最近收到的短信。' : '验证码已发送至 ' + result.phone_masked,true);
}
function acceptSms(key,result) {
  const now=Date.now();
  smsAttempts.set(key,{requestId:result.sms_request_id,readyAt:now+result.retry_after*1000,
    expiresAt:now+(result.expires_in??600)*1000,sliderRequired:result.slider_required===true,challenge:result.challenge,
    loginComplete:result.app_login_complete===true});
  if(reloginId)byId('login-panel-description').textContent='请填写 '+result.phone_masked+' 最近收到的登录验证码。';
  byId('code').value='';
  smsCountdown();
}
async function submitManagerLogin(captcha={}) {
  const key=smsKey(),attempt=smsAttempts.get(key),relogging=Boolean(reloginId);
  portalFeedback(captcha.risk_token?'滑块已通过，正在核对登录结果…':relogging?'正在重新登录并更新共享凭据…':'正在登录并加入共享账号池…');
  const identity=relogging?{id:reloginId,force:true}:{phone:key,name:byId('login-name').value.trim()};
  let result;
  try{
    result=await portalApi(relogging?'/api/logins/relogin':'/api/logins/login',
      {...identity,code:captcha.risk_token||attempt.loginComplete?'':byId('code').value.trim(),sms_request_id:attempt.requestId,...captcha});
  }catch(error){
    if(error.code==='214100'){
      smsAttempts.set(key,{...attempt,sliderRequired:true,challenge:error.challenge,captchaConsumed:false,paused:false,
        sliderMessage:error.challenge?'':error.message});
      byId('code').value='';portalFeedback('');return;
    }
    if(error.code==='SMS_FLOW_CHANGED'||error.code==='SMS_EXPIRED'){
      if(error.sms)acceptSms(key,error.sms);
      else smsAttempts.delete(key);
    }
    if(error.appLoginComplete){
      smsAttempts.set(key,{...attempt,sliderRequired:false,challenge:null,loginComplete:true});
    }else if(captcha.risk_token&&error.code==='213010'){
      smsAttempts.set(key,{...attempt,sliderRequired:false,challenge:null,captchaConsumed:false});
    }else if(captcha.risk_token&&smsAttempts.get(key)===attempt){
      smsAttempts.set(key,{...attempt,sliderRequired:true,challenge:null,sliderMessage:error.message});
    }
    if(['213010','SMS_FLOW_CHANGED','SMS_EXPIRED'].includes(error.code))byId('code').value='';
    throw error;
  }
  smsAttempts.delete(key);
  if(relogging)setRelogin();
  else byId('manager-login-form').reset();
  portalFeedback(relogging?result.message:'账号已加入，灵犀和周三充值日均可选择使用。',true);
  window.publishAccountsChanged();onLogin?.(result.account);await refreshPool();
}
if (isManager) {
  byId('login-captcha-cancel').addEventListener('click',()=>{
    const attempt=smsAttempts.get(smsKey());
    if(working||!attempt)return;
    attempt.paused=true;clearLoginCaptcha();smsCountdown();byId('manager-login-button').focus();
  });
  window.addEventListener('message',event=>{
    const current=loginCaptcha,attempt=smsAttempts.get(smsKey());
    if(!current||current.token||current.key!==smsKey()||current.requestId!==attempt?.requestId
        ||current.challengeId!==attempt?.challenge?.id||attempt.captchaConsumed
        ||event.source!==current.frame.contentWindow||event.origin!==location.origin
        ||event.data?.type!=='app-login-captcha-success'||event.data.challenge_id!==current.challengeId
        ||typeof event.data.token!=='string'||!event.data.token||event.data.token.length>4096)return;
    current.token=event.data.token;smsCountdown();
  });
  byId('cancel-relogin').addEventListener('click', () => {
    const id=reloginId;setRelogin();portalFeedback('');onReloginCancel?.(id);
  });
  byId('phone').addEventListener('input', () => {
    byId('code').value='';smsCountdown();
  });
  byId('refresh-accounts').addEventListener('click', () => refreshPool().catch(error => portalFeedback(error.message)));
  byId('send-sms').addEventListener('click', () => {
    if (!reloginId && !byId('phone').reportValidity()) return;
    managerAction(() => sendManagerSms(Boolean(smsAttempts.get(smsKey())?.expiresAt > Date.now())));
  });
  byId('manager-login-form').addEventListener('submit', event => {
    event.preventDefault(); const attempt=smsAttempts.get(smsKey());
    if (!(attempt?.expiresAt > Date.now())) { portalFeedback('请先获取当前账号的验证码。'); return; }
    if(attempt.sliderRequired){attempt.paused=false;smsCountdown();return;}
    managerAction(()=>submitManagerLogin());
  });
  setInterval(smsCountdown, 1000);
  smsCountdown();
}
function reloadWhenIdle() {
  if (embedded && !root.closest('dialog').open || working || root.contains(document.activeElement) && document.activeElement.closest('.managed-account')) return;
  refreshPool().catch(error => portalFeedback(error.message));
}
window.addEventListener('accounts-changed', reloadWhenIdle);
window.addEventListener('focus', reloadWhenIdle);
refreshPool().catch(error => portalFeedback(error.message));
setInterval(() => { if (!document.hidden) reloadWhenIdle(); }, 10000);
return {refresh: reloadWhenIdle, focus: async (id, message) => {
  focusedLoginId = id; await refreshPool();
  const row = [...root.querySelectorAll('.managed-account')].find(item => item.dataset.loginId === id);
  if (!row) { portalFeedback('该账号已退出，请刷新账号列表。'); return; }
  row.tabIndex = -1; row.scrollIntoView({block:'center', behavior:'smooth'}); row.focus({preventScroll:true});
  if (message) portalFeedback(message);
}, relogin: id => managerAction(async () => {
  await refreshPool();
  const record = poolRecords.find(item => item.id === id);
  if (!record) { portalFeedback('该账号已退出，请刷新账号列表。'); return; }
  await reloginAccount(record);
})};
}
