from contextlib import nullcontext
from datetime import datetime, timedelta
import json
import re
import unittest
from unittest.mock import MagicMock, Mock, patch

from activities.coc_client import CocError
from credential_cache import CredentialCache
from http_transport import requests
from net_age import NetAge
from net_age_client import AI_IDS, BIRTH_IDS, MEMBER_ID, PAGE_ID, NetAgeClient, claim_config
from recharge.cmcc import SHANGHAI


class NetAgeTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 18, tzinfo=SHANGHAI)
        self.client = NetAgeClient(lambda: None, None, None)
        self.service = NetAge(lambda _: {}, lambda: [], None, None, None, now=lambda: self.now)

    def test_shared_product_code_does_not_cross_activity(self):
        record = {'activity_id': MEMBER_ID, 'skuCode': '200000043084'}
        rows = self.client.benefits(5, None, False, [record], [])
        self.assertEqual(rows['member']['status'], '已领取')
        self.assertEqual(rows['ai']['description'], '本月已选 0/2 款')

        record['activity_id'] = AI_IDS[1]
        rows = self.client.benefits(5, None, False, [record], [])
        self.assertTrue(rows['member']['available'])
        self.assertEqual(rows['ai']['description'], '本月已选 1/2 款')

    def test_uncertain_claim_requires_official_receipt_to_clear_intent(self):
        offer = self.client.benefits(5, None, False, [], [])['ai']['offers'][0]
        client = Mock(user={'provinceCode': '100'})

        def disconnect(*_, before_submit, **__):
            before_submit()
            raise CocError('连接中断', 'UPSTREAM_NETWORK', uncertain=True)

        client.post.side_effect = disconnect
        client.orders.return_value = []
        result = self.service._submit(client, 'a', 'ai', offer, '2026-11')
        self.assertFalse(result['confirmed'])
        self.assertEqual(client.post.call_count, 1)
        self.assertEqual(self.service.submissions['a']['ai']['choice'], offer['id'])

        record = {'activity_id': offer['activity'], 'prizeId': offer['sku'], 'createTime': '2026-11-18'}
        client.inspect.return_value = {'rows': self.client.benefits(5, None, False, [record], [])}
        client.surprise_chances.return_value = 0
        self.service.status('a', client)
        self.assertNotIn('ai', self.service.submissions['a'])

    def test_sms_requires_same_account_and_official_slider(self):
        client = Mock()
        client.inspect.return_value = {'rows': self.client.benefits(5, None, False, [], [])}
        with patch('net_age.NetAgeClient', return_value=client):
            preparation = self.service.begin('a', 'member', 'qq-music')
        self.assertTrue(preparation['needs_slider'])
        self.assertIn('QQ 音乐', preparation['notice'])
        self.assertEqual(client.post.call_args.args[0], 'activities/growth/verify/status')

        with self.assertRaises(CocError) as wrong_account:
            self.service.verification('b', preparation['operation'], 'sms', {})
        self.assertEqual(wrong_account.exception.code, 'VERIFICATION_MISSING')
        with self.assertRaises(CocError) as unverified:
            self.service.verification('a', preparation['operation'], 'sms', {})
        self.assertEqual(unverified.exception.code, 'SLIDER_REQUIRED')
        self.assertEqual(client.post.call_count, 1)
        self.service.verification('a', preparation['operation'], 'cancel', {})
        client.close.assert_called_once()

    def test_birthday_coupon_uses_sms_with_the_regular_prize_endpoint(self):
        years = re.match(r'(\d+)年', '10年3个月')
        offer = self.client.benefits(5, years, True, [], [])['birthday']['offers'][0]
        client = Mock(user={'provinceCode': '100'})
        client.orders.return_value = []
        self.service._submit(client, 'a', 'birthday', offer, '2026', code='123456')
        path, payload = client.post.call_args.args
        self.assertEqual(path, 'activities/prize/templatePrize')
        self.assertEqual(payload['activityId'], offer['activity'])
        self.assertEqual(client.post.call_args.kwargs['encryption'], 'aurora')
        self.assertEqual(json.loads(payload['jsonExtParam'])['productCode'], '130000006303')

    def test_actual_birthday_gift_details_resolve_a_different_pending_choice(self):
        annual = [{'activity_id': BIRTH_IDS[0], 'skuCode': '444444', 'skuName': '4张*网龄回馈-充值100立减5元',
                   'createTime': '2026-11-18 18:20:10'}]
        client = Mock()
        client.inspect.return_value = {'rows': self.client.benefits(5, None, False, [], annual)}
        client.surprise_chances.return_value = 0
        self.service.mark('a', 'birthday', {'choice': 'cinema', 'period': '2026'})

        row = self.service.status('a', client)['rows']['birthday']

        self.assertEqual(row['status'], '已领取')
        self.assertEqual([offer['name'] for offer in row['offers'] if offer['received']], ['4 张 5 元话费充值满减券（满 100 元可用）'])
        self.assertFalse(row['available'])
        self.assertNotIn('birthday', self.service.submissions['a'])

    def test_birthday_with_no_official_receipt_stays_blocked_for_resubmission(self):
        years = re.match(r'(\d+)年', '13年')
        client = Mock()
        client.inspect.return_value = {'rows': self.client.benefits(5, years, True, [], [])}
        client.surprise_chances.return_value = 0
        self.service.mark('a', 'birthday', {'choice': 'cinema', 'period': '2026'})

        row = self.service.status('a', client)['rows']['birthday']

        self.assertEqual(row['status'], '结果待确认')
        self.assertIn('已查询官方订单', row['description'])
        self.assertFalse(row['available'])
        self.assertIn('birthday', self.service.submissions['a'])

    def test_expired_app_uid_recovery_stops_before_order_or_wheel_requests(self):
        snapshot = {'id':'a', 'phone':'13800000000', 'cookie':'JSESSIONID=test; UID=test'}
        recover = Mock()
        client = NetAgeClient(lambda: snapshot, None, recover)
        http = Mock()
        client.session_factory = lambda: http
        with patch('activities.coc_client.AppLoginSession.from_credentials') as factory:
            flow = factory.return_value
            flow.call.side_effect = [(200, {'retCode':'410000'}, {}), (200, {'retCode':'400007'}, {})]
            with self.assertRaises(CocError) as failure:
                client.connect()

        self.assertEqual(failure.exception.code, 'APP_LOGIN_REQUIRED')
        self.assertEqual(failure.exception.status, 401)
        self.assertEqual(flow.call.call_count, 2)
        recover.assert_called_once_with(snapshot)
        http.post.assert_not_called()

    def test_flow_config_fetch_works_with_session_only_transport(self):
        transport = MagicMock(spec=['Session', 'RequestsError'])
        session = transport.Session.return_value.__enter__.return_value
        session.get.return_value.text = 'var configData = ' + json.dumps({
            'pageId': PAGE_ID, 'configItemList': [{'configContent_27': '931'}]})
        with patch('net_age_client.requests', transport):
            self.assertEqual(claim_config(), {'configContent_27': '931'})
        session.get.assert_called_once()

    def test_badge_counts_only_two_confirmed_available_groups_and_excludes_pending(self):
        years = re.match(r'(\d+)年', '13年')
        client = Mock(surprise_chances=Mock(return_value=4))
        rows = self.client.benefits(5, years, True, [], [])
        self.assertTrue(rows['flow']['available'] and rows['ai']['available'])
        client.inspect.return_value = {'rows': rows}
        self.assertEqual(self.service.status('a', client)['available_count'], 2)
        self.assertEqual(self.service.available_counts({'a','b'}), {'a':2})

        self.service.mark('a','birthday',{'choice':'phone','period':'2026'})
        self.assertEqual(self.service.available_counts({'a'}), {'a':1})
        self.now = self.now.replace(month=12, day=1)
        self.assertEqual(self.service.available_counts({'a'}), {})

    def test_badge_scan_checks_other_accounts_once_per_day_without_surprise_or_claims(self):
        years = re.match(r'(\d+)年', '13年')
        client = Mock()
        rows = self.client.benefits(5, years, False, [], [])
        client.inspect.return_value = {'rows': {group:rows[group] for group in ('member','birthday')}}
        self.service.connected = Mock(side_effect=lambda _:nullcontext(client))

        self.assertEqual(self.service.check_availability('a'), 1)
        self.assertEqual(self.service.check_availability('a'), 1)
        client.inspect.assert_called_once_with(self.now, badges_only=True)
        self.now += timedelta(days=1)
        self.assertEqual(self.service.check_availability('a'), 1)
        self.assertEqual(client.inspect.call_count, 2)
        client.surprise_chances.assert_not_called()
        client.post.assert_not_called()

    def test_confirmed_member_claim_immediately_decreases_the_cached_badge(self):
        years = re.match(r'(\d+)年', '13年')
        rows = self.client.benefits(5, years, True, [], [])
        self.service._record_availability('a', self.now, rows)
        offer = next(offer for offer in rows['member']['offers'] if offer['id']=='iqiyi')
        client = Mock(user={'provinceCode':'100'})
        client.post.side_effect = lambda *_, before_submit, **__: before_submit()
        client.orders.return_value = [{'activity_id':MEMBER_ID, 'skuCode':offer['products'][0],
                                      'createTime':'2026-11-18 17:00:00'}]
        self.assertTrue(self.service._submit(client,'a','member',offer,'2026-11',code='123456')['confirmed'])
        self.assertEqual(self.service.available_counts({'a'}), {'a':1})

    def test_no_monthly_order_permits_only_explicit_manual_repreparation(self):
        client = Mock(user={'provinceCode':'100'})
        client.inspect.side_effect = lambda *_args, **_kwargs: {'rows':self.client.benefits(5, None, False, [], [])}
        self.service.mark('a','flow',{'choice':'monthly','period':'2026-11'})
        self.service.mark('a','member',{'choice':'bilibili','period':'2026-11'})
        rows = self.service.status('a', client)['rows']
        for group in ('flow','member'):
            self.assertEqual(rows[group]['status'], '结果待确认')
            self.assertTrue(rows[group]['retry_available'])
            self.assertIn('本月暂未查到领取记录', rows[group]['description'])

        with patch('net_age.NetAgeClient', return_value=client):
            with self.assertRaises(CocError) as blocked:
                self.service.begin('a','member','bilibili')
            self.assertEqual(blocked.exception.code, 'SUBMISSION_PENDING')
            prepared = self.service.begin('a','member','bilibili',retry=True)
        self.assertTrue(prepared['needs_slider'])
        self.assertTrue(self.service.pending['a'].retry)
        client.post.assert_called_once_with('activities/growth/verify/status', {}, recover=True)
        self.assertIn('member', self.service.submissions['a'])

    def test_existing_monthly_order_blocks_retry_even_with_an_unknown_sku(self):
        receipt = {'activity_id':MEMBER_ID, 'skuCode':'unknown-provider-sku', 'createTime':'2026-11-18 23:00:00'}
        client = Mock()
        client.inspect.side_effect = lambda *_args, **_kwargs: {'rows':self.client.benefits(5, None, False, [receipt], [])}
        self.service.mark('a','member',{'choice':'bilibili','period':'2026-11'})
        row = self.service.status('a', client)['rows']['member']
        self.assertFalse(row['retry_available'])
        self.assertIn('发现本月订单', row['description'])
        with self.assertRaises(CocError) as blocked:
            self.service._offer(client,'a','member','bilibili',self.now,retry=True)
        self.assertEqual(blocked.exception.code, 'SUBMISSION_PENDING')
        client.post.assert_not_called()

    def test_confirmed_member_order_resolves_a_different_pending_choice(self):
        receipt = {'activity_id':MEMBER_ID, 'skuCode':'200000034571', 'createTime':'2026-11-18 23:00:00'}
        client = Mock()
        client.inspect.return_value = {'rows':self.client.benefits(5, None, False, [receipt], [])}
        self.service.mark('a','member',{'choice':'bilibili','period':'2026-11'})
        row = self.service.status('a', client)['rows']['member']
        self.assertEqual(row['status'], '已领取')
        self.assertEqual([offer['id'] for offer in row['offers'] if offer['received']], ['qq-music'])
        self.assertNotIn('member', self.service.submissions['a'])

    def test_pre_submit_failure_does_not_clear_an_earlier_pending_attempt(self):
        previous = {'choice':'monthly','period':'2026-11'}
        self.service.mark('a','flow',previous)
        client = Mock(user={'provinceCode':'100'})
        client.post.side_effect = CocError('官方设备认证暂未通过','GDT_REJECTED')
        offer = self.client.benefits(5, None, False, [], [])['flow']['offers'][0]

        with self.assertRaises(CocError):
            self.service._submit(client,'a','flow',offer,'2026-11',code='123456')

        self.assertEqual(self.service.submissions['a']['flow'],previous)
        client.orders.assert_not_called()

    def test_surprise_checks_actual_quota_and_prizes_reusing_the_scoped_session(self):
        snapshot = {'id':'a','phone':'13800000000'}
        self.service.credentials = lambda _:snapshot
        self.service.cache = CredentialCache()
        activity = Mock(user={'maskedMobile':'138****0000','token':'coc-token'})
        activity.post.side_effect = [{}, {'cmPassPortToken':'scoped-ticket'}]
        self.service.connected = Mock(side_effect=lambda _:nullcontext(activity))

        def response(code, data):
            value = Mock(status_code=200)
            value.json.return_value = {'code':code, 'data':data}
            return value

        def session(replies):
            client = MagicMock()
            client.__enter__.return_value = client
            client.headers = {}
            client.cookies = requests.Cookies()
            client.post.side_effect = replies
            return client

        count = response('200', 0)
        prizes = response('200', {'total':2,'list':[
            {'salesUnitId':10,'showName':'测试券','createTime':'2026-11-18'},
            {'salesUnitId':-1,'showName':'未中奖'},
        ]})
        first = session([response('0',{'mobileMask':'138****0000','token':'market-token'}),count,prizes])
        second = session([count,prizes])
        expected = {'remaining':0, 'prizes':[{'name':'测试券','date':'2026-11-18'}]}
        with patch('net_age.requests.Session', side_effect=[first,second]):
            self.assertEqual(self.service.surprise_details('a'), expected)
            self.assertEqual(self.service.surprise_details('a'), expected)

        self.service.connected.assert_called_once_with('a')
        self.assertEqual(activity.post.call_count, 2)
        self.assertEqual(second.headers['phone'],'market-token')
        self.assertEqual([call.args[0].rsplit('/',1)[1] for call in second.post.call_args_list],
                         ['residueLotteryCount','lotteryList'])

    def test_generic_claim_failure_reconciles_the_official_record(self):
        offer = self.client.benefits(5, None, False, [], [])['flow']['offers'][0]
        client = Mock(user={'provinceCode': '100'})

        def rejected(*_, before_submit, **__):
            before_submit()
            raise CocError('领取失败！（10）', '10')

        client.post.side_effect = rejected
        client.orders.return_value = [{'activity_id': offer['activity'], 'skuCode': offer['sms_product'],
                                       'createTime': '2026-11-18 22:01:03'}]
        result = self.service._submit(client, 'a', 'flow', offer, '2026-11', code='123456')
        self.assertTrue(result['confirmed'])
        self.assertEqual(client.post.call_count, 1)
        self.assertNotIn('flow', self.service.submissions['a'])


if __name__ == '__main__':
    unittest.main()
