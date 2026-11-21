"""China Mobile membership eligibility and user-confirmed claims."""
from base64 import b64encode
from contextlib import contextmanager
from copy import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import md5, sha256
from html import unescape
import json
import os
import re
import secrets
from threading import RLock
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from flask import jsonify, render_template, request

from app_login import AppLoginError, AppLoginSession
from http_transport import RequestException, requests
from member_benefits_protocol import CampusProtocolError, CampusRiskProtocol
from monthly_coupon_auth import APP_TOKEN_PATH
from recharge.cmcc import APP_UA


ORIGIN = 'https://dev.coc.10086.cn'
ENTRY = ORIGIN + '/coc3/canvas/rightsmarket-h5-canvas/online/wdqy?channelCode=P00000127028'
STARBUCKS = ORIGIN + '/coc3/canvas/rightsmarket-h5-canvas/online/xbkqy?channelCode=P00000111444'
STARBUCKS_COUPONS = ORIGIN + '/coc3/canvas/rightsget-h5-canvas/online/xbklhhyst?sourceid=056002'
STARBUCKS_COUPON_BUSINESS_TYPE = '1335'
STARBUCKS_COUPON_RULE = '738964678668390400'
STARBUCKS_SOURCE_ID = '056002'
STARBUCKS_CLAIM_SCOPE = 'national-rights-market:starbucks-claim'
STARBUCKS_CLAIMS = 'starbucks-claims'
MEMBER_AVAILABILITY = 'member-benefits-availability'
STARBUCKS_SMS_COOLDOWN = 60
STARBUCKS_STATUS_LABELS = {True: '已领取', False: '未领取', None: '领取状态查询失败'}
STARBUCKS_SIGNATURE_SALT = '7C9408E2E22ED1329A72837709851954'
STARBUCKS_ORDER_KEY = b'CHINAMOBILENEWGENERATIONCUSTOMER'
# The published xbkqy page uses this activity and SKU for its separate data package.
STARBUCKS_DATA_ACTIVITY = '15426'
STARBUCKS_DATA_SKU = '202'
CAMPUS_MEMBER = '1055'
CAMPUS_ACTIVITY = '12502'
CAMPUS_CHANNEL = 'P00000011939'
CAMPUS_PAGE = 'zeromzone'
CAMPUS_ENTRY = ORIGIN + '/coc3/canvas/rightsmarket-h5-canvas/online/' + CAMPUS_PAGE + '?channelCode=' + CAMPUS_CHANNEL
CAMPUS_SMS = '/coc3/coc3-market/api/sms/qwSendSmsCode'
CAMPUS_ORDER = '/coc3/coc3-market-order/api/external/order/placeOrder.do'
CAMPUS_CLAIMS = 'member-campus-claims'
CAMPUS_SMS_COOLDOWN = 60
PLATINUM_MEMBER = '1'
PLATINUM_ACTIVITY = '10164'
PLATINUM_PAGE = 'zero'
PLATINUM_ENTRY = ORIGIN + '/coc3/canvas/rightsmarket-h5-canvas/online/' + PLATINUM_PAGE + '?memberId=' + PLATINUM_MEMBER
PLATINUM_STRATEGY_GROUP = 'UC000202505120075'  # Official zero page's CardVoucher3column policy.
GUIZHOU_PLATINUM_MEMBER = '1447'
GUIZHOU_SUPERMARKET_GIFT_BAG = ('1452', '17230')
GUIZHOU_GIFT_BAG_HINT = '请去黔心选小程序领取该权益'
DISCOUNT_CLAIMS = 'member-discount-claims'
DISCOUNT_SMS_COOLDOWN = 60
DISCOUNT_STRATEGY_GROUP = 'UC000202505120076'  # Official halfprize page's CardVoucher3column policy.
AUTH = '/coc3/gr/login/ssoRights/userLoginV2'
CATALOG = '/coc3/coc3-market-activity/api/activity/getActivityGroupAndProductInfo'
ACTIVITY_OFFERS = '/coc3/coc3-market-activity/arrange/getProductByActivityId'
GROUP_OFFERS = '/coc3/coc3-market/arrange/getProductByGroupIdInTrimester'
ELIGIBILITY = '/coc3/coc3-market-activity/arrange/checkQualificByActivityId/v3'
PRODUCT_DETAIL = '/coc3/coc3-market/arrange/getProductDetailById'
PRODUCTS = '/coc3/gateway-route/gr-rights/rights/market/user/'
SUIXIN_PORTAL = ORIGIN + '/coc3/gr/static-2c/rightsget/'
SUIXIN_PUBLIC = '/coc3/gr/rights/'
SESSION_SCOPE = 'national-rights-market:member-benefits'
SUIXIN_SCOPE = 'national-rights-market:suixin'
SUIXIN_BUSINESS_TYPE = '8'
SUIXIN_ENTRY = SUIXIN_PORTAL + '#/rightsbaw?sourceid=056002'
SUIXIN_ORDER = SUIXIN_PUBLIC + 'orderRightsByEncrypt'
SUIXIN_VERIFICATION_SMS = SUIXIN_PUBLIC + 'sms/send/sms/by/token'
SUIXIN_CLAIMS = 'member-suixin-claims'
ANDROID_ORDER_NOTICE = '安卓版暂未接入该权益的订单验证，请在电脑版工具台处理。'
PUBLIC_PARAMETERS = {'channelCode', 'channelId', 'memberId', 'activityId', 'pageId',
                     'skuId', 'goodsId', 'productId', 'groupId', 'sellerId'}


class MemberBenefitsError(Exception):
    def __init__(self, message, code, status=502, *, verification_url=None, resume_available=False, verification_sms=False):
        super().__init__(message)
        self.code, self.status = code, status
        self.verification_url = verification_url
        self.resume_available = resume_available
        self.verification_sms = verification_sms


@dataclass(frozen=True)
class CampusPreparation:
    phone: str
    member_id: str
    sku_id: str
    period: str
    agreement: str
    binding: str


@dataclass(frozen=True)
class DiscountPreparation:
    phone: str
    member_id: str
    benefit_key: str
    activity_id: str
    sku_id: str
    period: str
    price_cents: int
    agreement: str
    page_url: str


def _official_link(value, fallback):
    if not isinstance(value, str) or not value:
        return fallback
    try:
        parts = urlsplit(urljoin(ORIGIN, value))
    except ValueError:
        return fallback
    if (parts.scheme != 'https' or not (parts.hostname or '').endswith('.10086.cn')
            or parts.netloc != parts.hostname or parts.path == '/activity/transit/transferDownload.html'):
        return fallback
    params = [(key, value) for key, value in parse_qsl(parts.query) if key in PUBLIC_PARAMETERS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), ''))


def _public_id(value):
    candidate = str(value or '')
    return candidate if re.fullmatch(r'[A-Za-z0-9_-]{1,80}', candidate) else ''


