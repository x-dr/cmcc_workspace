"""Shared China Mobile activity authentication and request transport."""
from __future__ import annotations

import base64
import json
import re
import secrets
import time
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app_login import AppCredentialError, AppLoginError, AppLoginSession
from http_transport import requests
from recharge.cmcc import APP_UA
from star_day_auth import encrypt_uid
from .coc_environment import collect

ORIGIN = 'https://dev.coc.10086.cn'
API = ORIGIN + '/coc/'
# Preserve the stored scope for the COC login already shared with seven-day.
COC_SCOPE = 'seven-day'
APP_SESSION_REJECTION_CODES = {'400007', '400009', '410000', '420000'}

PUBLIC_KEY = 'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA0a66eNBd/ioaGnEM2AV+ctQX8VYt6dszzI8zOv26Kc1A7ex/cu/txmbB6WrU5ARoTeS0gk3KS21e/xP1BR1FUqL/+YJ4v8gpFY21/2gWuEPVBoHEd34FMpDRuiQkpmrvuuRF+QJQIg5ASEBpwz4n15ltl7Fd4E+NLp/JRJxzNR55ZeVLLFaucWYe8FIvX5BKx8i+sJWZYDoxTbjrBTy2CZDZObT+PxhOBXsCkolsG8Ux+tIAoU+UAVSQ0qT62z/sHPVPngHAqmLP/sF3IM6Mrub7BqhwJe3rvddqrw+Q2Jg8upZ/oUlZvXtQZCxJXtg0eJVdKTmnE6hD+/M99ukEoQIDAQAB'

class CocError(Exception):
    def __init__(self, message, code='COC_ERROR', status=502, *, uncertain=False):
        super().__init__(message)
        self.code, self.status, self.uncertain = code, status, uncertain


def _aes_encrypt(value, key):
    pad = padding.PKCS7(128).padder()
    plain = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
    cipher = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return base64.b64encode(cipher.update(pad.update(plain) + pad.finalize()) + cipher.finalize()).decode()


def _encrypted(value, kind):
    key = secrets.token_hex(8).encode()
    public = serialization.load_der_public_key(base64.b64decode(PUBLIC_KEY))
    content = _aes_encrypt(value, key)
    if kind == 'meteor':
        header = {'coc-meteor': base64.b64encode(public.encrypt(base64.b64encode(key), rsa_padding.PKCS1v15())).decode(), 'coc-meteor-v': '2'}
        content = base64.b64encode(content.encode()).decode()
    else:
        header = {'Coc-En-Version': '1486234121417629696', 'coc-aurora-v': '2',
                  'coc-aurora': base64.b64encode(public.encrypt(key, rsa_padding.PKCS1v15())).decode()}
    return {'cocEnContent': content}, header, key


def _decode(response, key=None):
    if not isinstance(response, dict):
        raise CocError('官方响应格式异常', 'UPSTREAM_FORMAT')
    if key is not None and response.get('returnData'):
        cipher = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        unpad = padding.PKCS7(128).unpadder()
        raw = cipher.update(base64.b64decode(response['returnData'])) + cipher.finalize()
        response = json.loads((unpad.update(raw) + unpad.finalize()).decode())
    if str(response.get('code')) != '0':
        code = str(response.get('code', 'UNKNOWN'))[:60]
        message = str(response.get('message') or '官方暂未完成此操作')[:300]
        raise CocError(message + '（' + code + '）', code, 401 if code == '40' else 502)
    return response.get('data')


