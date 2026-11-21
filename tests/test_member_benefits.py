from base64 import b64decode
from hashlib import md5
import json
import unittest
from unittest.mock import Mock, patch

from flask import Flask
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from credential_cache import CredentialCache
from http_transport import RequestException, requests
from member_benefits_protocol import CampusProtocolError
from member_benefits import (
    ACTIVITY_OFFERS, AUTH, CAMPUS_ACTIVITY, CAMPUS_CHANNEL, CAMPUS_MEMBER,
    CAMPUS_ORDER, CAMPUS_SMS, CATALOG, DISCOUNT_STRATEGY_GROUP, ELIGIBILITY, GROUP_OFFERS, PRODUCT_DETAIL, PRODUCTS, STARBUCKS,
    PLATINUM_ACTIVITY, PLATINUM_ENTRY, PLATINUM_MEMBER, PLATINUM_STRATEGY_GROUP,
    STARBUCKS_COUPON_BUSINESS_TYPE, STARBUCKS_COUPON_RULE, STARBUCKS_COUPONS,
    STARBUCKS_DATA_ACTIVITY, STARBUCKS_DATA_SKU, STARBUCKS_ORDER_KEY, SUIXIN_PORTAL, SUIXIN_PUBLIC,
    SUIXIN_ENTRY, SUIXIN_ORDER, SUIXIN_VERIFICATION_SMS,
    MemberBenefits, MemberBenefitsError, _list_members, register_member_benefits,
)


class MemberBenefitsTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.auth_count = 0
        self.starbucks_ticket = Mock(return_value=(200, {'retCode': '000000', 'rspBody': {
            'token': 'starbucks-source-ticket'}}, {}))
        self.product_errors = []
        self.verification = False
        self.suixin_auth_code = '0'
        self.suixin_query_code = '0'
        self.suixin_sku_state = '1'
        self.suixin_order_response = {'code': '0'}
        self.suixin_risk_state = 'BLOCK'
        self.suixin_payloads = []
        self.starbucks_data_code = '0'
        self.starbucks_coupon_code = '0'
        self.starbucks_coupon_errors = []
        self.starbucks_coupon_state = '1'
        self.starbucks_sms_response = {'code': '0'}
        self.starbucks_order_response = {'code': '0', 'data': {'goodsName': '星巴克24元会员礼包'}}
        self.starbucks_order_confirms = True
        self.starbucks_nonces = []
        self.campus_order_response = {'resultCode': '0', 'data': 'fixture-order'}
        self.campus_order_http_status = 200
        self.campus_order_confirms = True
        self.before_campus_order = lambda: None
        self.discount_price = 1500
        self.discount_activity_offers = [
            {'skuid': 2192, 'name': '已选权益', 'price': 1250},
            {'skuid': 2191, 'name': '咪咕视频月卡', 'price': 1500},
        ]
        self.campus_device_response = {'code': '0', 'data': 'campus-device-fixture'}
        self.campus_device_requests = []
        self.campus_order_headers = []
        self.suixin_offer = {'bizType': 's1', 'isOrder': 0,
                             'receiveUrl': 'https://dev.coc.10086.cn/test?channelCode=public&token=private-token'}
        self.catalog = {'vipInfoList': [
            {'memberId': 1297, 'vipName': '中国移动×星巴克联合会员', 'activityInfoList': [
                {'activityShowName': '星巴克礼包', 'status': 1}]},
            {'memberId': 1055, 'vipName': '铂金会员（校园版）', 'activityInfoList': [
                {'activityShowName': '权益福袋', 'monthRemainTime': 1, 'status': 1}]},
        ]}

        def session_factory():
            session = requests.Session()

            def request(method, url, **kwargs):
                self.calls.append((method, url))
                if url.endswith(AUTH):
                    self.assertEqual(kwargs['json']['ticketType'], 11)
                    self.assertIn(kwargs['json']['ticket'], ('JSESSIONID=fake', 'starbucks-source-ticket'))
                    self.assertEqual(kwargs['json']['jsid'], 'JSESSIONID=issued-for-source'
                                     if kwargs['json']['ticket'] == 'starbucks-source-ticket' else 'JSESSIONID=fake')
                    if kwargs['json']['businessType'] == 'NEW_RIGHTS_PORTAL':
                        result = {'code': self.suixin_auth_code, 'data': {
                            'token': 'suixin-private-token', 'mobileMask': '138****0000'}}
                    else:
                        self.auth_count += 1
                        result = {'code': 10} if self.verification else {
                            'code': 0, 'data': {'token': f'private-token-{self.auth_count}', 'mobileMask': '138****0000'}}
                elif url.endswith(CATALOG):
                    self.assertEqual(session.headers['phone'], f'private-token-{self.auth_count}')
                    result = {'code': 0, 'data': self.catalog}
                elif url.endswith(GROUP_OFFERS):
                    self.assertEqual(kwargs['params']['groupId'], '9172')
                    result = {'resultCode': 0, 'data': [
                        {'skuid': 88, 'name': '样例权益', 'price': 0},
                        {'skuid': 89, 'name': '已选权益', 'price': 0},
                    ]}
                elif url.endswith(ELIGIBILITY):
                    params = {key: value for key, value in kwargs['params'].items() if key != 't'}
                    if params['activityId'] == STARBUCKS_DATA_ACTIVITY:
                        self.assertEqual(params, {'activityId': STARBUCKS_DATA_ACTIVITY, 'skuId': STARBUCKS_DATA_SKU})
                        result = {'resultCode': self.starbucks_data_code, 'data': [{'skuResultCode': '0'}]}
                    elif params['activityId'] == CAMPUS_ACTIVITY:
                        self.assertEqual(params, {'activityId': CAMPUS_ACTIVITY, 'skuId': '27287', 'memberId': CAMPUS_MEMBER})
                        result = {'resultCode': 0, 'data': [{'skuId': 27287, 'skuResultCode': '0'}]}
                    elif params['activityId'] == PLATINUM_ACTIVITY:
                        self.assertEqual(params, {'activityId': PLATINUM_ACTIVITY, 'skuId': '1089',
                                                  'memberId': PLATINUM_MEMBER})
                        self.assertEqual(session.headers['Referer'], PLATINUM_ENTRY)
                        result = {'resultCode': 0, 'data': [{'skuId': 1089, 'skuResultCode': '0'}]}
                    elif params['activityId'] == '10163':
                        self.assertIn(params['memberId'], ('1447', '1452'))
                        self.assertEqual(params['skuId'], '2191')
                        self.assertTrue(session.headers['Referer'].endswith('?memberId=' + params['memberId']))
                        result = {'resultCode': 0, 'data': [
                            {'skuId': 9999, 'skuResultCode': '3'},
                            {'skuId': 2191, 'skuResultCode': '0', 'skuResultMsg': 'success'}]}
                    else:
                        self.assertEqual(params, {'activityId': '227', 'skuId': '88', 'memberId': '1055'})
                        result = {'resultCode': 0, 'data': [{'skuResultCode': '3', 'skuResultMsg': '资格不足'}]}
                elif url.endswith(ACTIVITY_OFFERS):
                    params = {key: value for key, value in kwargs['params'].items() if key != 't'}
                    self.assertEqual(params['selfChannelCode'], CAMPUS_CHANNEL)
                    rule_code = None
                    if params['activityId'] == '10163':
                        self.assertIn(params['memberId'], ('1447', '1452'))
                        self.assertEqual(params['strategyGroupCode'], DISCOUNT_STRATEGY_GROUP)
                        self.assertEqual((params['pcFlag'], params['joinActType'], params['from']), (0, 3, 'page'))
                        self.assertTrue(kwargs['headers']['Referer'].endswith('?memberId=' + params['memberId']))
                        offers = self.discount_activity_offers
                    elif params['activityId'] == PLATINUM_ACTIVITY:
                        self.assertEqual(params, {'activityId': PLATINUM_ACTIVITY, 'selfChannelCode': CAMPUS_CHANNEL,
                                                  'pcFlag': 0, 'joinActType': 3, 'from': 'page',
                                                  'memberId': PLATINUM_MEMBER, 'strategyGroupCode': PLATINUM_STRATEGY_GROUP})
                        self.assertEqual(kwargs['headers']['Referer'], PLATINUM_ENTRY)
                        offers = [{'skuid': 1089, 'showName': '酷我音乐月卡', 'price': 0, 'paytype': 1, 'joinStatus': 0}]
                        rule_code = 'fixture-platinum-offer-rule'
                    else:
                        offers = [
                            {'skuid': 27287, 'name': '咪咕视频钻石会员月卡', 'price': 0, 'paytype': 1, 'joinStatus': 0},
                            {'skuid': 8, 'name': '非免费商品', 'price': 100, 'paytype': 1, 'joinStatus': 0},
                        ]
                    result = {'resultCode': 0, 'data': {'list': offers, 'ruleCode': rule_code}}
                elif url.endswith('/checkActivityMutex'):
                    result = {'resultCode': 0, 'data': {'status': 0}}
                elif url.endswith('/getProductDetailById'):
                    if str(kwargs['params']['id']) == '2191':
                        result = {'code': 0, 'data': {'id': 2191, 'price': self.discount_price, 'originPrice': 3000,
                            'paytype': 1, 'newSku': False, 'accountType': None,
                            'priceExplain': '按次计费，订购后立即生效。'}}
                    else:
                        result = {'code': 0, 'data': {'id': int(kwargs['params']['id']), 'price': 0, 'paytype': 1,
                                                     'priceExplain': '一次性领取，不自动续订。'}}
                elif url.endswith('/checkBlacklist'):
                    result = {'resultCode': 0, 'data': {'isBlackList': 0}}
                elif url.endswith(CAMPUS_SMS):
                    body = kwargs['json']
                    self.assertEqual(body['receivers'], session.headers['phone'])
                    self.assertTrue(body['member'])
                    self.assertEqual(body['businessChannelCode'], CAMPUS_CHANNEL)
                    cipher = Cipher(algorithms.AES(b'72fc912ce498430d'), modes.CBC(b'72fc912ce498430d')).decryptor()
                    self.assertRegex(cipher.update(b64decode(body['skuId'])).rstrip(b'\0').decode(),
                                     r'^(?:27287|2191|1089)_\d{13}$')
                    session.cookies.set('campus-sms', 'fixture')
                    result = {'resultCode': 0}
                elif url.endswith(CAMPUS_ORDER):
                    body = kwargs['json']
                    self.assertEqual(session.cookies.get('campus-sms'), 'fixture')
                    self.assertEqual(body['skuId'], 27287)
                    self.assertEqual(body['memberId'], CAMPUS_MEMBER)
                    self.assertEqual(body['activityId'], int(CAMPUS_ACTIVITY))
                    self.assertEqual(body['referer'], 'zeromzone')
                    self.assertEqual(body['operDfp'], 'fixture+device')
                    cipher = Cipher(algorithms.AES(b'a4jdiq597p3gls0r'), modes.CBC(b'a4jdiq597p3gls0r')).decryptor()
                    self.assertEqual(cipher.update(b64decode(body['smsCode'])).rstrip(b'\0'), b'0123')
                    self.assertEqual(len(json.loads(kwargs['headers']['wasmParams'])), 2)
                    self.assertEqual(kwargs['headers']['gr-risk-p3'], 'official-risk-token')
                    self.campus_order_headers.append(kwargs['headers'])
                    if self.campus_order_http_status != 200:
                        return Mock(status_code=self.campus_order_http_status)
                    if self.campus_order_response is None:
                        raise RequestException('fixture timeout')
                    result = self.campus_order_response
                    if str(result.get('resultCode', result.get('code'))) == '0' and self.campus_order_confirms:
                        self.catalog['vipInfoList'][1]['activityInfoList'][0].update(status=2, orderSkuList=[27287], monthRemainTime=0)
                elif url.endswith(PRODUCTS + 'bizInfo/get'):
                    self.assertEqual(kwargs['json']['token'], f'private-token-{self.auth_count}')
                    result = ({'code': self.product_errors.pop(0)} if self.product_errors else
                              {'code': '0', 'data': [{'bizType': 's1', 'bizName': '全网随心系'}]})
                elif url.endswith(PRODUCTS + 'goodsInfo/get'):
                    self.assertEqual(kwargs['json']['bizTypeList'], ['s1'])
                    result = {'code': '0', 'data': [self.suixin_offer]}
                elif url.endswith(SUIXIN_PUBLIC + 'checkTokenAndBusinessType'):
                    self.assertEqual(kwargs['json']['businessType'], STARBUCKS_COUPON_BUSINESS_TYPE)
                    self.assertEqual(kwargs['headers']['token'], 'suixin-private-token')
                    result = {'code': (self.starbucks_coupon_errors.pop(0) if self.starbucks_coupon_errors
                                       else self.starbucks_coupon_code)}
                elif url.endswith(SUIXIN_PUBLIC + 'getActIds'):
                    self.assertNotIn('phone', session.headers)
                    self.assertEqual(session.headers['innerToken'], 'suixin-private-token')
                    self.assertEqual(kwargs['headers']['token'], 'suixin-private-token')
                    self.assertIn(kwargs['json']['businessType'], ('8', STARBUCKS_COUPON_BUSINESS_TYPE))
                    self.assertEqual(kwargs['json']['token'], 'suixin-private-token')
                    result = {'code': self.suixin_query_code, 'data': [
                        {'actId': '4', 'actName': '随心看', 'corGoodsId': 'cor-goods-a'}]}
                elif url.endswith(SUIXIN_PUBLIC + 'geRightsList'):
                    self.assertEqual(kwargs['json']['actId'], '4')
                    if kwargs['json']['businessType'] == STARBUCKS_COUPON_BUSINESS_TYPE:
                        result = {'code': '0', 'data': {'ruleChainId': STARBUCKS_COUPON_RULE, 'floorInfoList': [{
                            'currentRemainNum': int(self.starbucks_coupon_state == '1'), 'goodsList': [
                                {'goodsName': '星巴克24元会员礼包', 'goodsOrderStat': self.starbucks_coupon_state,
                                 'discountPrice': '0.00', 'goodsId': 1234567890, 'skuId': '42'},
                            ]}]}}
                    else:
                        result = {'code': '0', 'data': {'floorInfoList': [{'floorName': '随心享生活包',
                            'currentRemainNum': self.suixin_offer['currentRemainNum'], 'goodsList': [
                                {'goodsName': '和包出行20元体验会员', 'goodsOrderStat': '2', 'discountPrice': '0.00'},
                                {'goodsName': '样例月卡', 'goodsOrderStat': self.suixin_sku_state, 'discountPrice': '0.00',
                                 'goodsId': 'sku-goods-a', 'skuId': 56986, 'accountType': 'MOBILE',
                                 'goodsDescrip': '<p>免费发放至本机号码。</p>'},
                            ]}]}}
                elif url.endswith(SUIXIN_PUBLIC + 'sms/sendSmsCode'):
                    if kwargs['json']['businessType'] == '8':
                        self.assertEqual(kwargs['json'], {'businessType': '8', 'sceneType': 'COMMON_ORDER',
                            'goodsId': 'sku-goods-a', 'skuId': 56986, 'token': 'suixin-private-token',
                            'frontOrderUrl': SUIXIN_ENTRY, 'channelId': '', 'from': ''})
                        session.cookies.set('suixin-sms', 'fixture')
                        return Mock(status_code=200, json=lambda: {'code': '0'})
                    self.assert_starbucks_headers(session, kwargs['headers'])
                    self.assertEqual(kwargs['json'], {
                        'businessType': '1335', 'sceneType': 'COMMON_ORDER', 'goodsId': '1234567890',
                        'skuId': 42, 'goodsName': '星巴克24元会员礼包', 'token': 'suixin-private-token',
                        'frontOrderUrl': STARBUCKS_COUPONS, 'channelId': '', 'from': '',
                    })
                    session.cookies.set('order-session', 'sms-session')
                    result = self.starbucks_sms_response
                elif url.endswith(SUIXIN_PUBLIC + 'orderRightsWithSmsByEncrypt'):
                    self.assert_starbucks_headers(session, kwargs['headers'])
                    self.assertEqual(set(kwargs['json']), {'ciphertext'})
                    decryptor = Cipher(algorithms.AES(STARBUCKS_ORDER_KEY), modes.ECB()).decryptor()
                    unpadder = padding.PKCS7(128).unpadder()
                    padded = decryptor.update(b64decode(kwargs['json']['ciphertext'], validate=True)) + decryptor.finalize()
                    self.assertEqual(json.loads(unpadder.update(padded) + unpadder.finalize()), {
                        'actName': None, 'accountType': 'MOBILE', 'goodsName': '星巴克24元会员礼包',
                        'goodsId': '1234567890', 'skuId': 42, 'actId': '4', 'corGoodsId': 'cor-goods-a',
                        'businessType': '1335', 'userId': '', 'smsCode': '0234', 'token': 'suixin-private-token',
                        'frontOrderUrl': STARBUCKS_COUPONS, 'sellerId': '', 'sellerMobile': '',
                        'operDfp': '', 'channelId': '', 'from': '',
                    })
                    self.assertEqual(session.cookies.get('order-session'), 'sms-session')
                    if self.starbucks_order_response is None:
                        raise RequestException('fixture timeout')
                    result = self.starbucks_order_response
                    if result['code'] == '0' and self.starbucks_order_confirms:
                        self.starbucks_coupon_state = '2'
                elif url.endswith(SUIXIN_VERIFICATION_SMS):
                    self.assertEqual(session.cookies.get('suixin-sms'), 'fixture')
                    self.assertEqual(kwargs['json'], {'businessType': '8', 'token': 'suixin-private-token',
                        'frontOrderUrl': SUIXIN_ENTRY, 'channelId': '', 'from': ''})
                    session.cookies.set('suixin-verification', 'fixture')
                    result = {'code': '0'}
                elif url.endswith(SUIXIN_ORDER):
                    self.assertEqual(session.cookies.get('suixin-sms'), 'fixture')
                    headers = kwargs['headers']
                    nonce = headers['randomstr']
                    self.assertRegex(nonce, r'^\d+&\*$')
                    signed = f"{headers['timestamp']}7C9408E2E22ED1329A72837709851954{nonce}"
                    self.assertEqual(headers['signature'], md5(signed.encode('ascii')).hexdigest())
                    decryptor = Cipher(algorithms.AES(STARBUCKS_ORDER_KEY), modes.ECB()).decryptor()
                    unpadder = padding.PKCS7(128).unpadder()
                    padded = decryptor.update(b64decode(kwargs['json']['ciphertext'], validate=True)) + decryptor.finalize()
                    self.suixin_payloads.append(json.loads(unpadder.update(padded) + unpadder.finalize()))
                    if self.suixin_order_response is None:
                        raise RequestException('fixture timeout')
                    result = self.suixin_order_response
                    if result['code'] == '0':
                        self.suixin_sku_state = '2'
                elif url.endswith(SUIXIN_PUBLIC + 'risk/statusSync'):
                    self.assertEqual(kwargs['json']['businessUniqueId'], 'fixture-risk')
                    result = {'code': '0', 'data': {'riskStatus': self.suixin_risk_state}}
                elif url.endswith(SUIXIN_PUBLIC + 'getActIdsNoLogin'):
                    self.assertNotIn('phone', session.headers)
                    self.assertNotIn('token', kwargs['json'])
                    result = {'code': '0', 'data': [{'actId': '4', 'actName': '随心看'}]}
                elif url.endswith(SUIXIN_PUBLIC + 'geRightsListNoLogin'):
                    self.assertNotIn('phone', session.headers)
                    self.assertEqual(kwargs['json']['actId'], '4')
                    result = {'code': '0', 'data': {'floorInfoList': [
                        {'goodsList': [{'goodsName': '样例月卡', 'goodsOrderStat': '1'}]}]}}
                else:
                    self.fail('Unexpected upstream request')
                return Mock(status_code=200, json=lambda: result)

            session.request = request
            return session

        owner = self
        class CampusProtocol:
            def prepare(self, cookies, token, user_agent, entry, *, order=None, before_submit=None):
                owner.assertEqual(next(cookie.value for cookie in cookies if cookie.name == 'campus-sms'), 'fixture')
                owner.assertEqual(token, f'private-token-{owner.auth_count}')
                owner.assertRegex(entry, r'/online/(?:zero|zeromzone|halfprize)\?')
                owner.campus_device_requests.append({'token': token, 'user_agent': user_agent, 'entry': entry})
                result = owner.campus_device_response
                device = result.get('data') if isinstance(result, dict) and str(result.get('code')) == '0' else ''
                if not device:
                    raise CampusProtocolError('官方设备 SDK 未返回校验参数。')
                value = {'risk_token': 'official-risk-token',
                         'wasm_params': ['official-wasm-key', device], 'device': 'fixture%2Bdevice', 'cookies': [{
                    'name': 'BSFIT_DEVICEID', 'value': 'fixture%2Bdevice',
                    'domain': 'dev.coc.10086.cn', 'path': '/', 'secure': True,
                }]}
                if order is not None:
                    if order['skuId'] == 27287:
                        owner.assertNotIn('memberId', order)
                        owner.assertEqual(order['activityId'], int(CAMPUS_ACTIVITY))
                        owner.assertEqual(order['referer'], 'zeromzone')
                    elif order['skuId'] == 1089:
                        owner.assertEqual(entry, PLATINUM_ENTRY)
                        owner.assertEqual(order['memberId'], PLATINUM_MEMBER)
                        owner.assertEqual(order['activityId'], int(PLATINUM_ACTIVITY))
                        owner.assertEqual(order['referer'], 'zero')
                        owner.assertEqual(order['ruleCode'], 'fixture-platinum-offer-rule')
                        owner.assertTrue(order['receipt'] and order['faultyReceipt'])
                    else:
                        owner.assertEqual(order['skuId'], 2191)
                        owner.assertIn(order['memberId'], ('1447', '1452'))
                        owner.assertTrue(entry.endswith('?memberId=' + order['memberId']))
                        owner.assertEqual(order['activityId'], 10163)
                        owner.assertEqual(order['referer'], 'halfprize')
                    cipher = Cipher(algorithms.AES(b'a4jdiq597p3gls0r'), modes.CBC(b'a4jdiq597p3gls0r')).decryptor()
                    owner.assertEqual(cipher.update(b64decode(order['smsCode'])).rstrip(b'\0'), b'0123')
                    before_submit()
                    owner.before_campus_order()
                    owner.calls.append(('POST', CAMPUS_ORDER))
                    owner.campus_order_headers.append({
                        'wasmParams': json.dumps(value['wasm_params']), 'gr-risk-p3': value['risk_token']})
                    if owner.campus_order_response is None:
                        raise CampusProtocolError('fixture timeout', uncertain=True)
                    value['order'] = {'status': owner.campus_order_http_status,
                                      'body': owner.campus_order_response}
                    if (owner.campus_order_http_status == 200 and owner.campus_order_confirms
                            and str(owner.campus_order_response.get('resultCode')) == '0'):
                        for member in owner.catalog['vipInfoList']:
                            if str(member['memberId']) != order.get('memberId', CAMPUS_MEMBER):
                                continue
                            for activity in member['activityInfoList']:
                                if str(activity.get('activityId')) == str(order['activityId']):
                                    activity.update(status=2, orderSkuList=[order['skuId']], monthRemainTime=0)
                return value

        self.service = MemberBenefits(lambda _id: {'phone': '13800000000', 'cookie': 'JSESSIONID=fake'},
                                      CredentialCache(), session_factory=session_factory,
                                      app_factory=lambda _: Mock(call=self.starbucks_ticket,
                                                                 cookie='JSESSIONID=issued-for-source'),
                                      campus_protocol=CampusProtocol())

    def assert_starbucks_headers(self, session, headers):
        self.assertEqual(session.headers['Referer'], STARBUCKS_COUPONS)
        self.assertEqual(headers['innerToken'], session.headers['innerToken'])
        self.assertEqual(headers['token'], 'suixin-private-token')
        self.assertEqual(headers['Content-Type'], 'application/json;charset=UTF-8')
        self.assertEqual(headers['Accept'], 'application/json, text/plain, */*')
        self.assertRegex(headers['timestamp'], r'^[0-9]{13}$')
        nonce = headers['randomstr']
        self.assertRegex(nonce, r'^[a-f0-9]{16}$')
        self.assertNotIn(nonce, self.starbucks_nonces)
        self.starbucks_nonces.append(nonce)
        signed = f"{headers['timestamp']}7C9408E2E22ED1329A72837709851954{nonce}"
        self.assertEqual(headers['signature'], md5(signed.encode('ascii')).hexdigest())

    def add_discount_member(self, member_id=1447, name='黔心选铂金会员'):
        self.catalog['vipInfoList'].append({
            'memberId': member_id, 'vipName': name,
            'activityInfoList': [{'activityId': 10163, 'activityShowName': '五折购1款', 'markType': 4,
                                  'status': 1, 'monthRemainTime': 1,
                                  'activityUrl': 'https://dev.coc.10086.cn/coc3/canvas/rightsmarket-h5-canvas/online/'
                                                 f'halfprize?memberId={member_id}'}],
            'activityProductList': [{'aid': 10163, 'skuid': 2191, 'name': '咪咕视频钻石会员月卡',
                                     'price': 1500, 'paytype': 1, 'joinStatus': 0}],
        })
        return self.service.query('login-a')['members'][-1]['benefits'][0]

    def test_query_reuses_session_and_returns_official_entry_without_credentials(self):
        first = self.service.query('login-a')
        second = self.service.query('login-a')
        self.assertEqual(first, second)
        self.assertEqual(sum(url.endswith(AUTH) for _, url in self.calls), 2)
        self.assertEqual(sum(url.endswith('geRightsList') for _, url in self.calls), 2)
        self.assertEqual([row['id'] for row in first['members']], ['1297', '1055'])
        self.assertEqual(first['members'][0]['benefits'][0]['url'], STARBUCKS)
        self.assertEqual(first['products'][0]['key'], 's1')
        self.assertNotIn('url', first['products'][0])
        self.assertNotIn('private-token', str(first))

    def test_official_verification_stops_without_querying_catalog(self):
        self.verification = True
        with self.assertRaises(MemberBenefitsError) as raised:
            self.service.query('login-a')
        self.assertEqual(raised.exception.code, 'MEMBER_VERIFICATION')
        self.assertEqual(len(self.calls), 1)

    def test_product_login_error_recovers_before_reading_member_catalogue(self):
        self.product_errors = ['100002']
        result = self.service.query('login-a')
        self.assertEqual(self.auth_count, 2)
        self.assertEqual([row['id'] for row in result['members']], ['1297', '1055'])
        self.assertEqual(result['products'][0]['key'], 's1')
        self.assertEqual(sum(url.endswith(CATALOG) for _, url in self.calls), 1)

    def test_repeated_product_login_error_does_not_show_guest_catalogue(self):
        self.product_errors = ['100002', '100002']
        with self.assertRaises(MemberBenefitsError) as raised:
            self.service.query('login-a')
        self.assertEqual((raised.exception.status, raised.exception.code), (401, 'MEMBER_SESSION_EXPIRED'))
        self.assertEqual(self.auth_count, 2)
        self.assertFalse(any(url.endswith(CATALOG) for _, url in self.calls))

    def test_duplicate_member_products_keep_their_own_quota_and_price(self):
        members = _list_members({'vipInfoList': [
            {'memberId': 1447, 'vipCode': 'CS81180102', 'vipName': '黔心选铂金会员',
             'activityInfoList': [{'activityId': 10163, 'activityShowName': '五折购1款', 'monthRemainTime': 1,
                                   'activityUrl': 'https://dev.coc.10086.cn.evil.test/order'}],
             'activityProductList': [{'aid': 10163, 'name': '测试商品', 'price': 500},
                                     {'aid': 99999, 'name': '其他权益', 'price': 0}]},
            {'memberId': 1452, 'vipCode': 'CS81180102', 'vipName': '黔心选商超会员',
             'activityInfoList': [{'activityShowName': '权益福袋', 'status': 2, 'monthRemainTime': 0}]},
        ]})
        self.assertEqual([row['id'] for row in members], ['1447', '1452'])
        offer = members[0]['benefits'][0]
        self.assertEqual(offer['offers'], [{'name': '测试商品', 'price_cents': 500}])
        self.assertIn('memberId=1447', offer['url'])
        self.assertTrue(members[1]['benefits'][0]['claimed'])

    def test_group_check_uses_the_selected_member_and_excludes_already_ordered_sku(self):
        self.catalog['vipInfoList'][1]['activityInfoList'][0].update(
            activityId=227, folderId=9172, markType=1, orderSkuList=[89])
        key = self.service.query('login-a')['members'][1]['benefits'][0]['key']
        data = self.service.offers('login-a', '1055', key, '88')
        self.assertEqual(data['offers'], [{'id': '88', 'name': '样例权益', 'price_cents': 0}])
        self.assertEqual(data['eligibility'], {'eligible': False, 'message': '资格不足'})
        with self.assertRaises(MemberBenefitsError):
            self.service.offers('login-a', '1055', key, '89')
        with self.assertRaises(MemberBenefitsError):
            self.service.offers('login-a', '1297', key, '88')
        self.assertEqual(sum(url.endswith(ELIGIBILITY) for _, url in self.calls), 1)
        self.assertEqual(sum(url.endswith(AUTH) for _, url in self.calls), 2)

    def test_guizhou_gift_bags_show_mini_program_hint_with_official_or_unknown_status(self):
        self.add_discount_member()
        self.add_discount_member(1452, '黔心选商超会员')
        self.catalog['vipInfoList'][-1]['activityInfoList'].append({
            'activityId': 17230, 'activityShowName': '权益福袋', 'status': 1, 'monthRemainTime': 1,
        })
        result = self.service.query('login-a')
        platinum, supermarket = result['members'][-2:]
        platinum_discount, platinum_gift = platinum['benefits']
        self.assertTrue(platinum_discount['selection']['can_check'])
        self.assertIsNone(platinum_gift['remaining'])
        self.assertFalse(platinum_gift['claimed'])
        self.assertEqual(platinum_gift['claim_hint'], '请去黔心选小程序领取该权益')
        self.assertNotIn('selection', platinum_gift)
        self.assertEqual(self.service.availability['login-a']['other_claims'], 2)
        discount, gift = supermarket['benefits']
        self.assertTrue(discount['selection']['can_check'])
        self.assertEqual(gift['claim_hint'], '请去黔心选小程序领取该权益')
        self.assertEqual(gift['remaining'], 1)
        self.assertNotIn('selection', gift)
        with self.assertRaises(MemberBenefitsError) as outside_toolbox:
            self.service.offers('login-a', '1452', gift['key'])
        self.assertEqual(outside_toolbox.exception.status, 404)

        self.catalog['vipInfoList'][-2]['activityInfoList'].append({
            'activityId': 17774, 'activityShowName': '权益福袋', 'status': 2, 'monthRemainTime': 0,
        })
        updated = self.service.query('login-a')['members'][-2]['benefits']
        self.assertEqual([benefit['kind'] for benefit in updated], ['五折购', '福袋'])
        self.assertTrue(updated[1]['claimed'])

    def test_discount_uses_member_and_sku_qualification_then_reads_official_price(self):
        page = 'https://dev.coc.10086.cn/coc3/canvas/rightsmarket-h5-canvas/online/halfprize?memberId=1447'
        self.catalog['vipInfoList'].append({
            'memberId': 1447,
            'activityInfoList': [{'activityId': 10163, 'activityShowName': '五折购1款', 'markType': 4,
                                  'status': 1, 'monthRemainTime': 1, 'activityUrl': page, 'orderSkuList': [2192]}],
            'activityProductList': [{'aid': 10163, 'skuid': sku, 'name': '样例商品', 'price': 1500}
                                    for sku in (2191, 2192)],
        })
        benefit = _list_members(self.catalog)[2]['benefits'][0]
        original_read = self.service._read
        checks = []

        def read(session, path, *, recover=True, referer=None, **params):
            if path in (ELIGIBILITY, PRODUCT_DETAIL):
                self.assertEqual(session.headers['Referer'], page)
                checks.append((path, params))
                if path == ELIGIBILITY:
                    return [{'skuId': 2192, 'skuResultCode': 3}, {'skuId': 2191, 'skuResultCode': 0}]
                return {'id': 2191, 'price': 1400, 'serviceAgreementContent': '<p>按次计费。</p>'}
            return original_read(session, path, recover=recover, referer=referer, **params)

        with patch.object(self.service, '_read', side_effect=read):
            self.assertTrue(self.service.offers('login-a', '1447', benefit['key'])['can_check'])
            self.assertEqual(checks, [])
            with self.assertRaises(MemberBenefitsError):
                self.service.offers('login-a', '1447', benefit['key'], '2192')
            self.assertEqual(checks, [])
            checked = self.service.offers('login-a', '1447', benefit['key'], '2191')

        self.assertEqual(checks, [(ELIGIBILITY, {'activityId': '10163', 'memberId': '1447', 'skuId': '2191'}),
                                  (PRODUCT_DETAIL, {'id': '2191', 'isNeedDesc': 'false'})])
        self.assertTrue(checked['eligibility']['eligible'])
        self.assertEqual(checked['purchase_preview'], {'price_cents': 1400, 'agreement_name': '业务受理说明',
                                                       'agreement': '按次计费。'})

    def test_discount_lists_activity_offers_in_official_order_and_rejects_overview_only_skus(self):
        benefit = self.add_discount_member()
        self.catalog['vipInfoList'][-1]['activityProductList'] = [
            {'aid': 10163, 'skuid': 9999, 'name': '总览摘要商品', 'price': 500},
        ]
        refreshed = self.service.query('login-a')['members'][-1]['benefits'][0]
        self.assertEqual([row['id'] for row in refreshed['selection']['offers']], ['2192', '2191'])
        with self.assertRaises(MemberBenefitsError) as error:
            self.service.discount_prepare('login-a', '1447', benefit['key'], '9999')
        self.assertEqual(error.exception.code, 'MEMBER_OFFER_NOT_FOUND')
        prepared = self.service.discount_prepare('login-a', '1447', benefit['key'], '2191')
        self.assertEqual(prepared['purchase_preview']['price_cents'], 1500)

    def test_android_lists_discount_choices_before_order_validation(self):
        discount = self.add_discount_member()
        with patch.dict('os.environ', LINGXI_ANDROID='1'):
            self.calls.clear()
            members = self.service.query('login-a')['members']
            benefit = next(item for item in members[-1]['benefits'] if item['key'] == discount['key'])
            self.assertEqual([(offer['id'], offer['price_cents']) for offer in benefit['selection']['offers']],
                             [('2192', 1250), ('2191', 1500)])
            self.assertTrue(benefit['local_order_notice'])
            with self.assertRaises(MemberBenefitsError) as error:
                self.service.discount_prepare('login-a', '1447', discount['key'], '2191')
            self.assertEqual(error.exception.code, 'MEMBER_ORDER_UNSUPPORTED')
        self.assertFalse(any(url.endswith((CAMPUS_SMS, CAMPUS_ORDER)) for _, url in self.calls))

    def test_claimed_names_follow_confirmed_skus_and_look_up_each_missing_name_once(self):
        for member_id, name in ((1447, '黔心选铂金会员'), (1452, '黔心选商超会员')):
            self.add_discount_member(member_id, name)
            member = self.catalog['vipInfoList'][-1]
            member['activityInfoList'][0].update(status=2, monthRemainTime=0, orderSkuList=[30180, 30180, 2191])
        original_read = self.service._read
        lookups = []

        def read(session, path, **params):
            if path == PRODUCT_DETAIL:
                lookups.append(params['id'])
                return {'id': 30180, 'name': '和包出行15元体验会员'}
            return original_read(session, path, **params)

        with patch.object(self.service, '_read', side_effect=read):
            members = self.service.query('login-a')['members'][-2:]
        self.assertEqual(lookups, ['30180'])
        for member in members:
            self.assertEqual(member['benefits'][0]['claimed_products'],
                             ['和包出行15元体验会员', '咪咕视频钻石会员月卡'])

    def test_discount_sms_and_order_are_bound_to_confirmed_price_and_product(self):
        benefit = self.add_discount_member()
        prepared = self.service.discount_prepare('login-a', '1447', benefit['key'], '2191')
        self.assertEqual(prepared['purchase_preview']['price_cents'], 1500)
        self.assertIn('按次计费', prepared['purchase_preview']['agreement'])
        sms = self.service.discount_sms('login-a', '2191', prepared['preparation_id'])
        self.assertEqual(sms['operation_id'], self.service.discount_sms(
            'login-a', '2191', prepared['preparation_id'])['operation_id'])
        self.assertEqual(sum(url.endswith(CAMPUS_SMS) for _, url in self.calls), 1)
        with self.assertRaises(MemberBenefitsError):
            self.service.discount_claim('login-b', sms['operation_id'], '0123')
        result = self.service.discount_claim('login-a', sms['operation_id'], '0123')
        self.assertEqual((result['state'], result['claimed'], result['status_label']),
                         ('claimed', True, '已订购'))
        sent = self.campus_device_requests[-1]
        self.assertIn('/online/halfprize?', sent['entry'])
        self.assertEqual(sum(url.endswith(CAMPUS_ORDER) for _, url in self.calls), 1)

    def test_guizhou_discount_cards_with_the_same_activity_and_sku_have_independent_results(self):
        platinum = self.add_discount_member()
        supermarket = self.add_discount_member(1452, '黔心选商超会员')
        self.assertEqual(platinum['key'], supermarket['key'])
        preparations = [self.service.discount_prepare('login-a', member_id, platinum['key'], '2191')
                        for member_id in ('1447', '1452')]
        self.assertNotEqual(preparations[0]['preparation_id'], preparations[1]['preparation_id'])

        sms = self.service.discount_sms('login-a', '2191', preparations[1]['preparation_id'])
        self.assertTrue(self.service.discount_claim('login-a', sms['operation_id'], '0123')['claimed'])
        self.assertTrue(self.campus_device_requests[-1]['entry'].endswith('?memberId=1452'))
        overview = self.service.query('login-a')['members']
        self.assertFalse(overview[-2]['benefits'][0]['claimed'])
        self.assertEqual(overview[-2]['benefits'][0]['remaining'], 1)
        self.assertTrue(overview[-1]['benefits'][0]['claimed'])

        with self.assertRaises(MemberBenefitsError) as cooldown:
            self.service.discount_sms('login-a', '2191', preparations[0]['preparation_id'])
        self.assertEqual(cooldown.exception.code, 'MEMBER_RATE_LIMIT')
        with patch('member_benefits.DISCOUNT_SMS_COOLDOWN', 0):
            sms = self.service.discount_sms('login-a', '2191', preparations[0]['preparation_id'])
        self.assertTrue(self.service.discount_claim('login-a', sms['operation_id'], '0123')['claimed'])
        self.assertTrue(self.campus_device_requests[-1]['entry'].endswith('?memberId=1447'))
        self.assertEqual(sum(url.endswith(CAMPUS_ORDER) for _, url in self.calls), 2)

    def test_discount_price_change_stops_before_sending_sms(self):
        benefit = self.add_discount_member()
        prepared = self.service.discount_prepare('login-a', '1447', benefit['key'], '2191')
        self.discount_price = 1600
        with self.assertRaises(MemberBenefitsError) as raised:
            self.service.discount_sms('login-a', '2191', prepared['preparation_id'])
        self.assertEqual(raised.exception.code, 'MEMBER_PRICE_CHANGED')
        self.assertFalse(any(url.endswith(CAMPUS_SMS) for _, url in self.calls))

    def test_discount_bad_sms_can_be_corrected_with_the_bound_operation(self):
        benefit = self.add_discount_member()
        prepared = self.service.discount_prepare('login-a', '1447', benefit['key'], '2191')
        sms = self.service.discount_sms('login-a', '2191', prepared['preparation_id'])
        self.campus_order_response = {'resultCode': '-1006'}
        with self.assertRaises(MemberBenefitsError) as raised:
            self.service.discount_claim('login-a', sms['operation_id'], '0123')
        self.assertEqual(raised.exception.code, 'MEMBER_SMS_INVALID')
        self.campus_order_response = {'resultCode': '0', 'data': 'fixture-order'}
        result = self.service.discount_claim('login-a', sms['operation_id'], '0123')
        self.assertEqual(result['state'], 'claimed')
        self.assertEqual(sum(url.endswith(CAMPUS_ORDER) for _, url in self.calls), 2)
        self.assertEqual(sum(url.endswith(CAMPUS_SMS) for _, url in self.calls), 1)

    def test_discount_uncertain_order_is_never_replayed(self):
        benefit = self.add_discount_member()
        prepared = self.service.discount_prepare('login-a', '1447', benefit['key'], '2191')
        sms = self.service.discount_sms('login-a', '2191', prepared['preparation_id'])
        self.campus_order_response = None
        with self.assertRaises(MemberBenefitsError) as raised:
            self.service.discount_claim('login-a', sms['operation_id'], '0123')
        self.assertEqual(raised.exception.code, 'MEMBER_CLAIM_PENDING')
        with self.assertRaises(MemberBenefitsError) as repeated:
            self.service.discount_claim('login-a', sms['operation_id'], '0123')
        self.assertEqual(repeated.exception.code, 'MEMBER_CLAIM_STALE')
        self.assertEqual(sum(url.endswith(CAMPUS_ORDER) for _, url in self.calls), 1)

    def test_discount_resumes_original_order_after_official_challenge(self):
        benefit = self.add_discount_member()
        prepared = self.service.discount_prepare('login-a', '1447', benefit['key'], '2191')
        sms = self.service.discount_sms('login-a', '2191', prepared['preparation_id'])
        operation = sms['operation_id']
        self.campus_order_response = {'code': -1, 'data': {'risk': True, 'url':
            'https://dev.coc.10086.cn/coc3/gr/static-2c/risk-verify/#/voiceCaptcha?riskActionLogId=fixture'}}
        with self.assertRaises(MemberBenefitsError) as error:
            self.service.discount_claim('login-a', operation, '0123')
        self.assertTrue(error.exception.resume_available)
        self.assertEqual(sum(url.endswith(CAMPUS_ORDER) for _, url in self.calls), 1)
        with self.assertRaises(MemberBenefitsError) as error:
            self.service.discount_claim('login-b', operation, '', resume=True)
        self.assertEqual(error.exception.code, 'MEMBER_CLAIM_STALE')
        self.campus_order_response = {'resultCode': 0}
        app = Flask(__name__)
        with patch('member_benefits.MemberBenefits', return_value=self.service):
            register_member_benefits(app, self.service.credentials, self.service.cache)
        body = {'login_id': 'login-a', 'operation_id': operation}
        client = app.test_client()
        resumed = client.post('/api/member-benefits/discount/resume', json=body)
        self.assertEqual(resumed.status_code, 200)
        self.assertTrue(resumed.json['claimed'])
        self.assertEqual(client.post('/api/member-benefits/discount/resume', json=body).status_code, 409)
        self.assertEqual(sum(url.endswith(CAMPUS_SMS) for _, url in self.calls), 1)
        self.assertEqual(sum(url.endswith(CAMPUS_ORDER) for _, url in self.calls), 2)

    def test_resume_checks_official_status_before_sending_another_order(self):
        benefit = self.add_discount_member()
        prepared = self.service.discount_prepare('login-a', '1447', benefit['key'], '2191')
        sms = self.service.discount_sms('login-a', '2191', prepared['preparation_id'])
        self.campus_order_response = {'code': -1, 'data': {'risk': True, 'url':
            'https://dev.coc.10086.cn/coc3/gr/static-2c/risk-verify/#/voiceCaptcha?riskActionLogId=fixture'}}
        with self.assertRaises(MemberBenefitsError):
            self.service.discount_claim('login-a', sms['operation_id'], '0123')
        activity = self.catalog['vipInfoList'][-1]['activityInfoList'][0]
        activity.update(status=2, orderSkuList=[2191], monthRemainTime=0)
        result = self.service.discount_claim('login-a', sms['operation_id'], '', resume=True)
        self.assertTrue(result['claimed'])
        self.assertEqual(sum(url.endswith(CAMPUS_ORDER) for _, url in self.calls), 1)

    def test_resume_refuses_a_changed_discount_price_without_submitting(self):
        benefit = self.add_discount_member()
        prepared = self.service.discount_prepare('login-a', '1447', benefit['key'], '2191')
        sms = self.service.discount_sms('login-a', '2191', prepared['preparation_id'])
        self.campus_order_response = {'code': -1, 'data': {'risk': True, 'url':
            'https://dev.coc.10086.cn/coc3/gr/static-2c/risk-verify/#/voiceCaptcha?riskActionLogId=fixture'}}
        with self.assertRaises(MemberBenefitsError):
            self.service.discount_claim('login-a', sms['operation_id'], '0123')
        self.discount_price = 1600
        with self.assertRaises(MemberBenefitsError) as error:
            self.service.discount_claim('login-a', sms['operation_id'], '', resume=True)
        self.assertEqual(error.exception.code, 'MEMBER_PRICE_CHANGED')
        self.assertEqual(sum(url.endswith(CAMPUS_ORDER) for _, url in self.calls), 1)

    def test_campus_resume_stops_after_an_uncertain_submission(self):
        self.catalog['vipInfoList'][1]['activityInfoList'][0].update(activityId=int(CAMPUS_ACTIVITY), markType=0)
        prepared = self.service.campus_prepare('login-a', '27287')
        sms = self.service.campus_sms('login-a', '27287', prepared['preparation_id'])
        self.campus_order_response = {'code': -1, 'data': {'risk': True, 'url':
            'https://dev.coc.10086.cn/coc3/gr/static-2c/risk-verify/#/smsCaptcha?riskActionLogId=fixture'}}
        with self.assertRaises(MemberBenefitsError) as challenge:
            self.service.campus_claim('login-a', sms['operation_id'], '0123')
        self.assertTrue(challenge.exception.resume_available)
        self.campus_order_response = None
        with self.assertRaises(MemberBenefitsError) as unknown:
            self.service.campus_claim('login-a', sms['operation_id'], '', resume=True)
        self.assertEqual(unknown.exception.code, 'MEMBER_CLAIM_PENDING')
        with self.assertRaises(MemberBenefitsError) as repeated:
            self.service.campus_claim('login-a', sms['operation_id'], '', resume=True)
        self.assertEqual(repeated.exception.code, 'MEMBER_CLAIM_STALE')
        self.assertEqual(sum(url.endswith(CAMPUS_ORDER) for _, url in self.calls), 2)

    def test_starbucks_shows_bundle_and_data_even_when_data_check_is_unavailable(self):
        benefit = self.service.query('login-a')['members'][0]['benefits'][0]
        self.assertTrue(benefit['can_inspect'])
        self.calls.clear()
        for code in ('0', '429'):
            with self.subTest(code=code):
                self.starbucks_data_code = code
                result = self.service.offers('login-a', '1297', benefit['key'])
                self.assertEqual(result['url'], STARBUCKS)
                coupon, data = result['details']['options']
                self.assertEqual(coupon['name'], '星巴克 8 元优惠券 × 3')
                self.assertEqual(coupon['url'], STARBUCKS_COUPONS)
                self.assertTrue(coupon['available'])
                self.assertTrue(coupon['can_claim'])
                self.assertEqual(coupon['status_label'], '未领取')
                self.assertEqual(coupon['price'], '0.00')
                if code == '0':
                    self.assertTrue(data['available'])
                else:
                    self.assertIn('频繁', data['status_label'])
        self.assertEqual(sum(url.endswith(CATALOG) for _, url in self.calls), 2)
        self.assertEqual(sum(url.endswith(ELIGIBILITY) for _, url in self.calls), 2)
        self.assertEqual(sum(url.endswith(AUTH) for _, url in self.calls), 0)

    def test_starbucks_overview_reads_official_claim_state_without_opening_dialog(self):
        for official, claimed, label in (('1', False, '未领取'), ('2', True, '已领取'),
                                         (None, None, '领取状态查询失败')):
            with self.subTest(official=official):
                self.starbucks_coupon_state = official
                benefit = self.service.query('login-a')['members'][0]['benefits'][0]
                self.assertIs(benefit['claimed'], claimed)
                self.assertEqual(benefit['status_label'], label)
        self.assertFalse(any(url.endswith('sms/sendSmsCode') for _, url in self.calls))

    def test_campus_claim_binds_agreement_and_sms_to_selected_sku_and_account(self):
        self.catalog['vipInfoList'][1]['activityInfoList'][0].update(activityId=int(CAMPUS_ACTIVITY), markType=0)
        benefit = self.service.query('login-a')['members'][1]['benefits'][0]
        self.assertEqual(benefit['claim_flow'], 'campus')
        self.assertEqual([option['can_claim'] for option in benefit['selection']['offers']], [True, False])
        with self.assertRaises(MemberBenefitsError):
            self.service.campus_prepare('login-a', '8')
        detail = self.service.campus_prepare('login-a', '27287')
        self.assertIn('不自动续订', detail['agreement'])
        with self.assertRaises(MemberBenefitsError):
            self.service.campus_sms('login-b', '27287', detail['preparation_id'])
        with self.assertRaises(MemberBenefitsError):
            self.service.campus_sms('login-a', '8', detail['preparation_id'])
        sms = self.service.campus_sms('login-a', '27287', detail['preparation_id'])
        self.assertEqual(self.campus_device_requests, [])
        self.assertEqual(sms['operation_id'], self.service.campus_sms('login-a', '27287', detail['preparation_id'])['operation_id'])
        self.assertEqual(sum(url.endswith(CAMPUS_SMS) for _, url in self.calls), 1)
        self.assertFalse(any(url.endswith(CAMPUS_ORDER) for _, url in self.calls))
        with self.assertRaises(MemberBenefitsError):
            self.service.campus_claim('login-b', sms['operation_id'], '0123')
        result = self.service.campus_claim('login-a', sms['operation_id'], '0123')
        writes = [url for method, url in self.calls if method == 'POST']
        self.assertTrue(writes[-1].endswith(CAMPUS_ORDER))
        device_request = self.campus_device_requests[0]
        self.assertEqual(device_request['token'], f'private-token-{self.auth_count}')
        params = json.loads(self.campus_order_headers[0]['wasmParams'])
        self.assertEqual(params, ['official-wasm-key', 'campus-device-fixture'])
        self.assertEqual(result['state'], 'claimed')
        self.assertEqual(self.service.available_count({'login-a'}), 1)

    def test_platinum_gift_bag_uses_its_activity_and_keeps_campus_quota_separate(self):
        self.catalog['vipInfoList'][1]['activityInfoList'][0].update(activityId=int(CAMPUS_ACTIVITY))
        self.catalog['vipInfoList'].append({
            'memberId': int(PLATINUM_MEMBER), 'vipName': '铂金会员',
            'activityInfoList': [{'activityId': int(PLATINUM_ACTIVITY), 'activityShowName': '权益福袋',
                                  'status': 1, 'monthRemainTime': 1, 'activityUrl': PLATINUM_ENTRY}],
            'activityProductList': [{'aid': int(PLATINUM_ACTIVITY), 'skuid': 9999, 'name': '总览摘要'}],
        })
        _, campus, platinum = self.service.query('login-a')['members']
        selection = platinum['benefits'][0]['selection']
        self.assertEqual(selection['offers'], [{'id': '1089', 'name': '酷我音乐月卡', 'price_cents': 0, 'can_claim': True}])
        self.assertNotIn('_rule_code', selection)
        count = self.service.available_count({'login-a'})
        with self.assertRaises(MemberBenefitsError):
            self.service.campus_prepare('login-a', '27287', member_id=PLATINUM_MEMBER)
        prepared = self.service.campus_prepare('login-a', '1089', member_id=PLATINUM_MEMBER)
        sms = self.service.campus_sms('login-a', '1089', prepared['preparation_id'])
        self.assertTrue(self.service.campus_claim('login-a', sms['operation_id'], '0123')['claimed'])
        self.assertEqual(self.service.available_count({'login-a'}), count - 1)
        _, campus, platinum = self.service.query('login-a')['members']
        self.assertEqual(campus['benefits'][0]['remaining'], 1)
        self.assertTrue(platinum['benefits'][0]['claimed'])
        self.assertIn('preparation_id', self.service.campus_prepare('login-a', '27287'))

    def test_campus_uncertain_order_is_not_submitted_again(self):
        for status, body in ((200, None), (200, {'code': 0}), (302, {}), (503, {'resultCode': 0})):
            with self.subTest(status=status, body=body):
                login_id = f'login-{status}-{body is None}'
                self.catalog['vipInfoList'][1]['activityInfoList'][0].update(activityId=int(CAMPUS_ACTIVITY), markType=0)
                detail = self.service.campus_prepare(login_id, '27287')
                sms = self.service.campus_sms(login_id, '27287', detail['preparation_id'])
                self.campus_order_response = body
                self.campus_order_http_status = status
                self.before_campus_order = lambda: self.assertEqual(self.service.campus_results[login_id]['state'], 'pending')
                start = len(self.calls)
                with self.assertRaises(MemberBenefitsError) as raised:
                    self.service.campus_claim(login_id, sms['operation_id'], '0123')
                self.assertEqual(raised.exception.code, 'MEMBER_CLAIM_PENDING')
                with self.assertRaises(MemberBenefitsError):
                    self.service.campus_claim(login_id, sms['operation_id'], '0123')
                self.assertEqual(sum(url.endswith(CAMPUS_ORDER) for _, url in self.calls[start:]), 1)
                benefit = self.service.query(login_id)['members'][1]['benefits'][0]
                self.assertTrue(benefit['claim_pending'])

    def test_campus_official_challenge_cannot_confirm_or_replay_an_order(self):
        self.catalog['vipInfoList'][1]['activityInfoList'][0].update(activityId=int(CAMPUS_ACTIVITY), markType=0)
        detail = self.service.campus_prepare('login-a', '27287')
        sms = self.service.campus_sms('login-a', '27287', detail['preparation_id'])
        self.campus_order_response = {'resultCode': '-490', 'data': {'risk': True, 'explain': 'ORDER_REFUSE'}}
        with self.assertRaises(MemberBenefitsError) as error:
            self.service.campus_claim('login-a', sms['operation_id'], '0123')
        self.assertEqual(error.exception.code, 'MEMBER_VERIFICATION')
        self.assertIn('订购次数', str(error.exception))
        with self.assertRaises(MemberBenefitsError):
            self.service.campus_claim('login-a', sms['operation_id'], '0123')
        self.assertEqual(sum(url.endswith(CAMPUS_ORDER) for _, url in self.calls), 1)

    def test_campus_score_refusal_preserves_device_rejection_and_frequency_message(self):
        self.catalog['vipInfoList'][1]['activityInfoList'][0].update(activityId=int(CAMPUS_ACTIVITY), markType=0)
        detail = self.service.campus_prepare('login-a', '27287')
        sms = self.service.campus_sms('login-a', '27287', detail['preparation_id'])
        self.campus_order_response = {'code': -1, 'message': '访问过于频繁，请稍后再试',
                                      'data': {'risk': True, 'explain': 'SCORE_REFUSE'}}
        with self.assertRaises(MemberBenefitsError) as error:
            self.service.campus_claim('login-a', sms['operation_id'], '0123')
        self.assertEqual((error.exception.code, error.exception.status), ('MEMBER_CAMPUS_DEVICE_REFUSED', 409))
        self.assertIn('设备校验未通过', str(error.exception))
        self.assertIn('访问过于频繁', str(error.exception))
        self.assertNotIn('login-a', self.service.campus_results)
        with self.assertRaises(MemberBenefitsError) as error:
            self.service.campus_claim('login-a', sms['operation_id'], '0123')
        self.assertEqual(error.exception.code, 'MEMBER_CLAIM_STALE')
        self.assertEqual(sum(url.endswith(CAMPUS_SMS) for _, url in self.calls), 1)
        self.assertEqual(sum(url.endswith(CAMPUS_ORDER) for _, url in self.calls), 1)

    def test_campus_score_refusal_is_recognized_without_risk_flag(self):
        session = Mock()
        session.request.return_value = Mock(status_code=200, json=lambda: {
            'resultCode': -1, 'data': {'explain': 'SCORE_REFUSE'},
        })
        with self.assertRaises(MemberBenefitsError) as error:
            self.service._request(session, 'POST', CAMPUS_ORDER)
        self.assertEqual(error.exception.code, 'MEMBER_CAMPUS_DEVICE_REFUSED')

    def test_campus_empty_http_200_is_the_official_success_contract(self):
        self.assertIsNone(self.service._official_result(CAMPUS_ORDER, 200, None))

    def test_discount_claim_returns_only_a_real_official_verification_link(self):
        app = Flask(__name__)
        with patch('member_benefits.MemberBenefits', return_value=self.service):
            register_member_benefits(app, self.service.credentials, self.service.cache)
        entry = 'https://dev.coc.10086.cn/coc3/gr/static-2c/risk-verify/'
        voice = entry + '#/voiceCaptcha?riskActionLogId=fixture%2Bvalue&language=cn'
        for explain, url, expected in ((None, voice, voice), ('SCORE_REFUSE', voice, voice),
                                       ('SCORE_REFUSE', entry + '#/orderFail?fixture=1', None),
                                       ('ORDER_REFUSE', voice, None),
                                       (None, voice.replace('dev.coc.10086.cn', 'dev.coc.10086.cn.evil.test'), None),
                                       (None, None, None)):
            with self.subTest(explain=explain, verification_expected=expected is not None):
                def rejected(*_):
                    data = {'risk': True, 'url': url, 'explain': explain}
                    self.service._official_result(CAMPUS_ORDER, 200, {'code': -1, 'data': data})

                with patch.object(self.service, 'discount_claim', side_effect=rejected):
                    response = app.test_client().post('/api/member-benefits/discount/claim', json={
                        'login_id': 'login-a', 'operation_id': 'fixture', 'sms_code': '0123',
                    })
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json.get('verification_url'), expected)

    def test_campus_order_frequency_and_login_codes_are_classified(self):
        for response, code in (({'code': -1, 'message': '访问过于频繁，请稍后再试'}, 'MEMBER_RATE_LIMIT'),
                               ({'resultCode': 926}, 'MEMBER_SESSION_EXPIRED'),
                               ({'code': 0, 'resultCode': -1006}, 'MEMBER_SMS_INVALID'),
                               ({'resultCode': 0, 'explain': 'SCORE_REFUSE'}, 'MEMBER_CAMPUS_DEVICE_REFUSED')):
            with self.subTest(response=response):
                session = Mock()
                session.request.return_value = Mock(status_code=200, json=lambda: response)
                with self.assertRaises(MemberBenefitsError) as error:
                    self.service._request(session, 'POST', CAMPUS_ORDER)
                self.assertEqual(error.exception.code, code)

    def test_campus_device_failure_preserves_sms_without_recording_an_order_attempt(self):
        self.catalog['vipInfoList'][1]['activityInfoList'][0].update(activityId=int(CAMPUS_ACTIVITY), markType=0)
        detail = self.service.campus_prepare('login-a', '27287')
        sms = self.service.campus_sms('login-a', '27287', detail['preparation_id'])
        self.campus_device_response = {'code': '0', 'data': ''}
        with self.assertRaises(MemberBenefitsError) as error:
            self.service.campus_claim('login-a', sms['operation_id'], '0123')
        self.assertEqual(error.exception.code, 'MEMBER_CAMPUS_DEVICE_UNAVAILABLE')
        self.assertNotIn('login-a', self.service.campus_results)
        self.assertFalse(any(url.endswith(CAMPUS_ORDER) for _, url in self.calls))
        self.campus_device_response = {'code': '0', 'data': 'campus-device-fixture'}
        self.assertEqual(self.service.campus_claim('login-a', sms['operation_id'], '0123')['state'], 'claimed')
        self.assertEqual(len(self.campus_device_requests), 2)
        self.assertEqual(sum(url.endswith(CAMPUS_SMS) for _, url in self.calls), 1)

    def test_campus_explicit_retry_after_bad_sms_uses_new_official_device_token(self):
        self.catalog['vipInfoList'][1]['activityInfoList'][0].update(activityId=int(CAMPUS_ACTIVITY), markType=0)
        detail = self.service.campus_prepare('login-a', '27287')
        sms = self.service.campus_sms('login-a', '27287', detail['preparation_id'])
        self.campus_order_response = {'resultCode': '-1006'}
        with self.assertRaises(MemberBenefitsError) as error:
            self.service.campus_claim('login-a', sms['operation_id'], '0123')
        self.assertEqual(error.exception.code, 'MEMBER_SMS_INVALID')
        self.assertIn('验证码错误', str(error.exception))
        self.campus_device_response = {'code': '0', 'data': 'second-official-device'}
        self.campus_order_response = {'resultCode': '0', 'data': 'fixture-order'}
        self.assertEqual(self.service.campus_claim('login-a', sms['operation_id'], '0123')['state'], 'claimed')
        self.assertEqual(json.loads(self.campus_order_headers[-1]['wasmParams']),
                         ['official-wasm-key', 'second-official-device'])
        self.assertEqual(len(self.campus_device_requests), 2)
        self.assertEqual(sum(url.endswith(CAMPUS_SMS) for _, url in self.calls), 1)

    def test_sidebar_count_uses_recent_account_quotas_and_updates_after_claim(self):
        self.suixin_offer.update(currentRemainNum=2)
        self.service.query('login-a')
        self.service.query('login-b')
        calls = len(self.calls)
        self.assertEqual(self.service.available_count({'login-a', 'login-b'}), 8)
        self.assertEqual(self.service.available_counts({'login-a', 'login-b', 'unknown'}), {'login-a': 4, 'login-b': 4})
        self.assertEqual(self.service.available_count({'login-a'}), 4)
        self.assertEqual(len(self.calls), calls)
        sms = self.service.starbucks_sms('login-a')
        self.service.starbucks_claim('login-a', sms['operation_id'], '0234')
        self.assertEqual(self.service.available_count({'login-a'}), 3)
        self.assertEqual(self.service.available_counts({'login-a', 'login-b'}), {'login-a': 3, 'login-b': 4})
        self.service._starbucks_period = lambda: '2000-01'
        self.assertEqual(self.service.available_count({'login-a', 'login-b'}), 0)

    def test_sidebar_excludes_discounts_and_legacy_totals_that_include_them(self):
        self.suixin_offer.update(currentRemainNum=2)
        self.service.availability['login-a'] = {
            'period': self.service._starbucks_period(), 'other': 33, 'starbucks': 1, 'campus': 1,
        }
        self.assertEqual(self.service.available_count({'login-a'}), 2)
        discount = self.add_discount_member()
        self.assertEqual(discount['remaining'], 1)
        self.assertTrue(discount['selection']['offers'])
        self.assertEqual(self.service.available_count({'login-a'}), 4)

    def test_starbucks_coupon_eligibility_and_claimed_state_are_independent_of_data(self):
        benefit = self.service.query('login-a')['members'][0]['benefits'][0]
        self.starbucks_coupon_state = '2'
        options = self.service.offers('login-a', '1297', benefit['key'])['details']['options']
        self.assertTrue(options[0]['claimed'])
        self.assertEqual(options[0]['status_label'], '已领取')
        self.assertTrue(options[1]['available'])
        self.starbucks_coupon_code = '100026'
        options = self.service.offers('login-a', '1297', benefit['key'])['details']['options']
        self.assertIn('未确认', options[0]['status_label'])
        self.assertTrue(options[1]['available'])

    def test_starbucks_claim_uses_sms_session_and_confirms_official_result(self):
        sms = self.service.starbucks_sms('login-a')
        self.assertEqual(self.service.starbucks_sms('login-a')['operation_id'], sms['operation_id'])
        with self.assertRaises(MemberBenefitsError):
            self.service.starbucks_claim('login-b', sms['operation_id'], '0234')
        result = self.service.starbucks_claim('login-a', sms['operation_id'], '0234')
        self.assertEqual(result['state'], 'claimed')
        self.assertEqual(result['status_label'], '已领取')
        with self.assertRaises(MemberBenefitsError):
            self.service.starbucks_claim('login-a', sms['operation_id'], '0234')
        self.assertEqual(sum(url.endswith('sms/sendSmsCode') for _, url in self.calls), 1)
        self.assertEqual(sum(url.endswith('orderRightsWithSmsByEncrypt') for _, url in self.calls), 1)
        self.starbucks_ticket.assert_called_once()
        self.assertEqual(self.starbucks_ticket.call_args.args[1]['sourceId'], '056002')

    def test_starbucks_sms_rejection_reports_failed_stage_without_resending(self):
        self.starbucks_sms_response = {'code': '40001', 'message': '请求参数错误！'}
        for _ in range(2):
            with self.assertRaises(MemberBenefitsError) as raised:
                self.service.starbucks_sms('login-a')
            self.assertEqual(raised.exception.code, 'MEMBER_SMS_BAD_REQUEST')
            self.assertIn('尚未进入短信验证', str(raised.exception))
        self.assertEqual(sum(url.endswith('sms/sendSmsCode') for _, url in self.calls), 1)

    def test_starbucks_recovers_expired_app_ticket_before_sending_sms_once(self):
        self.service.recover = Mock()
        self.starbucks_ticket.side_effect = [
            (200, {'retCode': '410000'}, {}), self.starbucks_ticket.return_value,
        ]
        self.assertIn('operation_id', self.service.starbucks_sms('login-a'))
        self.service.recover.assert_called_once()
        self.assertEqual(self.starbucks_ticket.call_count, 2)
        self.assertEqual(sum(url.endswith('sms/sendSmsCode') for _, url in self.calls), 1)

    def test_starbucks_recovers_claim_session_before_sms_and_leaves_catalogue_session_intact(self):
        benefit = self.service.query('login-a')['members'][0]['benefits'][0]
        self.service.offers('login-a', '1297', benefit['key'])
        self.starbucks_coupon_errors = ['100002']
        self.assertIn('operation_id', self.service.starbucks_sms('login-a'))
        self.assertEqual(self.starbucks_ticket.call_count, 2)
        self.assertEqual(sum(url.endswith('sms/sendSmsCode') for _, url in self.calls), 1)
        auth_calls = sum(url.endswith(AUTH) for _, url in self.calls)
        self.service.offers('login-a', '1297', benefit['key'])
        self.assertEqual(sum(url.endswith(AUTH) for _, url in self.calls), auth_calls)

    def test_starbucks_does_not_repeat_sms_after_official_session_rejection(self):
        self.starbucks_sms_response = {'code': '100002'}
        for _ in range(2):
            with self.assertRaises(MemberBenefitsError) as raised:
                self.service.starbucks_sms('login-a')
            self.assertEqual(raised.exception.code, 'MEMBER_STARBUCKS_SMS_SESSION_EXPIRED')
        self.assertEqual(sum(url.endswith('sms/sendSmsCode') for _, url in self.calls), 1)

    def test_starbucks_stops_after_one_failed_claim_session_recovery(self):
        self.starbucks_coupon_code = '100002'
        with self.assertRaises(MemberBenefitsError) as raised:
            self.service.starbucks_sms('login-a')
        self.assertEqual(raised.exception.code, 'MEMBER_STARBUCKS_SESSION_EXPIRED')
        self.assertEqual(self.starbucks_ticket.call_count, 2)
        self.assertFalse(any(url.endswith('sms/sendSmsCode') for _, url in self.calls))

    def test_starbucks_acceptance_with_unclaimed_record_reports_status_without_resubmission(self):
        self.starbucks_order_confirms = False
        sms = self.service.starbucks_sms('login-a')
        result = self.service.starbucks_claim('login-a', sms['operation_id'], '0234')
        self.assertEqual(result['state'], 'submitted')
        self.assertIs(result['claimed'], False)
        self.assertEqual(result['status_label'], '未领取')
        with self.assertRaises(MemberBenefitsError) as raised:
            self.service.starbucks_sms('login-a')
        self.assertEqual(raised.exception.code, 'MEMBER_CLAIM_PENDING')

    def test_starbucks_uncertain_submit_and_official_verification_are_not_replayed(self):
        for response, error_code in ((None, 'MEMBER_CLAIM_PENDING'),
                                     ({'code': '-1', 'data': {'risk': True}}, 'MEMBER_VERIFICATION')):
            with self.subTest(error_code=error_code):
                login_id = 'login-' + error_code.lower()
                self.starbucks_order_response = response
                sms = self.service.starbucks_sms(login_id)
                with self.assertRaises(MemberBenefitsError) as raised:
                    self.service.starbucks_claim(login_id, sms['operation_id'], '0234')
                self.assertEqual(raised.exception.code, error_code)
                with self.assertRaises(MemberBenefitsError):
                    self.service.starbucks_claim(login_id, sms['operation_id'], '0234')
        self.assertEqual(sum(url.endswith('orderRightsWithSmsByEncrypt') for _, url in self.calls), 2)

    def test_suixin_distinguishes_no_remaining_claims_from_unknown_quota(self):
        self.catalog['vipInfoList'] = []
        for order, quota, expected in [(0, 0, 'claimed'), (0, '2', 'available'),
                                       (0, -1, 'unknown'), (0, None, 'unknown'), (4, 0, 'unknown')]:
            with self.subTest(order=order, quota=quota):
                self.suixin_offer.update(isOrder=order, currentRemainNum=quota)
                product = self.service.query('login-a')['products'][0]
                self.assertEqual(product['claim_status'], expected)
        self.assertEqual(sum(url.endswith(AUTH) for _, url in self.calls), 1)

    def test_suixin_inspects_the_selected_product_without_fabricating_claim_options(self):
        self.suixin_offer['currentRemainNum'] = 1
        result = self.service.suixin('login-a', 's1')
        self.assertEqual(result['remaining'], 1)
        self.assertEqual(result['options'], [])
        self.assertFalse(result['can_claim'])
        self.assertNotIn('url', result)
        with self.assertRaises(MemberBenefitsError) as raised:
            self.service.suixin('login-a', 'not-in-catalogue')
        self.assertEqual(raised.exception.status, 404)

    def test_suixin_public_catalogue_is_separate_from_account_claims(self):
        self.suixin_auth_code = '100002'
        self.suixin_offer.update(currentRemainNum=1, receiveUrl=SUIXIN_PORTAL + '#/rightsbaw?sourceid=056002')
        result = self.service.suixin('login-a', 's1')
        self.assertEqual(result['options'], [{'name': '样例月卡', 'detail': '随心看'}])
        self.assertEqual(result['catalogue_kind'], 'public')
        self.assertFalse(result['can_claim'])
        self.assertIn('尚待确认', result['message'])
        self.suixin_offer['currentRemainNum'] = 0
        self.assertEqual(self.service.suixin('login-a', 's1')['options'], [])
        self.assertEqual(sum(url.endswith('getActIdsNoLogin') for _, url in self.calls), 1)

    def test_suixin_shows_account_offers_and_claimed_item_even_at_zero_quota(self):
        self.suixin_offer.update(currentRemainNum=1, receiveUrl=SUIXIN_PORTAL + '#/rightsbaw')
        first = self.service.suixin('login-a', 's1')
        self.assertEqual(first['catalogue_kind'], 'account')
        self.assertTrue(first['options'][0]['claimed'])
        self.assertEqual(first['options'][0]['price'], '0.00')
        self.assertTrue(first['options'][1]['available'])
        self.suixin_offer['currentRemainNum'] = 0
        second = self.service.suixin('login-a', 's1')
        self.assertTrue(second['options'][0]['claimed'])
        self.assertFalse(second['options'][1]['available'])
        self.assertNotIn('suixin-private-token', str(second))
        self.assertEqual(sum(url.endswith(AUTH) for _, url in self.calls), 2)

    def test_suixin_official_verification_remains_visible_without_public_fallback(self):
        self.suixin_offer.update(currentRemainNum=1, receiveUrl=SUIXIN_PORTAL + '#/rightsbaw')
        self.suixin_query_code = '10'
        result = self.service.suixin('login-a', 's1')
        self.assertIn('官方要求验证', result['message'])
        self.assertEqual(result['options'], [])
        self.assertFalse(any(url.endswith('getActIdsNoLogin') for _, url in self.calls))

    def test_suixin_claim_binds_account_product_and_sms_then_checks_official_status(self):
        self.suixin_offer.update(currentRemainNum=1, receiveUrl=SUIXIN_ENTRY)
        product = self.service.suixin('login-a', 's1')
        self.assertTrue(product['can_claim'])
        offer = next(option for option in product['options'] if option['can_claim'])
        self.assertNotIn('_order', offer)
        operation = self.service.suixin_prepare('login-a', 's1', offer['id'])
        self.assertEqual(operation['agreement'], '免费发放至本机号码。')
        self.assertFalse(any(url.endswith('/sms/sendSmsCode') for _, url in self.calls))
        operation_id = operation['operation_id']
        self.service.suixin_sms('login-a', operation_id)
        self.service.suixin_sms('login-a', operation_id)
        with self.assertRaises(MemberBenefitsError):
            self.service.suixin_claim('login-b', operation_id, '0123')
        self.assertTrue(self.service.suixin_claim('login-a', operation_id, '0123')['claimed'])
        self.assertEqual(self.service.suixin('login-a', 's1')['claim_status'], 'claimed')
        self.assertEqual(sum(url.endswith('/sms/sendSmsCode') for _, url in self.calls), 1)
        self.assertEqual(self.suixin_payloads, [{
            'goodsId': 'sku-goods-a', 'skuId': 56986, 'actId': '4', 'corGoodsId': 'cor-goods-a',
            'goodsName': '样例月卡', 'businessType': '8', 'price': '0.00', 'userId': '', 'smsCode': '0123',
            'token': 'suixin-private-token', 'frontOrderUrl': SUIXIN_ENTRY, 'channelId': '', 'from': '',
            'operDfp': '', 'sellerId': '', 'sellerMobile': '',
        }])

    def test_suixin_resume_requires_official_pass_and_uncertain_order_is_not_replayed(self):
        self.suixin_offer.update(currentRemainNum=1, receiveUrl=SUIXIN_ENTRY)
        offer = next(o for o in self.service.suixin('login-a', 's1')['options'] if o['can_claim'])
        operation = self.service.suixin_prepare('login-a', 's1', offer['id'])['operation_id']
        self.service.suixin_sms('login-a', operation)
        self.suixin_order_response = {'code': '-1', 'data': {'risk': True, 'businessUniqueId': 'fixture-risk', 'url':
            'https://dev.coc.10086.cn/coc3/gr/static-2c/risk-verify/#/voiceCaptcha?fixture=1'}}
        with self.assertRaises(MemberBenefitsError) as challenge:
            self.service.suixin_claim('login-a', operation, '0123')
        self.assertTrue(challenge.exception.resume_available)
        self.assertEqual(self.service.suixin('login-a', 's1')['verification']['operation_id'], operation)
        with self.assertRaises(MemberBenefitsError) as blocked:
            self.service.suixin_claim('login-a', operation, '', resume=True)
        self.assertTrue(blocked.exception.resume_available)
        self.assertEqual(len(self.suixin_payloads), 1)
        self.suixin_risk_state = 'PASS'
        self.suixin_order_response = None
        with self.assertRaises(MemberBenefitsError) as unknown:
            self.service.suixin_claim('login-a', operation, '', resume=True)
        self.assertEqual(unknown.exception.code, 'MEMBER_CLAIM_PENDING')
        self.assertEqual(self.suixin_payloads[-1]['uniqueId'], 'fixture-risk')
        self.assertEqual(self.service.suixin('login-a', 's1')['claim_status'], 'pending')
        with self.assertRaises(MemberBenefitsError):
            self.service.suixin_claim('login-a', operation, '', resume=True)
        self.assertEqual(len(self.suixin_payloads), 2)

    def test_suixin_preserves_the_official_link_even_without_a_resume_id(self):
        url = 'https://dev.coc.10086.cn/coc3/gr/static-2c/risk-verify/#/voiceCaptcha?fixture=1'
        for code, data in [('40013', {'url': url}), ('10', {'riskInfo': {'riskUrl': url}})]:
            with self.subTest(code=code), self.assertRaises(MemberBenefitsError) as challenge:
                self.service._official_result(SUIXIN_ORDER, 200, {'code': code, 'data': data})
            self.assertEqual(challenge.exception.verification_url, url)
            self.assertFalse(challenge.exception.resume_available)
            self.assertFalse(challenge.exception.verification_sms)

    def test_suixin_secondary_sms_is_explicit_and_uses_the_same_order(self):
        self.suixin_offer.update(currentRemainNum=1, receiveUrl=SUIXIN_ENTRY)
        offer = next(o for o in self.service.suixin('login-a', 's1')['options'] if o['can_claim'])
        operation = self.service.suixin_prepare('login-a', 's1', offer['id'])['operation_id']
        self.service.suixin_sms('login-a', operation)
        self.suixin_order_response = {'code': '40013', 'data': None}
        with self.assertRaises(MemberBenefitsError) as challenge:
            self.service.suixin_claim('login-a', operation, '0123')
        self.assertTrue(challenge.exception.verification_sms)
        self.assertIsNone(challenge.exception.verification_url)
        self.assertFalse(any(url.endswith(SUIXIN_VERIFICATION_SMS) for _, url in self.calls))
        with self.assertRaises(MemberBenefitsError):
            self.service.suixin_claim('login-a', operation, '001234', verification_sms=True)
        with self.assertRaises(MemberBenefitsError):
            self.service.suixin_verification_sms('login-b', operation)
        self.service.suixin_verification_sms('login-a', operation)
        verification = self.service.suixin('login-a', 's1')['verification']
        self.assertEqual(verification['operation_id'], operation)
        self.assertTrue(verification['sms_sent'])
        self.suixin_order_response = {'code': '0'}
        self.assertTrue(self.service.suixin_claim('login-a', operation, '001234', verification_sms=True)['claimed'])
        original, continued = self.suixin_payloads
        self.assertEqual(continued, {**original, 'smsCode': '001234'})