def _market_verification_url(value):
    if not isinstance(value, str) or re.search(r'[\x00-\x20\x7f\\]', value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    routes = {'/voiceCaptcha', '/smsCaptcha', '/imgCaptcha', '/wordCaptcha', '/idiomCaptcha',
              '/identityCaptcha', '/faceVerify', '/riskVerify', '/puzzleVerify'}
    if ((parts.scheme, parts.netloc, parts.path) == ('https', 'dev.coc.10086.cn', '/coc3/gr/static-2c/risk-verify/')
            and parts.fragment.partition('?')[0] in routes):
        return value
    return None


def _count(value):
    text = str(value)
    return int(text) if re.fullmatch(r'[0-9]{1,8}', text) else None


def _product_agreement(detail):
    content = detail.get('serviceAgreementContent') or detail.get('priceExplain') or ''
    return ' '.join(unescape(re.sub(r'<[^>]+>', ' ', str(content))).split())


def _starbucks_signed_headers(token, *, nonce=None):
    timestamp = str(time.time_ns() // 1_000_000)
    nonce = secrets.token_hex(8) if nonce is None else nonce
    signature = md5(f'{timestamp}{STARBUCKS_SIGNATURE_SALT}{nonce}'.encode('ascii')).hexdigest()
    return {
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json;charset=UTF-8',
        'innerToken': token,
        'token': token,
        'timestamp': timestamp,
        'randomstr': nonce,
        'signature': signature,
    }


def _rights_ciphertext(payload):
    body = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    padder = padding.PKCS7(128).padder()
    encryptor = Cipher(algorithms.AES(STARBUCKS_ORDER_KEY), modes.ECB()).encryptor()
    return b64encode(encryptor.update(padder.update(body) + padder.finalize()) + encryptor.finalize()).decode('ascii')


def _market_encrypt(value, key):
    body = value.encode('utf-8')
    cipher = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
    return b64encode(cipher.update(body + b'\0' * (-len(body) % 16)) + cipher.finalize()).decode('ascii')


def _list_members(data, *, include_source=False):
    rows = data.get('vipInfoList')
    if not isinstance(rows, list):
        raise MemberBenefitsError('官方没有返回会员清单，请稍后刷新。', 'MEMBER_FORMAT')
    members = []
    for member in rows:
        if not isinstance(member, dict):
            continue
        member_id = _public_id(member.get('memberId'))
        if not member_id:
            continue
        member_link = ENTRY + '&' + urlencode({'memberId': member_id})
        benefits = []
        for item in member.get('activityInfoList') or []:
            name = str(item.get('activityShowName') or '')
            if '星巴克礼包' in name:
                kind, fallback = '礼包', STARBUCKS
            elif '福袋' in name:
                kind, fallback = '福袋', member_link
            elif re.search(r'[五5]折购', name):
                kind, fallback = '五折购', member_link
            else:
                continue
            activity_id = _public_id(item.get('activityId'))
            campus = member_id == CAMPUS_MEMBER and activity_id == CAMPUS_ACTIVITY and kind == '福袋'
            platinum = member_id == PLATINUM_MEMBER and activity_id == PLATINUM_ACTIVITY and kind == '福袋'
            folder_id = _public_id(item.get('folderId'))
            key = sha256(f'{activity_id}|{folder_id}|{name}'.encode()).hexdigest()[:24]
            source = member.get('skuSimpleVoList' if folder_id else 'activityProductList') or []
            offers = []
            if activity_id:
                for offer in member.get('activityProductList') or []:
                    if _public_id(offer.get('aid')) != activity_id:
                        continue
                    title = str(offer.get('showName') or offer.get('name') or '')
                    price = _count(offer.get('price'))
                    if title:
                        offers.append({'name': title[:120], 'price_cents': price})
            benefit = {
                'key': key, 'can_inspect': kind == '礼包' or bool(activity_id or folder_id or offers),
                'name': name[:120], 'kind': kind, 'remaining': _count(item.get('monthRemainTime')),
                'claimed': str(item.get('status')) == '2', 'offers': offers[:12],
                'url': CAMPUS_ENTRY + '&memberId=' + CAMPUS_MEMBER if campus else _official_link(item.get('activityUrl'), fallback),
            }
            if campus or platinum:
                benefit['claim_flow'] = 'campus'
            if kind == '福袋' and (member_id == GUIZHOU_PLATINUM_MEMBER
                                  or (member_id, activity_id) == GUIZHOU_SUPERMARKET_GIFT_BAG):
                benefit['can_inspect'] = False
                benefit['claim_hint'] = GUIZHOU_GIFT_BAG_HINT
            if include_source:
                benefit['_source'] = {
                    'activity_id': activity_id, 'folder_id': folder_id,
                    'mark_type': str(item.get('markType', '')),
                    'ordered_ids': tuple(dict.fromkeys(sku for value in item.get('orderSkuList') or []
                                                       if (sku := _public_id(value)).isdigit())),
                    'status': str(item.get('status')),
                    'offers': [offer for offer in source
                               if activity_id and _public_id(offer.get('aid')) == activity_id],
                }
            benefits.append(benefit)
        members.append({
            'id': member_id, 'name': str(member.get('vipName') or member.get('showName') or '会员')[:120],
            'benefits': benefits, 'url': member_link,
        })
    return members


class MemberBenefits:
    def __init__(self, credentials, cache, *, session_factory=None, app_factory=AppLoginSession.from_credentials,
                 recover=None, campus_protocol=None):
        self.credentials, self.cache = credentials, cache
        self.session_factory = session_factory or (lambda: requests.Session(impersonate='chrome', trust_env=False))
        self.app_factory = app_factory
        self.recover = recover
        self.campus_protocol = campus_protocol or CampusRiskProtocol(cache.storage)
        self.claim_lock = RLock()
        self.claims = {}
        self.claim_storage = cache.storage
        self.claim_results = self.claim_storage.load_activity_data(STARBUCKS_CLAIMS) if self.claim_storage else None
        self.claim_results = self.claim_results or {}
        self.suixin_operations = {}
        self.suixin_results = (self.claim_storage.load_activity_data(SUIXIN_CLAIMS) or {}) if self.claim_storage else {}
        self.campus_operations = {}
        self.campus_prepared = {}
        self.campus_results = (self.claim_storage.load_activity_data(CAMPUS_CLAIMS) or {}) if self.claim_storage else {}
        self.discount_operations = {}
        self.discount_prepared = {}
        self.discount_results = (self.claim_storage.load_activity_data(DISCOUNT_CLAIMS) or {}) if self.claim_storage else {}
        self.availability = (self.claim_storage.load_activity_data(MEMBER_AVAILABILITY) or {}) if self.claim_storage else {}

    def _request(self, session, method, path, **kwargs):
        try:
            response = session.request(method, ORIGIN + path, timeout=(7, 18), allow_redirects=False, **kwargs)
        except RequestException as error:
            raise MemberBenefitsError('暂时无法连接官方会员权益。', 'MEMBER_NETWORK') from error
        if response.status_code != 200:
            return self._official_result(path, response.status_code, None)
        try:
            value = response.json()
        except ValueError as error:
            raise MemberBenefitsError('官方会员权益响应格式有误。', 'MEMBER_FORMAT') from error
        return self._official_result(path, response.status_code, value)

    def _official_result(self, path, status_code, value):
        market_order = path == CAMPUS_ORDER
        if status_code == 429:
            raise MemberBenefitsError('官方请求频繁，请稍后再试。', 'MEMBER_RATE_LIMIT', 429)
        if path in (CAMPUS_ORDER, SUIXIN_ORDER) and 300 <= status_code < 400:
            raise MemberBenefitsError('领取提交后收到跳转，官方结果待核对。', 'MEMBER_ORDER_REDIRECT', 502)
        if status_code == 401 or 300 <= status_code < 400:
            raise MemberBenefitsError('会员权益会话已失效。', 'MEMBER_SESSION_EXPIRED', 401)
        if status_code != 200 and not (market_order and 200 < status_code < 300):
            raise MemberBenefitsError('官方会员权益暂时未返回数据。', 'MEMBER_HTTP')
        if market_order and status_code == 200 and value is None:
            return None
        if not isinstance(value, dict):
            raise MemberBenefitsError('官方会员权益响应格式有误。', 'MEMBER_FORMAT')
        code = (str(value.get('resultCode', value.get('code', ''))) if market_order else
                str(value.get('code', value.get('resultCode', ''))))
        data = value.get('data')
        order_request = path in (SUIXIN_ORDER, SUIXIN_PUBLIC + 'orderRightsWithSmsByEncrypt', CAMPUS_ORDER)
        sms_request = path.endswith('/sms/sendSmsCode') or path in (CAMPUS_SMS, SUIXIN_VERIFICATION_SMS)
        challenge = isinstance(data, dict) and data.get('risk')
        verification_url = _market_verification_url(data.get('url')) if market_order and challenge else None
        if market_order and (value.get('explain') == 'SCORE_REFUSE' or
                             isinstance(data, dict) and data.get('explain') == 'SCORE_REFUSE'):
            message = '官方设备校验未通过，本次领取未获确认。'
            if '频繁' in str(value.get('msg') or value.get('message') or ''):
                message += '官方提示访问过于频繁，请稍后再试。'
            reason = _public_id(code)
            if reason:
                message += f'（{reason}）'
            raise MemberBenefitsError(message, 'MEMBER_CAMPUS_DEVICE_REFUSED', 409, verification_url=verification_url)
        if path == SUIXIN_ORDER and (challenge or code in ('10', '40003', '40013')):
            fields = data if isinstance(data, dict) else {}
            risk_info = fields.get('riskInfo')
            url = _market_verification_url(fields.get('url'))
            if not url and isinstance(risk_info, dict):
                url = _market_verification_url(risk_info.get('riskUrl'))
            needs_sms = not url and code in ('40003', '40013')
            message = '官方要求进行额外的 6 位短信验证。' if needs_sms else (
                '官方要求进一步验证，本次领取尚未完成。' if url else '官方要求进一步验证，但未提供可打开的验证链接。')
            error = MemberBenefitsError(message, 'MEMBER_VERIFICATION', 409, verification_url=url,
                                        verification_sms=needs_sms)
            error.business_unique_id = _public_id(fields.get('businessUniqueId'))
            raise error
        if code == '10' or (order_request and (code in ('40003', '40013') or challenge)):
            message = '官方要求验证，请在中国移动 APP 处理。'
            if path == CAMPUS_ORDER:
                reason = re.sub(r'[^A-Za-z0-9_-]', '', code)[:24]
                if isinstance(data, dict) and data.get('explain') == 'ORDER_REFUSE':
                    message = '官方拒绝领取，本月订购次数可能已达上限。请刷新核对领取记录。'
                    verification_url = None
                else:
                    message = ('官方要求进一步验证，本次领取未获确认。' if verification_url else
                               '官方未返回可打开的验证入口，本次领取未获确认。')
                if reason:
                    message += f'（{reason}）'
            raise MemberBenefitsError(message, 'MEMBER_VERIFICATION', 409, verification_url=verification_url)
        if code == '100026' and path == SUIXIN_PUBLIC + 'checkTokenAndBusinessType':
            raise MemberBenefitsError('官方未确认此账号已开通对应权益。', 'MEMBER_NOT_ELIGIBLE', 409)
        if code in ('40006', '-516', '926') or (code == '100002' and path.startswith((PRODUCTS, SUIXIN_PUBLIC))):
            raise MemberBenefitsError('官方会员权益会话已失效。', 'MEMBER_SESSION_EXPIRED', 401)
        if code in ('999', '-800', '429') or (order_request and any(
                '请求频繁' in str(value.get(field) or '') or '访问过于频繁' in str(value.get(field) or '')
                for field in ('message', 'msg'))):
            raise MemberBenefitsError('官方请求频繁，请稍后再试。', 'MEMBER_RATE_LIMIT', 429)
        if path in (CAMPUS_ORDER, SUIXIN_ORDER) and code == '-1006':
            raise MemberBenefitsError('验证码错误，请核对短信后重新输入。', 'MEMBER_SMS_INVALID', 409)
        if sms_request and code == '40001':
            raise MemberBenefitsError('官方拒绝发送验证码（40001，请求参数错误）。本次尚未进入短信验证。',
                                      'MEMBER_SMS_BAD_REQUEST', 409)
        if market_order and code == '0' and 'resultCode' not in value:
            raise MemberBenefitsError('官方订单回执缺少有效结果码。', 'MEMBER_FORMAT')
        if code and code != '0' and (order_request or sms_request):
            public_code = _public_id(code)
            suffix = f'（{public_code}）' if public_code else ''
            reason = re.sub(r'[^\u4e00-\u9fff，。！？、：；（） ]', '', str(value.get('message') or value.get('msg') or ''))[:80]
            message = f'官方未通过本次请求{suffix}。'
            if reason:
                message += reason
            raise MemberBenefitsError(message, 'MEMBER_CLAIM_REJECTED', 409)
        if code not in ('0', '200'):
            raise MemberBenefitsError('官方暂未返回会员权益，请稍后查询。', 'MEMBER_UPSTREAM')
        return data

    def _login(self, session, snapshot, *, business_type='RIGHTS_MARKET', token_header='phone', starbucks_claim=False):
        cookie = snapshot.get('cookie') or ''
        if not re.search(r'(?:^|;)\s*JSESSIONID=', cookie, re.I):
            raise MemberBenefitsError('请先在账号管理恢复 APP 登录。', 'MEMBER_LOGIN_REQUIRED', 401)
        ticket = cookie
        if starbucks_claim:
            try:
                ticket, cookie = self._starbucks_ticket(snapshot)
            except MemberBenefitsError as error:
                if error.code != 'MEMBER_LOGIN_REQUIRED' or self.recover is None:
                    raise
                try:
                    self.recover(snapshot)
                except AppLoginError as recovery_error:
                    required = getattr(recovery_error, 'login_required', False)
                    code, status = ('MEMBER_LOGIN_REQUIRED', 401) if required else ('MEMBER_APP_TICKET_FAILED', 409)
                    raise MemberBenefitsError(str(recovery_error), code, status) from recovery_error
                updated = self.credentials(snapshot['id'])
                if not updated or updated['phone'] != snapshot['phone']:
                    raise MemberBenefitsError('账号已退出，请重新选择账号。', 'MEMBER_ACCOUNT_NOT_FOUND', 404)
                ticket, cookie = self._starbucks_ticket(updated)
        data = self._request(session, 'POST', AUTH, json={
            'ticket': ticket, 'ticketType': 11, 'businessType': business_type, 'wxoaType': '',
            'm1': '', 'm2': '', 's1': '', 's2': '', 'from': '', 'jsid': cookie,
            **({'smsCode': ''} if business_type == 'NEW_RIGHTS_PORTAL' else {}),
        })
        phone = snapshot['phone']
        if (not isinstance(data, dict) or not isinstance(data.get('token'), str) or not data['token']
                or data.get('mobileMask') != phone[:3] + '****' + phone[-4:]):
            raise MemberBenefitsError('官方认证未确认当前账号身份。', 'MEMBER_ACCOUNT_MISMATCH', 409)
        session.headers[token_header] = data['token']
        return {}

    def _starbucks_ticket(self, snapshot):
        try:
            flow = self.app_factory(snapshot)
            _, result, _ = flow.call(APP_TOKEN_PATH, {
                'sourceId': STARBUCKS_SOURCE_ID, 'url': STARBUCKS_COUPONS, 'cellNum': snapshot['phone'],
            })
        except AppLoginError as error:
            raise MemberBenefitsError('无法连接 APP 权益认证，请稍后再试。', 'MEMBER_APP_TICKET_FAILED') from error
        if str(result.get('retCode')) in ('410000', '420000', '400007', '400009'):
            raise MemberBenefitsError('APP 登录需要恢复，请在账号管理重新登录。', 'MEMBER_LOGIN_REQUIRED', 401)
        if str(result.get('retCode')) != '000000':
            raise MemberBenefitsError('官方未签发星巴克活动凭证。', 'MEMBER_APP_TICKET_FAILED', 409)
        body = result.get('rspBody')
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError as error:
                raise MemberBenefitsError('官方活动凭证格式无法识别。', 'MEMBER_FORMAT') from error
        ticket = body.get('token') if isinstance(body, dict) else None
        if not isinstance(ticket, str) or not ticket:
            raise MemberBenefitsError('官方未签发星巴克活动凭证。', 'MEMBER_APP_TICKET_FAILED', 409)
        return ticket, flow.cookie

    def _products(self, session, *, include_source=False):
        try:
            return self._fetch_products(session, include_source=include_source)
        except MemberBenefitsError as error:
            if error.code != 'MEMBER_SESSION_EXPIRED' or not self.cache.recover(session):
                raise
        return self._fetch_products(session, include_source=include_source)

    def _fetch_products(self, session, *, include_source=False):
        payload = {'token': session.headers['phone'], 'channelId': '100000120'}
        rules = self._request(session, 'POST', PRODUCTS + 'bizInfo/get', json=payload)
        if rules is None:
            return []
        if not isinstance(rules, list):
            raise MemberBenefitsError('未能核对全网随心系。', 'MEMBER_FORMAT')
        types = {str(row['bizType']): row for row in rules if row.get('bizType') is not None}
        if not types:
            return []
        goods = self._request(session, 'POST', PRODUCTS + 'goodsInfo/get',
                              json={**payload, 'bizTypeList': [row['bizType'] for row in types.values()]})
        if goods is None:
            return []
        if not isinstance(goods, list):
            raise MemberBenefitsError('未能核对全网随心系。', 'MEMBER_FORMAT')
        result = []
        for item in goods:
            rule = types.get(str(item.get('bizType')))
            if rule is None or str(item.get('isOrder')) not in ('0', '4'):
                continue
            row = {**item, **rule}
            name = str(row.get('bizName') or '')
            if '全网随心' not in name:
                continue
            remaining = _count(row.get('currentRemainNum')) if str(row['isOrder']) == '0' else None
            if remaining is None:
                status, label = 'unknown', '领取状态未核实'
            elif remaining == 0:
                status, label = 'claimed', '已领取'
            else:
                status, label = 'available', f'剩余 {remaining} 次'
            product = {
                'key': str(item['bizType']), 'name': name[:120],
                'detail': str(row.get('subtitle') or '')[:160],
                'claim_status': status, 'claim_label': label, 'remaining': remaining,
            }
            if include_source:
                link = str(row.get('receiveUrl') or '')
                page, _, fragment = link.partition('#')
                product['_public_catalogue'] = (page.partition('?')[0] == SUIXIN_PORTAL
                                                and fragment.partition('?')[0] == '/rightsbaw')
            result.append(product)
        return result

    @contextmanager
    def _connected(self, login_id, *, rights_entry=None, starbucks_claim=False):
        snapshot = self.credentials(login_id)
        if snapshot is None:
            raise MemberBenefitsError('请选择共享账号池中的账号。', 'MEMBER_ACCOUNT_NOT_FOUND', 404)
        snapshot = {**snapshot, 'id': login_id}
        scope, entry, business_type, token_header = (
            (SUIXIN_SCOPE, rights_entry, 'NEW_RIGHTS_PORTAL', 'innerToken') if rights_entry else
            (SESSION_SCOPE, ENTRY, 'RIGHTS_MARKET', 'phone')
        )
        if starbucks_claim:
            scope = STARBUCKS_CLAIM_SCOPE
        with self.session_factory() as session:
            session.headers.update({'User-Agent': APP_UA, 'Origin': ORIGIN, 'Referer': entry,
                                    'Accept': 'application/json'})
            self.cache.connect(scope, snapshot, session,
                               lambda: self._login(session, snapshot, business_type=business_type,
                                                   token_header=token_header, starbucks_claim=starbucks_claim),
                               headers=(token_header,))
            yield session
            self.cache.sync(session)

    def _read(self, session, path, *, recover=True, referer=None, **params):
        params['t'] = int(time.time())
        headers = {'Referer': referer} if referer else None
        try:
            return self._request(session, 'GET', path, params=params, headers=headers)
        except MemberBenefitsError as error:
            if not recover or error.code != 'MEMBER_SESSION_EXPIRED' or not self.cache.recover(session):
                raise
        return self._request(session, 'GET', path, params=params, headers=headers)

    def _catalog(self, session, *, recover=True):
        data = self._read(session, CATALOG, recover=recover, requestSource=1)
        if not isinstance(data, dict):
            raise MemberBenefitsError('未能读取官方会员清单。', 'MEMBER_FORMAT')
        return data

    def query(self, login_id):
        with self._connected(login_id) as session:
            try:
                products, product_note = self._products(session, include_source=True), ''
            except MemberBenefitsError as error:
                if error.code == 'MEMBER_SESSION_EXPIRED' or 'phone' not in session.headers:
                    raise
                products, product_note = [], str(error)
            # An expired session can return a successful-looking guest catalogue.
            members = _list_members(self._catalog(session), include_source=True)
            claimed_names = {}
            lookup_claimed_names = True
            for member in members:
                for benefit in member['benefits']:
                    if os.environ.get('LINGXI_ANDROID') == '1' and (benefit.get('claim_flow') == 'campus' or benefit['kind'] == '五折购'):
                        benefit['local_order_notice'] = ANDROID_ORDER_NOTICE
                    if (benefit['kind'] != '礼包' and benefit['can_inspect']
                            and not benefit['claimed'] and benefit['remaining'] != 0):
                        try:
                            benefit['selection'] = self._benefit_offers(session, benefit, member['id'])
                        except MemberBenefitsError as error:
                            benefit['selection_error'] = str(error)
                    if benefit.get('claim_flow') == 'campus':
                        with self.claim_lock:
                            pending = self.campus_results.get(self._gift_bag_result_key(login_id, member['id']), {})
                        if pending.get('period') == self._starbucks_period():
                            if pending['sku_id'] in benefit['_source']['ordered_ids']:
                                benefit['claimed'] = True
                            elif not benefit['claimed']:
                                benefit.update(claim_pending=True, status_message='已有领取提交记录，请刷新核对官方状态。')
                    if benefit['kind'] == '五折购':
                        discount_key = self._discount_result_key(
                            login_id, member['id'], benefit['_source']['activity_id'])
                        with self.claim_lock:
                            pending = self.discount_results.get(discount_key, {})
                        if pending.get('period') == self._starbucks_period():
                            if pending['sku_id'] in benefit['_source']['ordered_ids']:
                                benefit['claimed'] = True
                            elif not benefit['claimed']:
                                benefit.update(claim_pending=True,
                                               status_message='已有订购提交记录，请刷新核对官方状态。')
                    source = benefit.pop('_source')
                    if benefit['claimed'] and benefit['kind'] != '礼包':
                        for offer in source['offers']:
                            sku = _public_id(offer.get('skuid'))
                            if sku in source['ordered_ids'] and (name := offer.get('showName') or offer.get('name')):
                                claimed_names[sku] = str(name)[:120]
                        for sku in source['ordered_ids']:
                            if not lookup_claimed_names or sku in claimed_names:
                                continue
                            claimed_names[sku] = ''
                            try:
                                detail = self._read(session, PRODUCT_DETAIL, recover=False, id=sku, isNeedDesc='false')
                            except MemberBenefitsError as error:
                                lookup_claimed_names = error.code not in ('MEMBER_RATE_LIMIT', 'MEMBER_SESSION_EXPIRED')
                            else:
                                if isinstance(detail, dict) and _public_id(detail.get('id')) == sku:
                                    claimed_names[sku] = str(detail.get('showName') or detail.get('name') or '')[:120]
                        benefit['claimed_products'] = list(dict.fromkeys(
                            claimed_names[sku] for sku in source['ordered_ids'] if claimed_names.get(sku)))
                if member['id'] == GUIZHOU_PLATINUM_MEMBER and not any(
                        benefit['kind'] == '福袋' for benefit in member['benefits']):
                    member['benefits'].append({
                        'key': 'guizhou-platinum-gift', 'kind': '福袋', 'name': '权益福袋',
                        'remaining': None, 'claimed': False, 'can_inspect': False, 'offers': [],
                        'claim_hint': GUIZHOU_GIFT_BAG_HINT,
                    })
        starbucks = [benefit for member in members for benefit in member['benefits'] if benefit['kind'] == '礼包']
        if starbucks:
            status = self._starbucks_status(login_id)
            for benefit in starbucks:
                benefit.update(status)
        products = [self._suixin_details(login_id, product) for product in products]
        unclaimed = [benefit for member in members for benefit in member['benefits']
                     if benefit['kind'] not in ('礼包', '五折购') and not benefit['claimed']
                     and not benefit.get('claim_pending')]
        other_count = sum(benefit['remaining'] or 0 for benefit in unclaimed if benefit.get('claim_flow') != 'campus')
        gift_counts = {'campus': 0, 'platinum': 0}
        for member in members:
            field = 'platinum' if member['id'] == PLATINUM_MEMBER else 'campus'
            gift_counts[field] += sum(benefit['remaining'] or 0 for benefit in member['benefits']
                                      if benefit.get('claim_flow') == 'campus' and not benefit['claimed']
                                      and not benefit.get('claim_pending'))
        other_count += sum(product['remaining'] or 0 for product in products if product['claim_status'] == 'available')
        starbucks_count = max((benefit['remaining'] for benefit in starbucks if benefit.get('available')), default=0)
        with self.claim_lock:
            period = self._starbucks_period()
            records = {key: row for key, row in self.availability.items() if row['period'] == period}
            result = self.claim_results.get(login_id, {})
            if result.get('period') == period:
                starbucks_count = 0
            for member_id, field in ((CAMPUS_MEMBER, 'campus'), (PLATINUM_MEMBER, 'platinum')):
                if self.campus_results.get(self._gift_bag_result_key(login_id, member_id), {}).get('period') == period:
                    gift_counts[field] = 0
            records[login_id] = {'period': period, 'other_claims': other_count, 'starbucks': starbucks_count,
                                 **gift_counts}
            self._save_availability(records)
        return {'members': members, 'products': products, 'product_note': product_note}

    def _save_availability(self, records):
        if records == self.availability:
            return
        if self.claim_storage:
            self.claim_storage.save_activity_data(MEMBER_AVAILABILITY, records)
        self.availability = records

    @staticmethod
    def _discount_result_key(login_id, member_id, activity_id):
        return '|'.join((login_id, member_id, activity_id))

    @staticmethod
    def _gift_bag_result_key(login_id, member_id):
        return login_id if member_id == CAMPUS_MEMBER else f'{login_id}|{member_id}'

    def available_counts(self, account_ids):
        period = self._starbucks_period()
        with self.claim_lock:
            # Old 'other' totals include discounts and cannot be separated until the account is queried again.
            return {key: row.get('other_claims', 0) + row['starbucks'] + row.get('campus', 0) + row.get('platinum', 0)
                    for key, row in self.availability.items() if key in account_ids and row['period'] == period}

    def available_count(self, account_ids):
        return sum(self.available_counts(account_ids).values())

    def _account_rights(self, login_id, business_type, *, entry, rule_chain=None):
        with self._connected(login_id, rights_entry=entry) as session:
            return self._read_account_rights(session, business_type, rule_chain)

    def _read_account_rights(self, session, business_type, rule_chain):
        try:
            return self._fetch_account_rights(session, business_type, rule_chain)
        except MemberBenefitsError as error:
            if error.code != 'MEMBER_SESSION_EXPIRED' or not self.cache.recover(session):
                raise
        return self._fetch_account_rights(session, business_type, rule_chain)

    def _fetch_account_rights(self, session, business_type, rule_chain):
        token = session.headers['innerToken']
        params = {'businessType': business_type, 'token': token}
        headers = {'token': token}
        if rule_chain:
            self._request(session, 'POST', SUIXIN_PUBLIC + 'checkTokenAndBusinessType', headers=headers, json=params)
        activities = self._request(session, 'POST', SUIXIN_PUBLIC + 'getActIds', headers=headers, json=params)
        if not isinstance(activities, list):
            raise MemberBenefitsError('官方未返回当前账号的权益活动。', 'MEMBER_FORMAT')
        options = []
        activity_by_id = {activity_id: activity for activity in activities
                          if (activity_id := _public_id(activity.get('actId')))}
        for activity_id, activity in activity_by_id.items():
            catalogue = self._request(session, 'POST', SUIXIN_PUBLIC + 'geRightsList',
                                      headers=headers, json={**params, 'actId': activity_id})
            if rule_chain and str((catalogue or {}).get('ruleChainId')) != rule_chain:
                continue
            for floor in (catalogue or {}).get('floorInfoList') or []:
                remaining = _count(floor.get('currentRemainNum'))
                for offer in floor.get('goodsList') or []:
                    if name := offer.get('goodsName'):
                        price = str(offer.get('discountPrice', ''))
                        order_status = str(offer.get('goodsOrderStat'))
                        option = {
                            'name': str(name)[:120],
                            'detail': str(floor.get('floorName') or activity.get('actName') or '')[:80],
                            'price': price if re.fullmatch(r'[0-9]{1,8}(?:\.[0-9]{1,2})?', price) else None,
                            'remaining': remaining,
                            'claimed': order_status == '2',
                            'available': remaining is not None and remaining > 0 and order_status == '1',
                        }
                        if rule_chain == STARBUCKS_COUPON_RULE or business_type == SUIXIN_BUSINESS_TYPE:
                            if rule_chain == STARBUCKS_COUPON_RULE:
                                option['claimed'] = {'1': False, '2': True}.get(order_status)
                            sku_id = _public_id(offer.get('skuId'))
                            option['_order'] = {
                                'accountType': str(offer.get('accountType') or ('MOBILE' if rule_chain else '')),
                                'goodsName': option['name'],
                                'goodsId': _public_id(offer.get('goodsId')),
                                'skuId': int(sku_id) if sku_id.isdigit() else None,
                                'actId': activity_id,
                                'corGoodsId': _public_id((catalogue or {}).get('corGoodsId') or activity.get('corGoodsId')),
                                'businessType': business_type,
                            }
                        if business_type == SUIXIN_BUSINESS_TYPE:
                            order = option['_order']
                            option['id'] = sha256(json.dumps([order[key] for key in
                                ('actId', 'corGoodsId', 'goodsId', 'skuId')]).encode()).hexdigest()[:24]
                            option['_agreement'] = _product_agreement({'serviceAgreementContent': offer.get('goodsDescrip')})
                        options.append(option)
        return options

    def _starbucks_coupon(self, options):
        return next((option for option in options if re.fullmatch(r'星巴克\s*24\s*元会员礼包', option['name'])), None)

    def _starbucks_status(self, login_id):
        try:
            official = self._starbucks_coupon(self._account_rights(
                login_id, STARBUCKS_COUPON_BUSINESS_TYPE, entry=STARBUCKS_COUPONS, rule_chain=STARBUCKS_COUPON_RULE))
        except MemberBenefitsError as error:
            return {'claimed': None, 'status_label': str(error)}
        if official is None or official['claimed'] is None:
            return {'claimed': None, 'status_label': STARBUCKS_STATUS_LABELS[None]}
        claimed = official['claimed']
        result = self.claim_results.get(login_id, {})
        pending = not claimed and result.get('period') == self._starbucks_period()
        return {
            'claimed': claimed, 'status_label': STARBUCKS_STATUS_LABELS[claimed],
            'claimed_products': [official['name']] if claimed else [],
            'price': official['price'], 'remaining': official['remaining'],
            'available': official['available'] and not pending,
            'can_claim': self._can_claim_starbucks(official) and not pending,
            'submitted': pending and result['state'] == 'submitted',
            'status_message': '本月已有领取提交记录，请刷新查看官方状态。' if pending else '',
        }

    def _can_claim_starbucks(self, offer):
        return bool(offer and offer['available'] and offer['price'] in ('0', '0.0', '0.00')
                    and offer['_order']['accountType'] == 'MOBILE'
                    and all(offer['_order'][key] for key in ('goodsId', 'skuId', 'corGoodsId')))

    def _starbucks_period(self):
        return datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m')

    def _remember_starbucks_result(self, login_id, period, state):
        results = {key: value for key, value in self.claim_results.items() if value['period'] == period}
        if state:
            results[login_id] = {'period': period, 'state': state}
        else:
            results.pop(login_id, None)
        if self.claim_storage:
            self.claim_storage.save_activity_data(STARBUCKS_CLAIMS, results)
        self.claim_results = results

    def starbucks_sms(self, login_id):
        period = self._starbucks_period()
        with self.claim_lock:
            previous = self.claims.get(login_id, {})
            if previous.get('state') in ('sending', 'submitting'):
                raise MemberBenefitsError('当前账号已有一笔领取操作正在处理。', 'MEMBER_CLAIM_BUSY', 409)
            result = self.claim_results.get(login_id, {})
            if result.get('period') == period:
                raise MemberBenefitsError('本月已有领取提交记录，请查看官方领取状态。', 'MEMBER_CLAIM_PENDING', 409)
            if 'sent_at' in previous and previous['sent_at'] + STARBUCKS_SMS_COOLDOWN > time.monotonic():
                if previous.get('state') == 'sent':
                    return {'operation_id': previous['id'], 'message': '请使用刚收到的领取验证码。'}
                if previous.get('state') == 'sms_rejected':
                    raise MemberBenefitsError(previous['message'], previous['error_code'], 409)
                raise MemberBenefitsError('本次验证码发送结果待核对，请稍后再试。', 'MEMBER_CLAIM_BUSY', 409)
            operation = {'id': secrets.token_urlsafe(24), 'state': 'sending', 'period': period}
            self.claims[login_id] = operation

        try:
            with self._connected(login_id, rights_entry=STARBUCKS_COUPONS, starbucks_claim=True) as session:
                try:
                    offer = self._starbucks_coupon(self._read_account_rights(
                        session, STARBUCKS_COUPON_BUSINESS_TYPE, STARBUCKS_COUPON_RULE))
                except MemberBenefitsError as error:
                    if error.code != 'MEMBER_SESSION_EXPIRED':
                        raise
                    self.cache.invalidate(session)
                    raise MemberBenefitsError('星巴克领取会话恢复未通过，本次未发送验证码。',
                                              'MEMBER_STARBUCKS_SESSION_EXPIRED', 409) from error
                if not self._can_claim_starbucks(offer):
                    raise MemberBenefitsError('官方未确认当前账号可免费领取此礼包。', 'MEMBER_NOT_ELIGIBLE', 409)
                order = offer['_order']
                token = session.headers['innerToken']
                operation['sent_at'] = time.monotonic()
                try:
                    self._request(session, 'POST', SUIXIN_PUBLIC + 'sms/sendSmsCode',
                                  headers=_starbucks_signed_headers(token),
                                  json={'businessType': order['businessType'], 'sceneType': 'COMMON_ORDER',
                                        'goodsId': order['goodsId'], 'skuId': order['skuId'],
                                        'goodsName': order['goodsName'], 'token': token,
                                        'frontOrderUrl': STARBUCKS_COUPONS, 'channelId': '', 'from': ''})
                except MemberBenefitsError as error:
                    if error.code != 'MEMBER_SESSION_EXPIRED':
                        raise
                    self.cache.invalidate(session)
                    raise MemberBenefitsError('官方发码时拒绝了星巴克领取会话。请稍后重新获取验证码。',
                                              'MEMBER_STARBUCKS_SMS_SESSION_EXPIRED', 409) from error
                cookies = tuple(copy(cookie) for cookie in session.cookies.jar)
                snapshot = session._credential_binding[1]
        except MemberBenefitsError as error:
            with self.claim_lock:
                if error.code in ('MEMBER_SMS_BAD_REQUEST', 'MEMBER_STARBUCKS_SMS_SESSION_EXPIRED'):
                    operation.update(state='sms_rejected', message=str(error), error_code=error.code)
                else:
                    operation['state'] = 'sms_unconfirmed'
            raise

        with self.claim_lock:
            operation.update(state='sent', order=order, token=token, cookies=cookies, phone=snapshot['phone'])
        return {'operation_id': operation['id'], 'message': '验证码已发送，请填写短信中的领取验证码。'}

    def starbucks_claim(self, login_id, operation_id, sms_code):
        if not re.fullmatch(r'(?:[0-9]{4}|[0-9]{6})', sms_code or ''):
            raise MemberBenefitsError('请输入短信里的 4 位或 6 位验证码。', 'MEMBER_SMS_FORMAT', 400)
        snapshot = self.credentials(login_id)
        with self.claim_lock:
            operation = self.claims.get(login_id, {})
            if (operation.get('id') != operation_id or operation.get('state') != 'sent'
                    or operation.get('period') != self._starbucks_period()
                    or not snapshot or snapshot['phone'] != operation['phone']):
                raise MemberBenefitsError('当前领取验证不可继续，请重新核对资格。', 'MEMBER_CLAIM_STALE', 409)
            self._remember_starbucks_result(login_id, operation['period'], 'pending')
            operation['state'] = 'submitting'
            token, cookies = operation.pop('token'), operation.pop('cookies')
            order = operation['order']

        state = 'pending'
        claimed = None
        try:
            payload = {'actName': None, **order, 'userId': '', 'smsCode': sms_code, 'token': token,
                       'frontOrderUrl': STARBUCKS_COUPONS, 'sellerId': '', 'sellerMobile': '',
                       'operDfp': '', 'channelId': '', 'from': ''}
            with self.session_factory() as session:
                session.headers.update({'User-Agent': APP_UA, 'Origin': ORIGIN, 'Referer': STARBUCKS_COUPONS,
                                        'Accept': 'application/json', 'innerToken': token})
                for cookie in cookies:
                    session.cookies.jar.set_cookie(cookie)
                self._request(session, 'POST', SUIXIN_PUBLIC + 'orderRightsWithSmsByEncrypt',
                              headers=_starbucks_signed_headers(token),
                              json={'ciphertext': _rights_ciphertext(payload)})
                state = 'submitted'
                try:
                    offer = self._starbucks_coupon(self._fetch_account_rights(
                        session, STARBUCKS_COUPON_BUSINESS_TYPE, STARBUCKS_COUPON_RULE))
                    if offer and all(
                            offer['_order'][key] == order[key] for key in ('goodsId', 'skuId', 'actId')):
                        claimed = offer['claimed']
                        if claimed:
                            state = 'claimed'
                except MemberBenefitsError:
                    pass  # The order was accepted. Its final status can be queried in the dialog.
        except MemberBenefitsError as error:
            if error.code in ('MEMBER_CLAIM_REJECTED', 'MEMBER_VERIFICATION', 'MEMBER_RATE_LIMIT', 'MEMBER_SESSION_EXPIRED'):
                state = ''
                raise
            raise MemberBenefitsError('未收到官方领取结果，请刷新查看官方状态。', 'MEMBER_CLAIM_PENDING', 409) from error
        finally:
            with self.claim_lock:
                operation['state'] = state
                self._remember_starbucks_result(login_id, operation['period'], state)
                observed = self.availability.get(login_id)
                if state and observed and observed['period'] == operation['period']:
                    self._save_availability({**self.availability, login_id: {**observed, 'starbucks': 0}})

        label = STARBUCKS_STATUS_LABELS[claimed]
        return {'state': state, 'claimed': claimed, 'status_label': label,
                'claimed_products': [order['goodsName']] if claimed else [],
                'message': ('官方已确认领取成功。' if claimed else f'官方已受理。{label}，请稍后刷新。')}

    def _suixin_public_catalogue(self):
        # The official unauthenticated rightsbaw page provides general offers, not account eligibility.
        params = {'businessType': SUIXIN_BUSINESS_TYPE, 'channelId': '', 'from': ''}
        with self.session_factory() as session:
            session.headers.update({'Origin': ORIGIN, 'Referer': SUIXIN_PORTAL, 'Accept': 'application/json'})
            activities = self._request(session, 'POST', SUIXIN_PUBLIC + 'getActIdsNoLogin', json=params)
            options = []
            for activity in activities or []:
                catalogue = self._request(session, 'POST', SUIXIN_PUBLIC + 'geRightsListNoLogin',
                                          json={**params, 'actId': activity['actId']})
                for floor in (catalogue or {}).get('floorInfoList') or []:
                    group = str(activity.get('actName') or floor.get('floorName') or '')
                    for offer in floor.get('goodsList') or []:
                        if name := offer.get('goodsName'):
                            options.append({'name': str(name)[:120], 'detail': group[:80]})
            return options

    def suixin(self, login_id, product_key):
        return self._suixin_details(login_id, self._suixin_product(login_id, product_key))

    def _suixin_product(self, login_id, product_key):
        with self._connected(login_id) as session:
            products = self._products(session, include_source=True)
        product = next((row for row in products if row['key'] == product_key), None)
        if product is None:
            raise MemberBenefitsError('当前账号没有返回此权益，请刷新列表。', 'MEMBER_OFFER_NOT_FOUND', 404)
        return product

    @staticmethod
    def _can_claim_suixin(option):
        order = option['_order']
        return (option['available'] and option['price'] in ('0', '0.0', '0.00')
                and order['accountType'] == 'MOBILE' and bool(option['_agreement'])
                and all(order[key] for key in ('actId', 'corGoodsId', 'goodsId', 'skuId'))
                # The official page requires additional checks for these products.
                and (order['goodsId'], order['skuId']) not in {
                    ('2021999800006571', 2384), ('9990400200000080042', 2365), ('9990400200000080043', 2666),
                })

    def _save_suixin_result(self, login_id, product_key, period, offer_id, remaining=None):
        records = {key: row for key, row in self.suixin_results.items() if row['period'] == period}
        key = login_id + '|' + product_key
        if offer_id:
            records[key] = {'period': period, 'offer_id': offer_id, 'remaining': remaining}
        else:
            records.pop(key, None)
        if self.claim_storage:
            self.claim_storage.save_activity_data(SUIXIN_CLAIMS, records)
        self.suixin_results = records

    def suixin_prepare(self, login_id, product_key, offer_id):
        product = self._suixin_product(login_id, product_key)
        if not product['_public_catalogue'] or product['claim_status'] != 'available':
            raise MemberBenefitsError('官方未确认本账号还有可领的随心系权益。', 'MEMBER_NOT_ELIGIBLE', 409)
        period = self._starbucks_period()
        with self._connected(login_id, rights_entry=SUIXIN_PORTAL) as session:
            options = self._read_account_rights(session, SUIXIN_BUSINESS_TYPE, None)
            offer = next((item for item in options if item['id'] == offer_id), None)
            if not offer or not self._can_claim_suixin(offer):
                raise MemberBenefitsError('这项权益当前不支持在本地领取，请重新选择。', 'MEMBER_NOT_ELIGIBLE', 409)
            phone = session._credential_binding[1]['phone']
        with self.claim_lock:
            self._check_suixin_pending(login_id, product_key, period)
            previous = self.suixin_operations.get(login_id, {})
            if (previous.get('state') in ('sending', 'submitting', 'sending_verification_sms') or
                    previous.get('state') in ('verification', 'verification_sms') and previous['period'] == period):
                raise MemberBenefitsError('请先完成当前账号的随心系领取。', 'MEMBER_CLAIM_BUSY', 409)
            if previous.get('sent_at', 0) + STARBUCKS_SMS_COOLDOWN > time.monotonic():
                if (previous.get('state') == 'sent' and previous['product_key'] == product_key
                        and previous['offer'] == offer and previous['phone'] == phone and previous['period'] == period):
                    operation = previous
                else:
                    raise MemberBenefitsError('验证码发送间隔为 60 秒，请稍后再试。', 'MEMBER_RATE_LIMIT', 429)
            else:
                operation = {'id': secrets.token_urlsafe(24), 'state': 'prepared', 'period': period,
                             'phone': phone, 'product_key': product_key, 'offer': offer, 'remaining': product['remaining']}
                self.suixin_operations[login_id] = operation
        return {'operation_id': operation['id'], 'name': offer['name'], 'agreement': offer['_agreement']}

    def _check_suixin_pending(self, login_id, product_key, period):
        record = self.suixin_results.get(login_id + '|' + product_key, {})
        if record.get('period') == period:
            raise MemberBenefitsError('已有随心系领取提交，请刷新核对官方记录。', 'MEMBER_CLAIM_PENDING', 409)

    def suixin_sms(self, login_id, operation_id):
        snapshot = self.credentials(login_id)
        with self.claim_lock:
            operation = self.suixin_operations.get(login_id, {})
            if (operation.get('id') != operation_id or operation.get('period') != self._starbucks_period()
                    or operation.get('state') not in ('prepared', 'sent') or not snapshot
                    or snapshot['phone'] != operation['phone']):
                raise MemberBenefitsError('请重新选择随心系权益并确认说明。', 'MEMBER_CLAIM_STALE', 409)
            self._check_suixin_pending(login_id, operation['product_key'], operation['period'])
            if operation.get('sent_at', 0) + STARBUCKS_SMS_COOLDOWN > time.monotonic():
                return {'operation_id': operation_id, 'message': '请使用刚收到的随心系验证码。'}
            operation['state'] = 'sending'
        try:
            with self._connected(login_id, rights_entry=SUIXIN_PORTAL) as session:
                bound = operation['offer']
                offer = next((item for item in self._read_account_rights(session, SUIXIN_BUSINESS_TYPE, None)
                              if item['id'] == bound['id']), None)
                if (not offer or not self._can_claim_suixin(offer) or offer['_order'] != bound['_order']
                        or offer['_agreement'] != bound['_agreement']
                        or session._credential_binding[1]['phone'] != operation['phone']):
                    raise MemberBenefitsError('领取资格或说明已更新，请重新选择权益。', 'MEMBER_CLAIM_STALE', 409)
                token = session.headers['innerToken']
                operation['sent_at'] = time.monotonic()
                try:
                    self._request(session, 'POST', SUIXIN_PUBLIC + 'sms/sendSmsCode',
                                  headers=_starbucks_signed_headers(token, nonce=f'{secrets.randbits(56)}&*'),
                                  json={key: offer['_order'][key] for key in ('businessType', 'goodsId', 'skuId')} | {
                                      'sceneType': 'COMMON_ORDER', 'token': token, 'frontOrderUrl': SUIXIN_ENTRY,
                                      'channelId': '', 'from': '',
                                  })
                except MemberBenefitsError as error:
                    if error.code == 'MEMBER_SESSION_EXPIRED':
                        self.cache.invalidate(session)
                    raise
                cookies = tuple(copy(cookie) for cookie in session.cookies.jar)
        except BaseException:
            with self.claim_lock:
                operation['state'] = 'failed'
            raise
        with self.claim_lock:
            operation.update(state='sent', token=token, cookies=cookies)
        return {'operation_id': operation_id, 'message': '验证码已发送，请填写随心系领取短信中的 4 位验证码。'}

    def suixin_verification_sms(self, login_id, operation_id):
        snapshot = self.credentials(login_id)
        with self.claim_lock:
            operation = self.suixin_operations.get(login_id, {})
            if (operation.get('id') != operation_id or operation.get('state') != 'verification_sms'
                    or operation['period'] != self._starbucks_period() or not snapshot
                    or operation['phone'] != snapshot['phone']):
                raise MemberBenefitsError('本次随心系验证已失效，请重新选择权益。', 'MEMBER_CLAIM_STALE', 409)
            if 'verification_sms_at' in operation and time.monotonic() - operation['verification_sms_at'] < STARBUCKS_SMS_COOLDOWN:
                raise MemberBenefitsError('请等待 60 秒后再获取验证短信。', 'MEMBER_RATE_LIMIT', 429)
            self._check_suixin_pending(login_id, operation['product_key'], operation['period'])
            operation.update(state='sending_verification_sms', verification_sms_at=time.monotonic(), verification_sms_sent=False)
        token = operation['token']
        try:
            with self.session_factory() as session:
                session.headers.update({'User-Agent': APP_UA, 'Origin': ORIGIN, 'Referer': SUIXIN_PORTAL,
                                        'Accept': 'application/json', 'innerToken': token})
                for cookie in operation['cookies']:
                    session.cookies.jar.set_cookie(cookie)
                self._request(session, 'POST', SUIXIN_VERIFICATION_SMS,
                              headers=_starbucks_signed_headers(token, nonce=f'{secrets.randbits(56)}&*'),
                              json={'businessType': SUIXIN_BUSINESS_TYPE, 'token': token,
                                    'frontOrderUrl': SUIXIN_ENTRY, 'channelId': '', 'from': ''})
                operation.update(verification_sms_sent=True, cookies=tuple(copy(cookie) for cookie in session.cookies.jar))
        finally:
            with self.claim_lock:
                operation['state'] = 'verification_sms'
        return {'message': '官方已发送验证短信，请填写新收到的 6 位验证码。'}

    def suixin_claim(self, login_id, operation_id, sms_code, *, resume=False, verification_sms=False):
        digits = 6 if verification_sms else 4
        if not resume and not re.fullmatch(rf'[0-9]{{{digits}}}', sms_code):
            raise MemberBenefitsError(f'请输入短信里的 {digits} 位验证码。', 'MEMBER_SMS_FORMAT', 400)
        expected_state = 'verification' if resume else 'verification_sms' if verification_sms else 'sent'
        snapshot = self.credentials(login_id)
        with self.claim_lock:
            operation = self.suixin_operations.get(login_id, {})
            if (operation.get('id') != operation_id or operation.get('state') != expected_state
                    or operation.get('period') != self._starbucks_period() or not snapshot
                    or operation['phone'] != snapshot['phone']):
                raise MemberBenefitsError('当前随心系验证码会话不可继续，请重新选择权益。', 'MEMBER_CLAIM_STALE', 409)
            if verification_sms and not operation['verification_sms_sent']:
                raise MemberBenefitsError('请先获取 6 位验证短信。', 'MEMBER_VERIFICATION', 409, verification_sms=True)
            period, product_key = operation['period'], operation['product_key']
            self._check_suixin_pending(login_id, product_key, period)
            operation['state'] = 'submitting'
        offer, token = operation['offer'], operation['token']
        posted = False
        state = expected_state
        claimed = False
        try:
            with self.session_factory() as session:
                session.headers.update({'User-Agent': APP_UA, 'Origin': ORIGIN, 'Referer': SUIXIN_PORTAL,
                                        'Accept': 'application/json', 'innerToken': token})
                for cookie in operation['cookies']:
                    session.cookies.jar.set_cookie(cookie)
                options = self._fetch_account_rights(session, SUIXIN_BUSINESS_TYPE, None)
                latest = next((item for item in options if item['id'] == offer['id']), None)
                if latest and latest['claimed']:
                    claimed = True
                    state = 'claimed'
                else:
                    if (not latest or not self._can_claim_suixin(latest) or latest['_order'] != offer['_order']
                            or latest['_agreement'] != offer['_agreement']):
                        raise MemberBenefitsError('领取资格或说明已更新，请重新选择权益。', 'MEMBER_CLAIM_STALE', 409)
                    if resume:
                        unique_id = operation['business_unique_id']
                        status = self._request(session, 'POST', SUIXIN_PUBLIC + 'risk/statusSync',
                                               headers=_starbucks_signed_headers(token, nonce=f'{secrets.randbits(56)}&*'),
                                               json={'businessUniqueId': unique_id, 'token': token,
                                                     'frontOrderUrl': SUIXIN_ENTRY, 'channelId': '', 'from': ''})
                        if not isinstance(status, dict) or status.get('riskStatus') != 'PASS':
                            state = 'verification'
                            raise MemberBenefitsError('官方尚未确认验证通过，请完成验证后再继续。', 'MEMBER_VERIFICATION', 409,
                                                      verification_url=operation['verification_url'], resume_available=True)
                        payload = {**operation['payload'], 'uniqueId': unique_id}
                    else:
                        payload = {key: value for key, value in offer['_order'].items() if key != 'accountType'} | {
                            'price': offer['price'], 'userId': '', 'smsCode': sms_code, 'token': token,
                            'frontOrderUrl': SUIXIN_ENTRY, 'channelId': '', 'from': '', 'sellerId': '', 'sellerMobile': '',
                            'operDfp': next((cookie.value for cookie in session.cookies.jar
                                             if cookie.name == 'BSFIT_DEVICEID' and cookie.domain.lstrip('.') in
                                             ('dev.coc.10086.cn', 'coc.10086.cn', '10086.cn')), ''),
                        }
                    with self.claim_lock:
                        self._save_suixin_result(login_id, product_key, period, offer['id'], operation['remaining'])
                        posted = True
                        state = 'pending'
                    try:
                        self._request(session, 'POST', SUIXIN_ORDER,
                                      headers=_starbucks_signed_headers(token, nonce=f'{secrets.randbits(56)}&*'),
                                      json={'ciphertext': _rights_ciphertext(payload)})
                    finally:
                        operation['cookies'] = tuple(copy(cookie) for cookie in session.cookies.jar)
                    state = 'submitted'
                    try:
                        options = self._fetch_account_rights(session, SUIXIN_BUSINESS_TYPE, None)
                        claimed = any(item['id'] == offer['id'] and item['claimed'] for item in options)
                    except MemberBenefitsError:
                        pass  # Preserve the accepted submission until a later status read confirms it.
                    if claimed:
                        state = 'claimed'
        except MemberBenefitsError as error:
            if posted and error.verification_url and error.business_unique_id:
                state = 'verification'
                operation.update(payload=payload, business_unique_id=error.business_unique_id,
                                 verification_url=error.verification_url)
                error.resume_available = True
            elif posted and error.verification_sms:
                state = 'verification_sms'
                if not verification_sms:
                    operation['verification_sms_sent'] = False
            elif error.code == 'MEMBER_SMS_INVALID':
                state = 'verification_sms' if verification_sms else 'sent'
                error.verification_sms = verification_sms
            elif posted and error.code in ('MEMBER_CLAIM_REJECTED', 'MEMBER_RATE_LIMIT',
                                           'MEMBER_SESSION_EXPIRED', 'MEMBER_VERIFICATION'):
                state = 'failed'
            elif posted:
                state = 'pending'
                raise MemberBenefitsError('领取回执未确认，请刷新核对官方记录。', 'MEMBER_CLAIM_PENDING', 409) from error
            elif error.code in ('MEMBER_SESSION_EXPIRED', 'MEMBER_CLAIM_STALE'):
                state = 'failed'
            elif state == 'verification':
                error.resume_available = True
                error.verification_url = operation['verification_url']
            elif state == 'verification_sms':
                error.verification_sms = True
            raise
        finally:
            with self.claim_lock:
                operation['state'] = state
                if state not in ('sent', 'verification', 'verification_sms'):
                    for key in ('token', 'cookies', 'payload', 'business_unique_id', 'verification_url'):
                        operation.pop(key, None)
                if posted and state not in ('pending', 'submitted', 'claimed'):
                    self._save_suixin_result(login_id, product_key, period, '')
        return {'state': state, 'claimed': claimed, 'status_label': '已领取' if claimed else '领取状态待核对',
                'claimed_products': [offer['name']] if claimed else [],
                'message': '官方已确认领取。' if claimed else '官方已受理，请刷新核对领取状态。'}

    def _suixin_details(self, login_id, product):
        public_catalogue = product.pop('_public_catalogue')
        status = product['claim_status']
        message = {
            'available': '已查到剩余领取次数。具体可选权益和领取提交尚未接通。',
            'claimed': '官方目录显示本期已领取。',
            'unknown': '官方目录未确认当前账号的剩余领取次数。',
        }[status]
        options = []
        catalogue_kind = ''
        can_claim = False
        with self.claim_lock:
            period = self._starbucks_period()
            pending = self.suixin_results.get(login_id + '|' + product['key'], {})
            operation = self.suixin_operations.get(login_id, {})
            verification = ({'operation_id': operation['id'], 'url': operation['verification_url'],
                             'name': operation['offer']['name']}
                            if operation.get('state') == 'verification' and operation['period'] == period
                            and operation['product_key'] == product['key'] else None)
            if (operation.get('state') in ('verification_sms', 'sending_verification_sms')
                    and operation['period'] == period and operation['product_key'] == product['key']):
                verification = {'operation_id': operation['id'], 'name': operation['offer']['name'],
                                'sms': True, 'sms_sent': operation['verification_sms_sent']}
        if status in ('available', 'claimed') and public_catalogue:
            try:
                options = self._account_rights(login_id, SUIXIN_BUSINESS_TYPE, entry=SUIXIN_PORTAL)
                catalogue_kind = 'account'
                for option in options:
                    option['can_claim'] = self._can_claim_suixin(option)
                    del option['_order'], option['_agreement']
                can_claim = status == 'available' and any(option['can_claim'] for option in options)
                message = ('请选择一项权益领取。' if can_claim else
                           '官方显示本期已领取。' if status == 'claimed' else '当前商品暂不支持在工具台领取。')
            except MemberBenefitsError as error:
                message = str(error)
                if status == 'available' and error.code in ('MEMBER_SESSION_EXPIRED', 'MEMBER_UPSTREAM', 'MEMBER_FORMAT'):
                    try:
                        options = self._suixin_public_catalogue()
                    except MemberBenefitsError:
                        pass
                    if options:
                        catalogue_kind = 'public'
                        message = '以下为公开目录，本账号的领取资格尚待确认。'
        if pending.get('period') == period:
            confirmed = any(option.get('id') == pending['offer_id'] and option['claimed'] for option in options)
            remaining = product['remaining']
            if status == 'claimed' or (confirmed and remaining is not None and pending.get('remaining') is not None
                                       and remaining < pending['remaining']):
                with self.claim_lock:
                    if self.suixin_results.get(login_id + '|' + product['key']) is pending:
                        self._save_suixin_result(login_id, product['key'], period, '')
            elif confirmed and pending.get('remaining') == 1:
                product.update(claim_status='claimed', claim_label='已领取', remaining=0)
                can_claim, message, status = False, '', 'claimed'
            else:
                product.update(claim_status='pending', claim_label='领取状态待核对')
                can_claim, message = False, '已有随心系领取提交，请刷新核对官方记录。'
        if status == 'available' and verification:
            product['verification'] = verification
            can_claim, message = False, ''
        return {**product, 'options': options, 'catalogue_kind': catalogue_kind,
                'can_claim': can_claim, 'message': message}

    def _check_sku(self, session, activity_id, sku_id, *, recover=True, **params):
        data = self._read(session, ELIGIBILITY, recover=recover, activityId=activity_id, skuId=sku_id, **params)
        row = next((item for item in data if isinstance(item, dict) and _public_id(item.get('skuId')) == sku_id),
                   None) if isinstance(data, list) else None
        if (row is None and isinstance(data, list) and len(data) == 1
                and isinstance(data[0], dict) and data[0].get('skuId') is None):
            row = data[0]
        if row is None or row.get('skuResultCode') is None:
            raise MemberBenefitsError('官方未确认领取资格。', 'MEMBER_FORMAT')
        eligible = str(row['skuResultCode']) == '0'
        message = '资格核对通过，请在官方页面完成确认。' if eligible else str(
            row.get('skuResultMsg') or '官方未允许领取。')[:160]
        return {'eligible': eligible, 'message': message}

    def _campus_benefit(self, session, member_id, *, recover=True):
        members = _list_members(self._catalog(session, recover=recover), include_source=True)
        return next((benefit for member in members if member['id'] == member_id
                     for benefit in member['benefits'] if benefit.get('claim_flow') == 'campus'), None)

    def _campus_product(self, session, member_id, sku_id, *, recover=True, check_completed=False):
        benefit = self._campus_benefit(session, member_id, recover=recover)
        if check_completed and benefit and sku_id in benefit['_source']['ordered_ids']:
            return None, '', '', None
        if (not benefit or benefit['_source']['status'] != '1' or not benefit['remaining']
                or sku_id in benefit['_source']['ordered_ids']):
            raise MemberBenefitsError('当前会员没有可领取的福袋次数。', 'MEMBER_NOT_ELIGIBLE', 409)
        selection = self._benefit_offers(session, benefit, member_id, recover=recover, include_rule=True)
        if not any(offer['id'] == sku_id and offer.get('can_claim') for offer in selection['offers']):
            raise MemberBenefitsError('请选择此会员可免费领取的福袋商品。', 'MEMBER_OFFER_NOT_FOUND', 404)
        activity_id = benefit['_source']['activity_id']
        mutex = self._read(session, '/coc3/coc3-market-activity/api/activity/checkActivityMutex',
                           recover=recover, activityId=activity_id, memberId=member_id)
        if not isinstance(mutex, dict) or str(mutex.get('status')) != '0':
            message = mutex.get('mutexRemind') if isinstance(mutex, dict) else None
            raise MemberBenefitsError(str(message or '官方要求确认活动限制，本次未进入领取验证。'),
                                      'MEMBER_VERIFICATION', 409)
        result = self._check_sku(session, activity_id, sku_id, recover=recover, memberId=member_id)
        if not result['eligible']:
            raise MemberBenefitsError(result['message'], 'MEMBER_NOT_ELIGIBLE', 409)
        detail = self._read(session, PRODUCT_DETAIL, recover=recover, id=sku_id, isNeedDesc='false')
        if (not isinstance(detail, dict) or _public_id(detail.get('id')) != sku_id
                or _count(detail.get('price')) != 0 or str(detail.get('paytype')) != '1'
                or detail.get('newSku') or detail.get('accountType') not in (None, '', 0, '0', 17, '17')):
            raise MemberBenefitsError('该商品需要额外订购或绑定验证，本次未发送验证码。', 'MEMBER_VERIFICATION', 409)
        binding = ''
        if str(detail.get('accountType')) == '17':
            account = self._read(session, '/coc3/coc3-market/arrange/queryPhoneBindInfo', recover=recover, id=sku_id)
            if not isinstance(account, dict) or not (account.get('nickname') or account.get('sensiveMobile')):
                raise MemberBenefitsError('官方未确认该商品的到账账号。', 'MEMBER_VERIFICATION', 409)
            binding = '权益将发放至：' + str(account.get('nickname') or account['sensiveMobile'])
        restriction = self._request(session, 'POST', '/coc3/coc3-market-order/api/external/black/checkBlacklist',
                                    json={'skuId': int(sku_id)})
        if not isinstance(restriction, dict) or str(restriction.get('isBlackList')) != '0':
            raise MemberBenefitsError('官方订购限制核对未通过，本次未发送验证码。', 'MEMBER_NOT_ELIGIBLE', 409)
        return detail, binding, str(mutex.get('preRemind') or ''), selection.get('_rule_code')

    def campus_prepare(self, login_id, sku_id, *, member_id=CAMPUS_MEMBER):
        if os.environ.get('LINGXI_ANDROID') == '1':
            raise MemberBenefitsError(ANDROID_ORDER_NOTICE, 'MEMBER_ORDER_UNSUPPORTED', 409)
        period = self._starbucks_period()
        result_key = self._gift_bag_result_key(login_id, member_id)
        with self.claim_lock:
            if self.campus_results.get(result_key, {}).get('period') == period:
                raise MemberBenefitsError('已有领取提交记录，请刷新核对官方状态。', 'MEMBER_CLAIM_PENDING', 409)
        with self._connected(login_id) as session:
            session.headers['Referer'] = (PLATINUM_ENTRY if member_id == PLATINUM_MEMBER else
                                          CAMPUS_ENTRY + '&memberId=' + CAMPUS_MEMBER)
            detail, binding, reminder, _ = self._campus_product(session, member_id, sku_id)
            phone = session._credential_binding[1]['phone']
        agreement = self._campus_agreement(detail, reminder)
        terms = CampusPreparation(phone, member_id, sku_id, period, agreement, binding)
        with self.claim_lock:
            preparations = self.campus_prepared.get(login_id, {})
            preparations = {key: value for key, value in preparations.items() if value.period == period}
            preparation_id = next((key for key, value in preparations.items() if value == terms), None)
            if preparation_id is None:
                preparation_id = secrets.token_urlsafe(24)
                preparations = {key: value for key, value in preparations.items()
                                if (value.member_id, value.sku_id) != (member_id, sku_id)}
                preparations[preparation_id] = terms
            self.campus_prepared[login_id] = preparations
        return {'agreement': agreement, 'preparation_id': preparation_id,
                'agreement_name': str(detail.get('serviceAgreementName') or '业务受理说明'), 'binding': binding}

    @staticmethod
    def _campus_agreement(detail, reminder):
        agreement = _product_agreement(detail)
        if not agreement:
            raise MemberBenefitsError('官方未返回商品受理说明，本次未进入短信验证。', 'MEMBER_FORMAT', 409)
        return f'{reminder}\n\n{agreement}' if reminder else agreement

    def _campus_device(self, session, order, before_submit):
        if os.environ.get('LINGXI_ANDROID') == '1':
            raise MemberBenefitsError(ANDROID_ORDER_NOTICE, 'MEMBER_ORDER_UNSUPPORTED', 409)
        try:
            result = self.campus_protocol.prepare(tuple(session.cookies.jar), session.headers['phone'],
                                                  session.headers['User-Agent'], session.headers['Referer'],
                                                  order=order, before_submit=before_submit)
        except CampusProtocolError as error:
            code = 'MEMBER_CLAIM_PENDING' if error.uncertain else 'MEMBER_VERIFICATION'
            raise MemberBenefitsError(str(error), code, 409) from error
        for cookie in result['cookies']:
            session.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'],
                                path=cookie.get('path') or '/', secure=bool(cookie.get('secure')))
        return result

    def campus_sms(self, login_id, sku_id, preparation_id):
        period = self._starbucks_period()
        with self.claim_lock:
            preparation = self.campus_prepared.get(login_id, {}).get(preparation_id)
            if preparation is None or preparation.sku_id != sku_id or preparation.period != period:
                raise MemberBenefitsError('商品受理说明已更新，请重新选择商品。', 'MEMBER_CLAIM_STALE', 409)
            member_id = preparation.member_id
            result_key = self._gift_bag_result_key(login_id, member_id)
            previous = self.campus_operations.get(login_id, {})
            if self.campus_results.get(result_key, {}).get('period') == period:
                raise MemberBenefitsError('已有领取提交记录，请刷新核对官方状态。', 'MEMBER_CLAIM_PENDING', 409)
            if previous.get('state') in ('sending', 'submitting'):
                raise MemberBenefitsError('当前账号已有一笔福袋操作正在处理。', 'MEMBER_CLAIM_BUSY', 409)
            if 'sent_at' in previous and previous['sent_at'] + CAMPUS_SMS_COOLDOWN > time.monotonic():
                if previous.get('state') == 'sent' and previous['preparation'] == preparation:
                    return {'operation_id': previous['id'], 'message': '请使用刚收到的福袋领取验证码。'}
                raise MemberBenefitsError('请在 60 秒后重新获取验证码。', 'MEMBER_RATE_LIMIT', 429)
            operation = {'id': secrets.token_urlsafe(24), 'sku_id': sku_id, 'state': 'sending', 'period': period,
                         'preparation': preparation, 'result_key': result_key}
            self.campus_operations[login_id] = operation
        try:
            with self._connected(login_id) as session:
                session.headers['Referer'] = (PLATINUM_ENTRY if member_id == PLATINUM_MEMBER else
                                              CAMPUS_ENTRY + '&memberId=' + CAMPUS_MEMBER)
                detail, binding, reminder, rule_code = self._campus_product(session, member_id, sku_id)
                snapshot = session._credential_binding[1]
                if (snapshot['phone'] != preparation.phone or self._campus_agreement(detail, reminder) != preparation.agreement
                        or binding != preparation.binding):
                    raise MemberBenefitsError('商品受理说明或账号已变化，请重新选择商品。', 'MEMBER_CLAIM_STALE', 409)
                token = session.headers['phone']
                operation['sent_at'] = time.monotonic()
                try:
                    self._request(session, 'POST', CAMPUS_SMS, json={
                        'receivers': token, 'skuId': _market_encrypt(f'{sku_id}_{time.time_ns() // 1_000_000}', b'72fc912ce498430d'),
                        'member': True, 'businessChannelCode': CAMPUS_CHANNEL,
                    })
                except MemberBenefitsError as error:
                    if error.code == 'MEMBER_SESSION_EXPIRED':
                        self.cache.invalidate(session)
                    raise
                scope, _, _, headers, generation = session._credential_binding
                operation.update(phone=snapshot['phone'], token=token, rule_code=rule_code,
                                 binding=(scope, snapshot, None, headers, generation),
                                 cookies=tuple(copy(cookie) for cookie in session.cookies.jar))
        except BaseException:
            with self.claim_lock:
                if previous.get('state') == 'sent' and 'sent_at' not in operation:
                    self.campus_operations[login_id] = previous
                else:
                    operation['state'] = 'failed'
            raise
        with self.claim_lock:
            operation['state'] = 'sent'
        return {'operation_id': operation['id'], 'message': '验证码已发送，请填写短信中的领取验证码。'}

    def _save_campus_result(self, operation, state):
        records = {key: row for key, row in self.campus_results.items() if row['period'] == operation['period']}
        result_key = operation['result_key']
        if state:
            records[result_key] = {'period': operation['period'], 'sku_id': operation['sku_id'], 'state': state}
        else:
            records.pop(result_key, None)
        if self.claim_storage:
            self.claim_storage.save_activity_data(CAMPUS_CLAIMS, records)
        self.campus_results = records

    def campus_claim(self, login_id, operation_id, sms_code, *, resume=False):
        if not resume and not re.fullmatch(r'(?:[0-9]{4}|[0-9]{6})', sms_code):
            raise MemberBenefitsError('请输入短信里的 4 位或 6 位验证码。', 'MEMBER_SMS_FORMAT', 400)
        snapshot = self.credentials(login_id)
        with self.claim_lock:
            operation = self.campus_operations.get(login_id, {})
            if (operation.get('id') != operation_id or operation.get('state') != ('verification' if resume else 'sent')
                    or operation.get('period') != self._starbucks_period()
                    or not snapshot or operation['phone'] != snapshot['phone']):
                raise MemberBenefitsError('当前福袋验证不可继续，请重新获取验证码。', 'MEMBER_CLAIM_STALE', 409)
            encrypted_sms = operation.pop('verification_sms') if resume else _market_encrypt(sms_code, b'a4jdiq597p3gls0r')
            operation['state'] = 'submitting'
        preparation = operation['preparation']
        member_id = preparation.member_id
        platinum = member_id == PLATINUM_MEMBER
        state = ''
        claimed = None
        retain_sms = False
        verification_pending = False
        attempt_recorded = False

        def record_order_attempt():
            nonlocal state, attempt_recorded
            with self.claim_lock:
                self._save_campus_result(operation, 'pending')
                state = 'pending'
                attempt_recorded = True

        try:
            entry = PLATINUM_ENTRY if platinum else CAMPUS_ENTRY + '&memberId=' + CAMPUS_MEMBER
            with self.session_factory() as session:
                session.headers.update({'User-Agent': APP_UA, 'Origin': ORIGIN, 'Referer': entry,
                                        'Accept': 'application/json', 'phone': operation['token']})
                for cookie in operation['cookies']:
                    session.cookies.jar.set_cookie(cookie)
                session._credential_binding = operation['binding']
                if resume:
                    detail, binding, reminder, _ = self._campus_product(session, member_id, preparation.sku_id,
                                                                        recover=False, check_completed=True)
                    if detail is None:
                        with self.claim_lock:
                            self._save_campus_result(operation, 'claimed')
                        state = 'claimed'
                        return {'state': state, 'claimed': True, 'status_label': '已领取',
                                'message': '官方已确认领取成功。'}
                    if self._campus_agreement(detail, reminder) != preparation.agreement or binding != preparation.binding:
                        raise MemberBenefitsError('商品受理说明或到账账号已变化，请重新选择商品。', 'MEMBER_CLAIM_STALE', 409)
                order = {'skuId': int(operation['sku_id']), 'activityId': int(PLATINUM_ACTIVITY if platinum else CAMPUS_ACTIVITY),
                         'channelCode': CAMPUS_CHANNEL, 'selfChannelCode': CAMPUS_CHANNEL,
                         'smsCode': encrypted_sms, 'dealType': 0,
                         'referer': PLATINUM_PAGE if platinum else CAMPUS_PAGE, 'receipt': True, 'faultyReceipt': True}
                if platinum:
                    order['memberId'] = member_id
                    if operation['rule_code']:
                        order['ruleCode'] = operation['rule_code']
                try:
                    risk = self._campus_device(session, order, record_order_attempt)
                except MemberBenefitsError as error:
                    if state:
                        raise
                    if error.code == 'MEMBER_SESSION_EXPIRED':
                        self.cache.invalidate(session)
                        raise
                    retain_sms = True
                    raise MemberBenefitsError(f'设备校验未完成，尚未提交领取。{error}',
                                              'MEMBER_CAMPUS_DEVICE_UNAVAILABLE', error.status) from error
                operation['cookies'] = tuple(copy(cookie) for cookie in session.cookies.jar)
                try:
                    response = risk['order']
                    self._official_result(CAMPUS_ORDER, response['status'], response['body'])
                except MemberBenefitsError as error:
                    if error.code == 'MEMBER_SESSION_EXPIRED':
                        self.cache.invalidate(session)
                    raise
                state = 'submitted'
                try:
                    benefit = self._campus_benefit(session, member_id, recover=False)
                    if benefit and operation['sku_id'] in benefit['_source']['ordered_ids']:
                        claimed = True
                        state = 'claimed'
                except MemberBenefitsError:
                    pass  # The order was accepted. A later overview read can confirm it.
        except MemberBenefitsError as error:
            if not state or error.code in ('MEMBER_SMS_INVALID', 'MEMBER_CLAIM_REJECTED', 'MEMBER_VERIFICATION',
                                          'MEMBER_CAMPUS_DEVICE_REFUSED', 'MEMBER_RATE_LIMIT', 'MEMBER_SESSION_EXPIRED'):
                state = ''
                verification_pending = bool(error.verification_url) or (resume and retain_sms)
                error.resume_available = verification_pending
                retain_sms |= verification_pending or error.code == 'MEMBER_SMS_INVALID'
                raise
            if error.code == 'MEMBER_CLAIM_PENDING':
                raise
            raise MemberBenefitsError('未收到官方领取结果，请刷新查看状态。', 'MEMBER_CLAIM_PENDING', 409) from error
        finally:
            with self.claim_lock:
                operation['state'] = state or ('verification' if verification_pending else 'sent' if retain_sms else 'failed')
                if verification_pending:
                    operation['verification_sms'] = encrypted_sms
                if not retain_sms:
                    for key in ('cookies', 'token', 'binding'):
                        del operation[key]
                if attempt_recorded and state != 'pending':
                    self._save_campus_result(operation, state)
                observed = self.availability.get(login_id)
                field = 'platinum' if platinum else 'campus'
                if state and observed and observed['period'] == operation['period'] and observed.get(field):
                    self._save_availability({**self.availability, login_id: {**observed, field: 0}})
        return {'state': state, 'claimed': claimed, 'status_label': '已领取' if claimed else '领取状态待核对',
                'message': '官方已确认领取成功。' if claimed else '官方已受理，请刷新核对发放状态。'}

    def _discount_product(self, session, member_id, benefit_key, sku_id, *, recover=True, check_completed=False):
        members = _list_members(self._catalog(session, recover=recover), include_source=True)
        benefit = next((row for member in members if member['id'] == member_id
                        for row in member['benefits'] if row['key'] == benefit_key), None)
        if (check_completed and benefit and benefit['kind'] == '五折购'
                and sku_id in benefit['_source']['ordered_ids']):
            return benefit, None, '', ''
        if (not benefit or benefit['kind'] != '五折购' or benefit['_source']['status'] != '1'
                or not benefit['remaining'] or sku_id in benefit['_source']['ordered_ids']):
            raise MemberBenefitsError('当前账号没有可订购的五折购次数。', 'MEMBER_NOT_ELIGIBLE', 409)
        session.headers['Referer'] = benefit['url']
        selection = self._benefit_offers(session, benefit, member_id, recover=recover)
        if not any(offer['id'] == sku_id for offer in selection['offers']):
            raise MemberBenefitsError('请选择当前五折购清单中的商品。', 'MEMBER_OFFER_NOT_FOUND', 404)
        activity_id = benefit['_source']['activity_id']
        mutex = self._read(session, '/coc3/coc3-market-activity/api/activity/checkActivityMutex',
                           recover=recover, activityId=activity_id, memberId=member_id)
        if not isinstance(mutex, dict) or str(mutex.get('status')) != '0':
            message = mutex.get('mutexRemind') if isinstance(mutex, dict) else None
            raise MemberBenefitsError(str(message or '官方要求确认活动限制，本次未进入订购验证。'),
                                      'MEMBER_VERIFICATION', 409)
        eligibility = self._check_sku(session, activity_id, sku_id, recover=recover, memberId=member_id)
        if not eligibility['eligible']:
            raise MemberBenefitsError(eligibility['message'], 'MEMBER_NOT_ELIGIBLE', 409)
        detail = self._read(session, PRODUCT_DETAIL, recover=recover, id=sku_id, isNeedDesc='false')
        price = _count(detail.get('price')) if isinstance(detail, dict) else None
        if (not isinstance(detail, dict) or _public_id(detail.get('id')) != sku_id or price is None or price <= 0
                or str(detail.get('paytype')) != '1' or detail.get('newSku')
                or detail.get('accountType') not in (None, '', 0, '0')):
            raise MemberBenefitsError('该商品的支付方式暂不支持本地订购。', 'MEMBER_VERIFICATION', 409)
        restriction = self._request(session, 'POST',
            '/coc3/coc3-market-order/api/external/black/checkBlacklist', json={'skuId': int(sku_id)})
        if not isinstance(restriction, dict) or str(restriction.get('isBlackList')) != '0':
            raise MemberBenefitsError('官方订购限制核对未通过，本次未发送验证码。',
                                      'MEMBER_NOT_ELIGIBLE', 409)
        agreement = _product_agreement(detail)
        if not agreement:
            raise MemberBenefitsError('官方未返回商品受理说明，本次未进入短信验证。', 'MEMBER_FORMAT', 409)
        return benefit, detail, agreement, str(mutex.get('preRemind') or '')

    def discount_prepare(self, login_id, member_id, benefit_key, sku_id):
        if os.environ.get('LINGXI_ANDROID') == '1':
            raise MemberBenefitsError(ANDROID_ORDER_NOTICE, 'MEMBER_ORDER_UNSUPPORTED', 409)
        period = self._starbucks_period()
        with self._connected(login_id) as session:
            benefit, detail, agreement, reminder = self._discount_product(
                session, member_id, benefit_key, sku_id)
            phone = session._credential_binding[1]['phone']
        page_url = benefit['url']
        price = _count(detail['price'])
        terms = DiscountPreparation(phone, member_id, benefit_key, benefit['_source']['activity_id'],
                                    sku_id, period, price, agreement, page_url)
        result_key = self._discount_result_key(login_id, member_id, terms.activity_id)
        with self.claim_lock:
            if self.discount_results.get(result_key, {}).get('period') == period:
                raise MemberBenefitsError('已有订购提交记录，请刷新核对官方状态。',
                                          'MEMBER_CLAIM_PENDING', 409)
            preparations = {key: value for key, value in self.discount_prepared.get(login_id, {}).items()
                            if value.period == period}
            preparation_id = next((key for key, value in preparations.items() if value == terms), None)
            if preparation_id is None:
                preparation_id = secrets.token_urlsafe(24)
                preparations = {key: value for key, value in preparations.items()
                                if value.member_id != member_id or value.activity_id != terms.activity_id}
                preparations[preparation_id] = terms
            self.discount_prepared[login_id] = preparations
        return {'url': page_url, 'eligibility': {'eligible': True, 'message': '官方资格核对通过。'},
                'purchase_preview': {'price_cents': price,
                                     'agreement_name': str(detail.get('serviceAgreementName') or '业务受理说明')[:120],
                                     'agreement': f'{reminder}\n\n{agreement}' if reminder else agreement},
                'preparation_id': preparation_id}

    def discount_sms(self, login_id, sku_id, preparation_id):
        period = self._starbucks_period()
        with self.claim_lock:
            preparation = self.discount_prepared.get(login_id, {}).get(preparation_id)
            previous = self.discount_operations.get(login_id, {})
            if preparation is None or preparation.sku_id != sku_id or preparation.period != period:
                raise MemberBenefitsError('商品价格或受理说明已更新，请重新选择。', 'MEMBER_CLAIM_STALE', 409)
            result_key = self._discount_result_key(login_id, preparation.member_id, preparation.activity_id)
            if self.discount_results.get(result_key, {}).get('period') == period:
                raise MemberBenefitsError('已有订购提交记录，请刷新核对官方状态。',
                                          'MEMBER_CLAIM_PENDING', 409)
            if previous.get('state') in ('sending', 'submitting'):
                raise MemberBenefitsError('当前账号已有一笔五折购正在处理。', 'MEMBER_CLAIM_BUSY', 409)
            if previous.get('sent_at', 0) + DISCOUNT_SMS_COOLDOWN > time.monotonic():
                if (previous.get('state') == 'sent' and previous.get('preparation') == preparation
                        and previous.get('period') == period):
                    return {'operation_id': previous['id'], 'message': '请使用刚收到的订购验证码。'}
                raise MemberBenefitsError('请在 60 秒后重新获取验证码。', 'MEMBER_RATE_LIMIT', 429)
            operation = {'id': secrets.token_urlsafe(24), 'state': 'sending', 'period': period,
                         'preparation': preparation, 'result_key': result_key}
            self.discount_operations[login_id] = operation
        try:
            with self._connected(login_id) as session:
                session.headers['Referer'] = preparation.page_url
                benefit, detail, agreement, _ = self._discount_product(
                    session, preparation.member_id, preparation.benefit_key, sku_id)
                snapshot = session._credential_binding[1]
                if (snapshot['phone'] != preparation.phone or benefit['url'] != preparation.page_url
                        or benefit['_source']['activity_id'] != preparation.activity_id
                        or _count(detail.get('price')) != preparation.price_cents or agreement != preparation.agreement):
                    raise MemberBenefitsError('商品价格或受理说明已更新，请重新核对。',
                                              'MEMBER_PRICE_CHANGED', 409)
                operation['sent_at'] = time.monotonic()
                self._request(session, 'POST', CAMPUS_SMS, json={
                    'receivers': session.headers['phone'],
                    'skuId': _market_encrypt(f'{sku_id}_{time.time_ns() // 1_000_000}', b'72fc912ce498430d'),
                    'member': True, 'businessChannelCode': CAMPUS_CHANNEL,
                })
                scope, _, _, headers, generation = session._credential_binding
                operation.update(phone=snapshot['phone'], token=session.headers['phone'],
                                 binding=(scope, snapshot, None, headers, generation),
                                 cookies=tuple(copy(cookie) for cookie in session.cookies.jar))
        except BaseException:
            with self.claim_lock:
                operation['state'] = 'failed'
            raise
        with self.claim_lock:
            operation['state'] = 'sent'
        return {'operation_id': operation['id'], 'message': '验证码已发送，请填写短信中的订购验证码。'}

    def _save_discount_result(self, operation, state):
        records = {key: row for key, row in self.discount_results.items()
                   if row['period'] == operation['period']}
        if state:
            records[operation['result_key']] = {
                'period': operation['period'], 'sku_id': operation['preparation'].sku_id, 'state': state}
        else:
            records.pop(operation['result_key'], None)
        if self.claim_storage:
            self.claim_storage.save_activity_data(DISCOUNT_CLAIMS, records)
        self.discount_results = records

    def discount_claim(self, login_id, operation_id, sms_code, *, resume=False):
        if not resume and not re.fullmatch(r'(?:[0-9]{4}|[0-9]{6})', sms_code):
            raise MemberBenefitsError('请输入短信里的 4 位或 6 位验证码。', 'MEMBER_SMS_FORMAT', 400)
        snapshot = self.credentials(login_id)
        with self.claim_lock:
            operation = self.discount_operations.get(login_id, {})
            if (operation.get('id') != operation_id or operation.get('state') != ('verification' if resume else 'sent')
                    or operation.get('period') != self._starbucks_period() or not snapshot
                    or operation.get('phone') != snapshot['phone']):
                raise MemberBenefitsError('当前五折购验证不可继续，请重新获取验证码。',
                                          'MEMBER_CLAIM_STALE', 409)
            encrypted_sms = operation.pop('verification_sms') if resume else _market_encrypt(sms_code, b'a4jdiq597p3gls0r')
            operation['state'] = 'submitting'
        preparation = operation['preparation']
        state = ''
        claimed = None
        retain_sms = False
        verification_pending = False
        attempt_recorded = False

        def record_order_attempt():
            nonlocal state, attempt_recorded
            with self.claim_lock:
                self._save_discount_result(operation, 'pending')
                state = 'pending'
                attempt_recorded = True

        try:
            with self.session_factory() as session:
                session.headers.update({'User-Agent': APP_UA, 'Origin': ORIGIN, 'Referer': preparation.page_url,
                                        'Accept': 'application/json', 'phone': operation['token']})
                for cookie in operation['cookies']:
                    session.cookies.jar.set_cookie(cookie)
                session._credential_binding = operation['binding']
                benefit, detail, agreement, _ = self._discount_product(
                    session, preparation.member_id, preparation.benefit_key,
                    preparation.sku_id, recover=False, check_completed=resume)
                if detail is None:
                    with self.claim_lock:
                        self._save_discount_result(operation, 'claimed')
                    state = 'claimed'
                    return {'state': state, 'claimed': True, 'status_label': '已订购',
                            'message': '官方已确认订购成功。'}
                if (benefit['url'] != preparation.page_url or
                        benefit['_source']['activity_id'] != preparation.activity_id or
                        _count(detail.get('price')) != preparation.price_cents or agreement != preparation.agreement):
                    raise MemberBenefitsError('商品价格或受理说明已更新，本次没有提交订购。',
                                              'MEMBER_PRICE_CHANGED', 409)
                order = {'skuId': int(preparation.sku_id), 'activityId': int(preparation.activity_id),
                         'memberId': preparation.member_id, 'channelCode': CAMPUS_CHANNEL,
                         'selfChannelCode': CAMPUS_CHANNEL,
                         'smsCode': encrypted_sms, 'dealType': 0,
                         'referer': urlsplit(preparation.page_url).path.rsplit('/', 1)[-1]}
                try:
                    risk = self._campus_device(session, order, record_order_attempt)
                except MemberBenefitsError as error:
                    if state:
                        raise
                    retain_sms = True
                    raise MemberBenefitsError(f'设备校验未完成，尚未提交订购。{error}',
                                              'MEMBER_CAMPUS_DEVICE_UNAVAILABLE', error.status) from error
                operation['cookies'] = tuple(copy(cookie) for cookie in session.cookies.jar)
                response = risk['order']
                self._official_result(CAMPUS_ORDER, response['status'], response['body'])
                state = 'submitted'
                try:
                    current = next((row for member in _list_members(
                        self._catalog(session, recover=False), include_source=True)
                        if member['id'] == preparation.member_id
                        for row in member['benefits'] if row['key'] == preparation.benefit_key), None)
                    if current and preparation.sku_id in current['_source']['ordered_ids']:
                        claimed = True
                        state = 'claimed'
                except MemberBenefitsError:
                    pass
        except MemberBenefitsError as error:
            if not state or error.code in ('MEMBER_SMS_INVALID', 'MEMBER_CLAIM_REJECTED', 'MEMBER_VERIFICATION',
                                          'MEMBER_CAMPUS_DEVICE_REFUSED', 'MEMBER_RATE_LIMIT',
                                          'MEMBER_SESSION_EXPIRED', 'MEMBER_PRICE_CHANGED'):
                state = ''
                verification_pending = bool(error.verification_url) or (resume and retain_sms)
                error.resume_available = verification_pending
                retain_sms |= verification_pending or error.code == 'MEMBER_SMS_INVALID'
                raise
            raise MemberBenefitsError('未收到官方订购结果，请刷新核对状态。',
                                      'MEMBER_CLAIM_PENDING', 409) from error
        finally:
            with self.claim_lock:
                operation['state'] = state or ('verification' if verification_pending else 'sent' if retain_sms else 'failed')
                if verification_pending:
                    operation['verification_sms'] = encrypted_sms
                if not retain_sms:
                    for key in ('cookies', 'token', 'binding'):
                        operation.pop(key, None)
                if attempt_recorded and state != 'pending':
                    self._save_discount_result(operation, state)
        return {'state': state, 'claimed': claimed,
                'status_label': '已订购' if claimed else '订购状态待核对',
                'message': '官方已确认订购成功。' if claimed else '官方已受理，请刷新核对订购状态。'}

    def offers(self, login_id, member_id, benefit_key, sku_id=None):
        with self._connected(login_id) as session:
            members = _list_members(self._catalog(session), include_source=True)
            benefit = next((row for member in members if member['id'] == member_id
                            for row in member['benefits'] if row['key'] == benefit_key), None)
            if not benefit or not benefit['can_inspect']:
                raise MemberBenefitsError('官方已更新该权益，请刷新后再选择。', 'MEMBER_OFFER_NOT_FOUND', 404)
            if benefit['kind'] == '礼包':
                if sku_id is not None:
                    raise MemberBenefitsError('请从星巴克专页选择要领取的权益。', 'MEMBER_OFFER_NOT_FOUND', 404)
                coupon = {
                    'name': '星巴克 8 元优惠券 × 3', 'url': STARBUCKS_COUPONS,
                    'detail': '到账后 30 天内有效。首月主动领取，第 2 至 6 个月自动发放，第 7 个月起需主动领取。',
                    **self._starbucks_status(login_id),
                }
                data_package = {'name': '1 GB 通用流量日包', 'url': None, 'detail': '星巴克联合会员每月可领。'}
                try:
                    eligibility = self._check_sku(session, STARBUCKS_DATA_ACTIVITY, STARBUCKS_DATA_SKU)
                    data_package['available'] = eligibility['eligible']
                    if not eligibility['eligible']:
                        data_package['status_label'] = eligibility['message']
                except MemberBenefitsError as error:
                    data_package['status_label'] = str(error)
                return {
                    'offers': [], 'can_check': False, 'url': STARBUCKS,
                    'details': {
                        'message': coupon.get('status_message', ''),
                        'options': [coupon, data_package],
                    },
                }
            return self._benefit_offers(session, benefit, member_id, sku_id)

    def _benefit_offers(self, session, benefit, member_id, sku_id=None, *, recover=True, include_rule=False):
        source = benefit['_source']
        activity_id, folder_id = source['activity_id'], source['folder_id']
        raw_offers = source['offers']
        campus = benefit.get('claim_flow') == 'campus'
        platinum = campus and member_id == PLATINUM_MEMBER
        discount = benefit['kind'] == '五折购'
        rule_code = None
        if (campus or discount) and activity_id:
            params = {'activityId': activity_id, 'selfChannelCode': CAMPUS_CHANNEL,
                      'pcFlag': 0, 'joinActType': 3, 'memberId': member_id, 'from': 'page'}
            referer = None
            if platinum:
                referer = PLATINUM_ENTRY
                params['strategyGroupCode'] = PLATINUM_STRATEGY_GROUP
            elif discount:
                page = urlsplit(benefit['url'])
                if page.netloc == urlsplit(ORIGIN).netloc:
                    referer = benefit['url']
                    params['selfChannelCode'] = dict(parse_qsl(page.query)).get('channelCode') or CAMPUS_CHANNEL
                    if page.path.endswith('/halfprize'):
                        params['strategyGroupCode'] = DISCOUNT_STRATEGY_GROUP
            data = self._read(session, ACTIVITY_OFFERS, recover=recover, referer=referer, **params)
            raw_offers = data.get('list') if isinstance(data, dict) else None
            if (platinum or discount) and not isinstance(raw_offers, list):
                raise MemberBenefitsError('官方未返回活动商品清单，请刷新后重试。', 'MEMBER_FORMAT')
            if platinum and include_rule:
                rule_code = data.get('ruleCode')
        elif folder_id:
            raw_offers = self._read(session, GROUP_OFFERS, groupId=folder_id)
        elif not raw_offers:
            data = self._read(session, ACTIVITY_OFFERS, activityId=activity_id)
            raw_offers = data.get('list') if isinstance(data, dict) else None
        if raw_offers is None:
            raw_offers = []
        if not isinstance(raw_offers, list):
            raise MemberBenefitsError('官方没有返回可识别的权益清单。', 'MEMBER_FORMAT')
        offers = []
        for offer in raw_offers:
            offer_id = _public_id(offer.get('skuid'))
            if offer_id and offer_id in source['ordered_ids']:
                continue
            title = str(offer.get('showName') or offer.get('name') or '')
            if title:
                option = {'id': offer_id, 'name': title[:120], 'price_cents': _count(offer.get('price'))}
                if campus:
                    option['can_claim'] = bool(offer_id.isdigit() and option['price_cents'] == 0
                                               and str(offer.get('paytype')) == '1'
                                               and str(offer.get('joinStatus')) != '3'
                                               and str(offer.get('qualificationResult')) != '2'
                                               and offer.get('equityBindAccountType') in (None, '', 0, '0', 17, '17'))
                offers.append(option)
        can_check = bool(activity_id and (discount or benefit['kind'] == '福袋'
                                          and source['mark_type'] in ('1', '2', '6', '7'))
                         and not benefit['claimed'] and benefit['remaining'] != 0)
        result = {'offers': offers, 'can_check': can_check, 'url': benefit['url']}
        if rule_code:
            result['_rule_code'] = rule_code
        if sku_id is not None:
            if not can_check or not any(offer['id'] == sku_id for offer in offers):
                raise MemberBenefitsError('该商品不在当前权益的可核对清单中。', 'MEMBER_OFFER_NOT_FOUND', 404)
            if discount and urlsplit(benefit['url']).netloc == urlsplit(ORIGIN).netloc:
                session.headers['Referer'] = benefit['url']
            result['eligibility'] = self._check_sku(session, activity_id, sku_id, memberId=member_id)
            if discount and result['eligibility']['eligible']:
                result['eligibility']['message'] = '官方资格核对通过。'
                detail = self._read(session, PRODUCT_DETAIL, id=sku_id, isNeedDesc='false')
                if not isinstance(detail, dict) or _public_id(detail.get('id')) != sku_id:
                    raise MemberBenefitsError('官方商品详情与所选商品不一致，请刷新后重试。', 'MEMBER_FORMAT')
                result['purchase_preview'] = {
                    'price_cents': _count(detail.get('price')),
                    'agreement_name': str(detail.get('serviceAgreementName') or '业务受理说明')[:120],
                    'agreement': _product_agreement(detail),
                }
        return result


def register_member_benefits(app, credentials, cache, *, recover=None):
    service = MemberBenefits(credentials, cache, recover=recover)
    app.extensions['member_benefits'] = service

    @app.errorhandler(MemberBenefitsError)
    def member_benefits_error(error):
        payload = {'error': str(error), 'code': error.code}
        if error.verification_url:
            payload['verification_url'] = error.verification_url
        if error.resume_available:
            payload['resume_available'] = True
        if error.verification_sms:
            payload['verification_sms'] = True
        return jsonify(payload), error.status

    @app.get('/member-benefits')
    def member_benefits_page():
        return render_template('member_benefits.html')

    @app.get('/api/member-benefits/status')
    def member_benefits_status():
        return jsonify(service.query(request.args.get('login_id')))

    @app.get('/api/member-benefits/offers')
    def member_benefits_offers():
        args = request.args
        return jsonify(service.offers(args.get('login_id'), args.get('member_id'),
                                      args.get('benefit_key'), args.get('sku_id')))

    @app.post('/api/member-benefits/starbucks/sms')
    def member_benefits_starbucks_sms():
        data = request.get_json(silent=True)
        login_id = _public_id(data.get('login_id')) if isinstance(data, dict) else ''
        if not login_id:
            raise MemberBenefitsError('请选择共享账号。', 'MEMBER_ACCOUNT_NOT_FOUND', 400)
        return jsonify(service.starbucks_sms(login_id))

    @app.post('/api/member-benefits/starbucks/claim')
    def member_benefits_starbucks_claim():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise MemberBenefitsError('请填写领取验证码。', 'MEMBER_SMS_FORMAT', 400)
        return jsonify(service.starbucks_claim(_public_id(data.get('login_id')),
                                               _public_id(data.get('operation_id')),
                                               str(data.get('sms_code') or '')))

    @app.post('/api/member-benefits/campus/<action>')
    def member_benefits_campus(action):
        data = request.get_json(silent=True) or {}
        login_id = _public_id(data.get('login_id')) if isinstance(data, dict) else ''
        if not login_id:
            raise MemberBenefitsError('请选择共享账号。', 'MEMBER_ACCOUNT_NOT_FOUND', 400)
        if action == 'claim':
            return jsonify(service.campus_claim(login_id, _public_id(data.get('operation_id')),
                                                 str(data.get('sms_code') or '')))
        if action == 'resume':
            return jsonify(service.campus_claim(login_id, _public_id(data.get('operation_id')), '', resume=True))
        if action not in ('prepare', 'sms'):
            raise MemberBenefitsError('操作不存在。', 'MEMBER_OFFER_NOT_FOUND', 404)
        sku_id = _public_id(data.get('sku_id'))
        if not sku_id.isdigit():
            raise MemberBenefitsError('请选择要领取的福袋商品。', 'MEMBER_OFFER_NOT_FOUND', 400)
        if action == 'sms' and data.get('agreed') is not True:
            raise MemberBenefitsError('请先阅读并确认商品受理说明。', 'MEMBER_AGREEMENT_REQUIRED', 400)
        if action == 'prepare':
            member_id = _public_id(data.get('member_id', CAMPUS_MEMBER))
            if member_id not in (CAMPUS_MEMBER, PLATINUM_MEMBER):
                raise MemberBenefitsError('请选择当前可在本地领取的会员福袋。', 'MEMBER_OFFER_NOT_FOUND', 404)
            return jsonify(service.campus_prepare(login_id, sku_id, member_id=member_id))
        return jsonify(service.campus_sms(login_id, sku_id, _public_id(data.get('preparation_id'))))

    @app.post('/api/member-benefits/discount/<action>')
    def member_benefits_discount(action):
        data = request.get_json(silent=True) or {}
        login_id = _public_id(data.get('login_id')) if isinstance(data, dict) else ''
        if not login_id:
            raise MemberBenefitsError('请选择共享账号。', 'MEMBER_ACCOUNT_NOT_FOUND', 400)
        if action == 'claim':
            return jsonify(service.discount_claim(login_id, _public_id(data.get('operation_id')),
                                                   str(data.get('sms_code') or '')))
        if action == 'resume':
            return jsonify(service.discount_claim(login_id, _public_id(data.get('operation_id')), '', resume=True))
        if action not in ('prepare', 'sms'):
            raise MemberBenefitsError('操作不存在。', 'MEMBER_OFFER_NOT_FOUND', 404)
        sku_id = _public_id(data.get('sku_id'))
        if not sku_id.isdigit():
            raise MemberBenefitsError('请选择要订购的五折商品。', 'MEMBER_OFFER_NOT_FOUND', 400)
        if action == 'sms':
            if data.get('agreed') is not True:
                raise MemberBenefitsError('请先确认价格和商品受理说明。',
                                          'MEMBER_AGREEMENT_REQUIRED', 400)
            return jsonify(service.discount_sms(
                login_id, sku_id, _public_id(data.get('preparation_id'))))
        member_id = _public_id(data.get('member_id'))
        benefit_key = _public_id(data.get('benefit_key'))
        if not member_id or not benefit_key:
            raise MemberBenefitsError('五折购商品信息已失效，请刷新后重试。',
                                      'MEMBER_OFFER_NOT_FOUND', 400)
        return jsonify(service.discount_prepare(login_id, member_id, benefit_key, sku_id))

    @app.get('/api/member-benefits/suixin')
    def member_benefits_suixin():
        args = request.args
        return jsonify(service.suixin(args.get('login_id'), args.get('product_key')))

    @app.post('/api/member-benefits/suixin/<action>')
    def member_benefits_suixin_action(action):
        data = request.get_json(silent=True)
        login_id = _public_id(data.get('login_id')) if isinstance(data, dict) else ''
        if not login_id:
            raise MemberBenefitsError('请选择共享账号。', 'MEMBER_ACCOUNT_NOT_FOUND', 400)
        if action == 'prepare':
            product_key, offer_id = _public_id(data.get('product_key')), _public_id(data.get('offer_id'))
            if not product_key or not offer_id:
                raise MemberBenefitsError('请选择要领取的随心系商品。', 'MEMBER_OFFER_NOT_FOUND', 400)
            return jsonify(service.suixin_prepare(login_id, product_key, offer_id))
        operation_id = _public_id(data.get('operation_id'))
        if action == 'sms':
            if data.get('agreed') is not True:
                raise MemberBenefitsError('请先阅读并确认领取说明。', 'MEMBER_AGREEMENT_REQUIRED', 400)
            return jsonify(service.suixin_sms(login_id, operation_id))
        if action == 'verification-sms':
            return jsonify(service.suixin_verification_sms(login_id, operation_id))
        if action in ('claim', 'resume', 'verification-claim'):
            return jsonify(service.suixin_claim(login_id, operation_id, str(data.get('sms_code') or ''),
                                               resume=action == 'resume', verification_sms=action == 'verification-claim'))
        raise MemberBenefitsError('操作不存在。', 'MEMBER_OFFER_NOT_FOUND', 404)