class CocClient:
    def __init__(self, credentials_provider, *, entry, session_factory=None, credential_cache=None,
                 app_session_recover=None, sdk_encrypt=None):
        self.entry = entry
        query = parse_qs(urlsplit(entry).query)
        self.page_id = query.get('pageId', [''])[0]
        self.channel = query.get('channelId', [''])[0]
        self.sdk_encrypt = sdk_encrypt
        self.credentials_provider = credentials_provider
        self.session_factory = session_factory or (lambda: requests.Session(impersonate='chrome'))
        self.credential_cache = credential_cache
        self.app_session_recover = app_session_recover
        self.identity_recovered = False
        self.client = None
        self.snapshot = None
        self.user = {}

    def current(self):
        latest = self.credentials_provider()
        if (latest is None or latest.get('phone') != self.snapshot.get('phone')
                or self.credential_cache is None and latest.get('cookie') != self.snapshot['cookie']):
            raise CocError('共享账号登录已更新，请重新获取本次验证', 'APP_CREDENTIALS_CHANGED', 409)

    def connect(self):
        snapshot = self.credentials_provider()
        if snapshot is None or not re.fullmatch(r'1[3-9]\d{9}', str(snapshot.get('phone', ''))):
            raise CocError('共享账号需要重新登录，请在账号管理恢复登录', 'APP_LOGIN_REQUIRED', 401)
        self.snapshot = snapshot
        self.client = self.session_factory()
        self.client.headers.update({'User-Agent': APP_UA, 'Origin': ORIGIN, 'Referer': self.entry,
                                    'Accept-Language': 'zh-CN,zh;q=0.9'})
        if self.credential_cache is None:
            self._authenticate()
        else:
            data = self.credential_cache.connect(COC_SCOPE,snapshot,self.client,
                self._authenticate)
            self.user = data['user']
        return self

    def _authenticate(self):
        # Cached-session recovery must read today's primary identity, rather
        # than the snapshot captured when the expired activity cookie was made.
        snapshot = self.credentials_provider()
        if snapshot is None or snapshot['phone'] != self.snapshot['phone']:
            raise CocError('共享账号已退出或切换，请重新选择账号', 'APP_LOGIN_REQUIRED', 401)
        uid = re.search(r'(?:^|;)\s*UID=([^;]+)', snapshot.get('cookie', ''), re.I)
        if uid is None or not re.search(r'(?:^|;)\s*JSESSIONID=', snapshot.get('cookie',''), re.I):
            raise CocError('共享账号缺少活动身份，请重新登录', 'APP_LOGIN_REQUIRED', 401)
        uid = uid[1]
        self.snapshot = snapshot
        try:
            flow = AppLoginSession.from_credentials(snapshot)
            _, result, _ = flow.call('/biz-orange/LN/checkUIDAction/checkUID', {
                'channelId': '12093', 'domain': ORIGIN, 'url': self.entry,
                'tel': snapshot['phone'], 'uId': uid,
            })
        except AppLoginError as error:
            raise CocError('APP 活动认证暂未完成，请校验并重连', 'APP_UID_FAILED') from error
        code = str(result.get('retCode'))
        if code in APP_SESSION_REJECTION_CODES:
            return self._recover_app_identity(snapshot, code, 'APP_LOGIN_REQUIRED')
        if code != '000000':
            raise CocError('APP 活动认证暂未完成（'+code[:40]+'）', 'APP_UID_FAILED')
        self.current()
        try:
            self.user = self.post('user/loginCheck', {'uid': encrypt_uid(uid), 'verifyType': 1}, encryption='aurora')
        except CocError as error:
            # These observed loginCheck rejections are not permission to
            # replay a business request carrying the same numeric code.
            if error.code not in ('1','30'):
                raise
            return self._recover_app_identity(snapshot, error.code, 'ACTIVITY_LOGIN_REQUIRED')
        return {'user':self.user}

    def _recover_app_identity(self, snapshot, reason, code):
        if self.app_session_recover is None or self.identity_recovered:
            raise CocError(f'活动登录未通过（{reason}），请重新登录此共享账号后查询。', code, 401)
        self.identity_recovered = True
        try:
            self.app_session_recover(snapshot)
        except AppLoginError as renewal:
            required = isinstance(renewal, AppCredentialError) and renewal.login_required
            raise CocError(str(renewal), 'APP_LOGIN_REQUIRED' if required else 'APP_REFRESH_FAILED',
                           401 if required else 502) from renewal
        self.client.cookies.clear()
        return CocClient._authenticate(self)

    def close(self):
        if self.client is not None:
            self.client.close()
            self.client = None

    def environment(self):
        # These are authenticated activity context values, not invented browser
        # hardware or automation-detection values. The host remains Python.
        return {**collect(), 'wasm_ver': '0.0.3',
                'page_url': self.entry, 'hostname': 'dev.coc.10086.cn',
                'protocol': 'https:', 'port': '', 'referer': self.entry, 'events': '',
                'user_agent': self.client.headers['User-Agent'], 'language': 'zh-CN',
                'languages': ['zh-CN'], 'on_line': True, 'cookie_enabled': True,
                'main_script': '', 'collection_time': int(time.time()*1000),
                'local_storage': {'benefit_phone': self.snapshot['phone'], 'pageId': self.page_id,
                                  'channelId': self.channel, 'userId': self.user.get('userId', '')},
                'cookie': {cookie.name: cookie.value for cookie in self.client.cookies.jar},
                'tls_fingerprint': {'has_subtle_crypto': False, 'is_secure_context': True,
                                    'protocol': 'https:', 'user_agent_contains_ssl': 'ssl' in APP_UA.lower()}}

    def wheel_request(self, payload):
        from .coc_wheel import encrypt, SevenDayCryptoError
        def transport(body, headers):
            self.current()
            try:
                result = self.client.post(API+'wasmrc/gDT', json=body, headers=headers,
                                          timeout=(8, 20), allow_redirects=False)
            except requests.RequestsError as error:
                raise SevenDayCryptoError('官方设备令牌服务连接失败，本次未提交业务请求。', 'DEVICE_TOKEN_NETWORK') from error
            if result.status_code != 200:
                raise SevenDayCryptoError('官方设备令牌服务 HTTP '+str(result.status_code), 'DEVICE_TOKEN_HTTP')
            try:
                return result.json()
            except ValueError as error:
                raise SevenDayCryptoError('官方设备令牌响应格式异常', 'DEVICE_TOKEN_FORMAT') from error
        try:
            encrypted = (self.sdk_encrypt or encrypt)(payload, [], APP_UA, transport=transport, environment=self.environment)
        except SevenDayCryptoError as error:
            raise CocError(str(error), error.code, error.status) from error
        return encrypted['data'], encrypted['headers']

    def post(self, path, payload, *, encryption=None, mutation=False, before_submit=None, recover=True):
        key, headers, body = None, {}, payload
        if encryption in ('aurora', 'meteor'):
            body, headers, key = _encrypted(payload, encryption)
        elif encryption == 'wheel':
            body, headers = self.wheel_request(payload)
        if mutation:
            self.current()
            if before_submit is not None:
                before_submit()
        try:
            response = self.client.post(API + path, json=body, headers=headers, timeout=(8, 20), allow_redirects=False)
        except requests.RequestsError as error:
            raise CocError('官方连接中断，将先核实结果后再决定后续操作', 'UPSTREAM_NETWORK', uncertain=mutation) from error
        if response.status_code not in (200,401):
            raise CocError('官方接口 HTTP ' + str(response.status_code), 'UPSTREAM_HTTP', uncertain=mutation)
        try:
            value = {'code':'40'} if response.status_code == 401 else response.json()
            return _decode(value, key if encryption == 'meteor' else None)
        except CocError as error:
            if error.code in ('40', '1000004000') and self.credential_cache is not None and getattr(self.client,'_credential_binding',None):
                if recover and not mutation and path != 'user/loginCheck' and self.credential_cache.recover(self.client):
                    self.user = self.client._credential_data['user']
                    return self.post(path,payload,encryption=encryption)
                self.credential_cache.invalidate(self.client)
            raise
        except (ValueError, UnicodeError) as error:
            raise CocError('官方返回了非预期内容，将先查询结果', 'UPSTREAM_FORMAT', uncertain=mutation) from error
