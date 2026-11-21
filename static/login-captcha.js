(() => {
  'use strict';
  const root=location.origin+'/login-verification/mgt/';
  const challengeId=new URLSearchParams(location.search).get('challenge_id');
  const status=document.getElementById('status'),verify=document.getElementById('verify'),retry=document.getElementById('retry');
  let finished=false,failed=false;
  retry.addEventListener('click',()=>location.reload());
  function fail(message='官方验证加载失败，请手动重新加载。'){
    if(finished||failed)return;
    failed=true;verify.replaceChildren();status.textContent=message;retry.hidden=false;
  }
  if(!challengeId){fail('登录验证已失效，请返回当前账号重新获取验证码。');retry.hidden=true;return;}
  const script=document.createElement('script');
  script.src=root+'decision-captcha/zw-captcha/index.js';script.onerror=()=>fail();
  script.onload=()=>{
    if(failed||finished)return;
    status.textContent='请手动拖动官方滑块，通过后将继续本次登录。';
    if(!window._zw?.Captcha){fail();return;}
    window._zw.Captcha(verify,{
      appId:'02aecbb71abe2113e42a602056797e09',language:'cn',isSaaS:false,showVoice:false,
      apiServer:root+'decision-captcha',constIDServer:root+'decision-engine/udid/m1',
      constID_js:root+'decision-captcha/zw-captcha/libs/const-id.js',
      ua_js:root+'decision-captcha/zw-captcha/libs/greenseer.js',
      path:root+'decision-captcha/zw-captcha/',style:'embed',width:Math.min(300,document.documentElement.clientWidth-16),
      success(token){
        if(finished||failed)return;
        if(typeof token!=='string'||!token||token.length>4096){fail('官方未返回有效验证结果，请重新加载验证。');return;}
        finished=true;status.textContent='验证通过，正在核对登录结果…';
        parent.postMessage({type:'app-login-captcha-success',challenge_id:challengeId,token},location.origin);
      },
      fail(){fail('验证未通过，请手动重新加载后再试。');},failSafe:()=>fail(),
    }).on('loadFail',()=>fail());
  };
  document.head.append(script);
})();
