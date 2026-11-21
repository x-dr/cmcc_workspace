import copy
from datetime import datetime, timedelta
import time
import unittest
from unittest.mock import Mock
from curl_cffi import requests

from checkin_client import CheckinClient, CheckinError, ENTRY, FLASH_PRODUCT, PreparedFlash
from recharge.cmcc import SHANGHAI


SNAPSHOT = {'phone': '13800000000', 'cookie': 'JSESSIONID=fake', 'profile': {}, 'credential_revision': 1}
NOW = 1793246400000  # 2026-10-29 12:00 Asia/Shanghai


class Response:
    def __init__(self, data=None, *, text='', status=200, headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}
        self.data = data

    def json(self):
        return self.data


class ProtocolSession:
    def __init__(self, *, signed=True, now=NOW):
        self.headers, self.cookies, self.calls, self.signed, self.now = {}, {}, [], signed, now
        self.stock, self.claimed = 1, False
        self.identity = {'mobile': SNAPSHOT['phone'], 'channel': 'app'}
        self.today_status = None
        self.flash_title, self.prize_name, self.start_time = '', '2元话费券', NOW
        self.other_zones = []
        self.end_time = NOW+3600000
        self.team_state = 0

    def get(self, url, **kwargs):
        self.calls.append(('GET', url, kwargs))
        if self.headers.get('login-check') or self.headers.get('x-requested-with'):
            return Response(text='{"code":"GOTO_LOGIN","data":null}')
        return Response(text='var loginPath="/appTokenLogin?sid=fake";')

    def post(self, url, *, json, **kwargs):
        self.calls.append(('POST', url, json, kwargs.get('headers', {})))
        if '/qwhdsso/appTokenLogin' in url:
            return Response({'code': 'SUCCESS', 'data': {'url': ENTRY}})
        if kwargs.get('headers', {}).get('login-check') != '1':
            return Response(text='<!doctype html>login redirect')
        path = url.split('/api/mark/')[-1]
        if path == 'user/info':
            data = self.identity
        elif path == 'info/commonInfo':
            data = {'sysTime': self.now, 'isMark31': True}
        elif path == 'mark31/markstatus':
            data = {'markstatus': [{'date': '20261029', 'status': self.today_status or ('1' if self.signed else '0')}],
                    'userinfo': {'accumulateTimes': 1 if self.signed else 0}}
        elif path == 'markSeckill/secConfig':
            data = {'secKillData': {'mainTitle': self.flash_title,
                    'secKillZones': [*self.other_zones, {'id': 123, 'startTime': self.start_time, 'endTime': self.end_time,
                    'prize': {'id': 456, 'name': self.prize_name, 'leaveStock': self.stock, 'status': '1' if self.claimed else '0'}}]}}
        elif path == 'mark31/domark':
            self.signed = True
            data = {'markPrize': {'name': '100MB流量日包券'}}
        elif path == 'markSeckill/redeem':
            self.claimed = True
            data = {}
        elif path == 'groupmark/status':
            data = {'status': self.team_state}
        elif path == 'groupmark/award':
            self.team_state = 4
            data = {'myPrize': {'name': '5元话费券'}}
        else:
            raise AssertionError(path)
        return Response({'code': 'SUCCESS', 'data': data})

    def close(self):
        pass


class CheckinProtocolTests(unittest.TestCase):
    def client(self, session):
        return CheckinClient(lambda: copy.deepcopy(SNAPSHOT), session_factory=lambda: session)

    def mutations(self, session):
        return [call for call in session.calls if call[0] == 'POST' and
                ('/domark' in call[1] or '/redeem' in call[1])]

    def test_inspect_uses_real_calendar_and_never_mutates(self):
        session = ProtocolSession()
        result = self.client(session).inspect()
        self.assertEqual((result['today_state'], result['month_count']), ('signed', 1))
        self.assertEqual(result['date'], '20261029')
        self.assertEqual(self.mutations(session), [])
        self.assertEqual(result['products'][0]['id'], FLASH_PRODUCT)
        self.assertNotIn('cookie', result)

    def test_page_navigation_is_not_treated_as_api_login_check(self):
        session = ProtocolSession()
        self.assertEqual(self.client(session).inspect()['today_state'], 'signed')
        self.assertNotIn('login-check', session.headers)
        self.assertEqual(len([call for call in session.calls if call[0] == 'GET']), 2)

    def test_opaque_mobile_is_bound_to_expected_masked_display_phone(self):
        session = ProtocolSession()
        session.identity = {'mobile': 'opaque-not-a-phone', 'nickName': '138****0000', 'channel': 'app'}
        self.assertEqual(self.client(session).inspect()['today_state'], 'signed')

    def test_wrong_masked_phone_cannot_use_opaque_identity(self):
        session = ProtocolSession()
        session.identity = {'mobile': 'opaque-not-a-phone', 'nickName': '139****1111', 'channel': 'app'}
        with self.assertRaises(CheckinError) as raised:
            self.client(session).signin()
        self.assertEqual(raised.exception.code, 'ACCOUNT_MISMATCH')
        self.assertEqual(self.mutations(session), [])

    def test_opaque_identity_without_verifiable_phone_is_rejected(self):
        session = ProtocolSession()
        session.identity = {'mobile': 'opaque-not-a-phone', 'channel': 'app'}
        with self.assertRaises(CheckinError) as raised:
            self.client(session).signin()
        self.assertEqual(raised.exception.code, 'ACCOUNT_MISMATCH')
        self.assertEqual(self.mutations(session), [])

    def test_already_signed_never_posts_again(self):
        session = ProtocolSession()
        result = self.client(session).signin()
        self.assertEqual(result['state'], 'already_signed')
        self.assertFalse(result['submitted'])
        self.assertEqual(self.mutations(session), [])

    def test_signin_once_then_verifies_official_calendar(self):
        session = ProtocolSession(signed=False)
        result = self.client(session).signin()
        self.assertEqual(result['state'], 'signed')
        self.assertEqual(result['month_count'], 1)
        self.assertEqual(len(self.mutations(session)), 1)
        self.assertEqual(self.mutations(session)[0][2], {'date': '20261029'})
        self.assertNotIn('Priority', self.mutations(session)[0][3])
        self.assertNotIn('/do/mark', self.mutations(session)[0][1])

    def test_submission_callback_runs_immediately_before_real_write(self):
        session = ProtocolSession(signed=False)
        before = Mock(side_effect=lambda: self.assertEqual(self.mutations(session), []))
        self.client(session).signin(before_submit=before)
        before.assert_called_once_with()
        before.reset_mock()
        self.client(session).signin(before_submit=before)
        before.assert_not_called()

    def test_flash_will_not_send_early(self):
        session = ProtocolSession(now=NOW-1000)
        result = self.client(session).flash(FLASH_PRODUCT)
        self.assertFalse(result['submitted'])
        self.assertEqual(self.mutations(session), [])

    def test_re_sign_status_is_not_claimed_as_today_signed(self):
        session = ProtocolSession()
        session.today_status = '2'
        with self.assertRaises(CheckinError) as raised:
            self.client(session).signin()
        self.assertEqual(raised.exception.code, 'TODAY_STATE_MISSING')
        self.assertEqual(self.mutations(session), [])

    def test_unknown_end_time_never_authorizes_flash_submission(self):
        session = ProtocolSession()
        session.end_time = 'not-a-time'
        result = self.client(session).flash(FLASH_PRODUCT)
        self.assertEqual(result['state'], 'skipped')
        self.assertEqual(self.mutations(session), [])

    def test_flash_requires_today_signin(self):
        session = ProtocolSession(signed=False)
        result = self.client(session).flash(FLASH_PRODUCT)
        self.assertFalse(result['submitted'])
        self.assertEqual(self.mutations(session), [])

    def test_flash_fetches_current_zone_and_claims_once(self):
        session = ProtocolSession()
        result = self.client(session).flash(FLASH_PRODUCT)
        self.assertEqual(result['state'], 'success')
        self.assertEqual(self.mutations(session)[0][2], {'secId': '123', 'prizeId': 456})
        self.assertEqual(self.mutations(session)[0][3].get('Priority'), 'u=0, i')
        result = self.client(session).flash(FLASH_PRODUCT)
        self.assertFalse(result['submitted'])
        self.assertEqual(len(self.mutations(session)), 1)

    def test_sold_out_does_not_post(self):
        session = ProtocolSession()
        session.stock = 0
        self.assertFalse(self.client(session).flash(FLASH_PRODUCT)['submitted'])
        self.assertEqual(self.mutations(session), [])

    def test_credential_rotation_before_submit_aborts(self):
        session = ProtocolSession(signed=False)
        provider = Mock(return_value=copy.deepcopy(SNAPSHOT))
        client = CheckinClient(provider, session_factory=lambda: session)
        original = session.post
        def post(url, **kwargs):
            response = original(url, **kwargs)
            if url.endswith('/markSeckill/secConfig'):
                provider.return_value = {**SNAPSHOT, 'credential_revision': 2, 'cookie': 'JSESSIONID=new'}
            return response
        session.post = post
        with self.assertRaises(CheckinError) as raised:
            client.signin()
        self.assertEqual(raised.exception.code, 'APP_CREDENTIALS_CHANGED')
        self.assertEqual(self.mutations(session), [])

    def test_timeout_after_submit_is_uncertain_and_never_retried(self):
        session = ProtocolSession(signed=False)
        original = session.post
        attempted = []
        def post(url, **kwargs):
            if url.endswith('/mark31/domark'):
                attempted.append(url)
                raise requests.RequestsError('timeout')
            return original(url, **kwargs)
        session.post = post
        with self.assertRaises(CheckinError) as raised:
            self.client(session).signin()
        self.assertTrue(raised.exception.uncertain)
        self.assertEqual(len(attempted), 1)

    def test_mismatched_sso_account_is_never_used(self):
        session = ProtocolSession(signed=False)
        original = session.post
        def post(url, **kwargs):
            if url.endswith('/user/info'):
                return Response({'code': 'SUCCESS', 'data': {'mobile': '13900000000', 'channel': 'app'}})
            return original(url, **kwargs)
        session.post = post
        with self.assertRaises(CheckinError) as raised:
            self.client(session).signin()
        self.assertEqual(raised.exception.code, 'ACCOUNT_MISMATCH')
        self.assertEqual(self.mutations(session), [])

    def test_refuses_cross_origin_sso_redirect(self):
        session = ProtocolSession()
        session.get = Mock(return_value=Response(status=302, headers={
            'location': 'https://other.example/private-path?sid=private-ticket',
        }))
        with self.assertRaises(CheckinError) as raised:
            self.client(session).inspect()
        self.assertEqual(raised.exception.code, 'SSO_REDIRECT')
        message = str(raised.exception)
        self.assertIn('活动入口 HTTP 302', message)
        self.assertIn('other.example', message)
        self.assertNotIn('private-', message)
        session.get.assert_called_once_with(ENTRY, timeout=(10, 20), allow_redirects=False)
        self.assertEqual(session.calls, [])

    def test_records_blocked_app_callback_without_leaking_userinfo(self):
        session = ProtocolSession()
        session.post = Mock(return_value=Response({'code': 'SUCCESS', 'data': {
            'url': 'https://private-user@wx.10086.cn/private-path?sid=private-ticket',
        }}))
        with self.assertRaises(CheckinError) as raised:
            self.client(session).inspect()
        self.assertEqual(raised.exception.code, 'SSO_REDIRECT')
        message = str(raised.exception)
        self.assertIn('APP 换票回调', message)
        self.assertIn('地址含用户信息', message)
        self.assertNotIn('private-', message)
        self.assertEqual([call[1] for call in session.calls], [ENTRY])
        session.post.assert_called_once()

    def test_unknown_product_is_rejected_before_network(self):
        session = ProtocolSession()
        with self.assertRaises(CheckinError):
            self.client(session).flash('a-purchase')
        self.assertEqual(session.calls, [])

    def test_weekly_coupon_is_only_available_at_its_official_time(self):
        session = ProtocolSession(now=NOW-1800000)
        session.flash_title, session.prize_name = '每周秒杀 限抢1次', '1元话费券'
        session.start_time = NOW-600000
        # A claimed weekly coupon from the previous week cannot block this week.
        session.other_zones = [{'id': 9, 'startTime': NOW-7*86400000, 'endTime': NOW-7*86400000+600000,
                                'prize': {'id': 9, 'name': '1元话费券', 'status': '1', 'leaveStock': 0}}]
        client = self.client(session)
        preview = client.inspect()
        self.assertEqual(preview['flash_kind'], 'weekly')
        self.assertEqual(preview['flash_next_at'], '2026-10-29T11:50:00+08:00')
        self.assertEqual(preview['products'][0]['name'], '1元话费券')
        self.assertFalse(client.flash(FLASH_PRODUCT)['submitted'])
        self.assertEqual(self.mutations(session), [])
        session.now = session.start_time
        self.assertTrue(client.flash(FLASH_PRODUCT)['submitted'])
        self.assertEqual(self.mutations(session)[0][2], {'secId': '123', 'prizeId': 456})

    def test_weekly_claim_record_prevents_redeeming_another_session_that_week(self):
        session = ProtocolSession()
        session.flash_title, session.prize_name = '每周秒杀', '1元话费券'
        result = self.client(session).flash(FLASH_PRODUCT, claimed_periods={'weekly': '2026-10-26'})
        self.assertEqual(result['flash_state'], 'already_claimed')
        self.assertEqual(self.mutations(session), [])

    def test_weekly_title_never_uses_a_monthly_coupon(self):
        session = ProtocolSession()
        session.flash_title = '每周秒杀'
        result = self.client(session).flash(FLASH_PRODUCT)
        self.assertFalse(result['submitted'])
        self.assertEqual(self.mutations(session), [])

    def test_team_award_checks_readiness_and_submits_once(self):
        session = ProtocolSession()
        client, before = self.client(session), Mock()
        self.assertEqual(client.team_award(before_submit=before), (0, ''))
        before.assert_not_called()
        session.team_state = 3
        self.assertEqual(client.team_award(before_submit=before, check_only=True), (3, ''))
        before.assert_not_called()
        self.assertEqual(client.team_award(before_submit=before), (4, '5元话费券'))
        self.assertEqual(client.team_award(before_submit=before), (4, ''))
        before.assert_called_once()
        self.assertEqual([call[2] for call in session.calls if call[1].endswith('/groupmark/award')], [{}])


class PreparedFlashTests(unittest.TestCase):
    def setUp(self):
        self.primary, self.workers = ProtocolSession(), []
        self.primary.cookies['HLWHD_SESSION_TOKEN'] = 'fake-activity-session'
        self.provider = Mock(return_value=copy.deepcopy(SNAPSHOT))
        def worker():
            session = ProtocolSession()
            session.close = Mock()
            self.workers.append(session)
            return session
        self.client = CheckinClient(self.provider, session_factory=lambda: self.primary,
                                   worker_session_factory=worker)
        self.settings = {'burst_count': 40, 'burst_interval': 0.05, 'burst_workers': 3}

    def test_burst_reuses_one_sso_and_prepares_a_session_for_each_timer(self):
        settings = {**self.settings, 'burst_interval': 0.01}
        result = self.client.flash(FLASH_PRODUCT, burst=settings)
        self.assertEqual((result['state'], result['attempt_count']), ('success', 40))
        sso_calls = [c for c in self.primary.calls if '/qwhdsso/appTokenLogin' in c[1]]
        self.assertEqual(len(sso_calls), 1)
        self.assertEqual(len(self.workers), 40)
        mutations = []
        for index, worker in enumerate(self.workers):
            self.assertEqual(worker.cookies['HLWHD_SESSION_TOKEN'], 'fake-activity-session')
            self.assertEqual(worker.headers['Referer'], ENTRY)
            warmup = [c for c in worker.calls if c[1].endswith('/info/commonInfo')]
            self.assertEqual(len(warmup), int(index < 3))
            mutations.extend(c for c in worker.calls if c[1].endswith('/redeem'))
            worker.close.assert_called_once_with()
        self.assertEqual(len(mutations), 40)
        self.assertTrue(all(c[3].get('Priority') == 'u=0, i' for c in mutations))
        self.assertEqual({tuple(sorted(c[2].items())) for c in mutations},
                         {(('prizeId', 456), ('secId', '123'))})
        self.assertEqual([attempt['index'] for attempt in result['attempts']], list(range(1, 41)))
        self.assertFalse(any(attempt['followup'] for attempt in result['attempts']))
        self.assertNotIn('cookie', str(result))
        self.assertNotIn('fake-activity-session', str(result))

    def test_reserved_burst_sends_exact_count_after_zone_end_with_zero_stock(self):
        self.primary.start_time = NOW-3600000
        self.primary.end_time = NOW-60000
        self.primary.stock = 0
        target = datetime.fromtimestamp(NOW/1000, SHANGHAI)
        result = self.client.flash(FLASH_PRODUCT,
            burst={'burst_count': 20, 'burst_interval': 0.01, 'burst_workers': 4},
            target_at=target, start_monotonic=time.monotonic())
        mutations = [call for worker in self.workers for call in worker.calls
                     if call[1].endswith('/redeem')]
        self.assertEqual((result['state'], result['attempt_count']), ('success', 20))
        self.assertEqual(len(mutations), 20)
        self.assertEqual([attempt['index'] for attempt in result['attempts']], list(range(1, 21)))

    def test_confirmed_claim_skips_reserved_burst_before_warming_workers(self):
        self.primary.flash_title, self.primary.prize_name = '每周秒杀', '1元话费券'
        self.primary.claimed = True
        result = self.client.flash(FLASH_PRODUCT, burst=self.settings)
        self.assertEqual((result['flash_state'], result['attempt_count']), ('already_claimed', 0))
        self.assertEqual(self.workers, [])

    def test_preparation_waits_for_noon_even_if_official_zone_started_earlier(self):
        target = datetime.fromtimestamp(NOW/1000, SHANGHAI)
        zone = {'id': 123, 'startTime': NOW-3600000, 'endTime': NOW+3600000,
                'prize': {'id': 456, 'status': '0', 'leaveStock': 1}}
        self.client._inspect = Mock(return_value=({'server_time': (target-timedelta(seconds=30)).isoformat(),
                'today_state': 'signed', 'flash_state': 'available'}, [zone]))
        prepared = PreparedFlash(self.client, self.primary, copy.deepcopy(SNAPSHOT))
        self.addCleanup(prepared.close)
        original = self.client._post
        def post(worker, path, *args, **kwargs):
            worker.now = NOW-30000
            return original(worker, path, *args, **kwargs)
        self.client._post = post
        began = time.monotonic()
        prepared.prepare(2, target)
        self.assertTrue(prepared.ready)
        self.assertGreaterEqual(prepared.start_monotonic, began+30)
        self.assertTrue(all(len(worker.calls) == 1 for worker in self.workers))

    def test_preparation_uses_scheduler_shared_monotonic_deadline(self):
        target = datetime.fromtimestamp(NOW/1000, SHANGHAI)
        zone = {'id': 123, 'startTime': NOW-3600000, 'endTime': NOW+3600000,
                'prize': {'id': 456, 'status': '0', 'leaveStock': 1}}
        self.client._inspect = Mock(return_value=({'server_time': target.isoformat(),
                'today_state': 'signed', 'flash_state': 'available'}, [zone]))
        prepared = PreparedFlash(self.client, self.primary, copy.deepcopy(SNAPSHOT))
        self.addCleanup(prepared.close)
        shared = time.monotonic()+10
        prepared.prepare(2, target, start_monotonic=shared)
        self.assertEqual(prepared.start_monotonic, shared)

    def test_one_limited_prewarm_connection_does_not_reduce_request_count(self):
        post = self.client._post

        def limit_second_connection(session, path, *args, **kwargs):
            if len(self.workers) == 2 and session is self.workers[-1] and path == 'info/commonInfo':
                return {'code': 'FAIL', 'status': 'GWT_LIMITED'}
            return post(session, path, *args, **kwargs)

        self.client._post = limit_second_connection
        prepared = PreparedFlash(self.client, self.primary, copy.deepcopy(SNAPSHOT))
        try:
            prepared.prepare(3, None)
            self.assertTrue(prepared.ready)
            self.assertEqual(prepared.worker_count, 2)
            self.assertEqual(len(self.workers), 3)
            self.assertFalse(any(c[1].endswith('/redeem') for w in self.workers for c in w.calls))
        finally:
            prepared.close()
        for worker in self.workers:
            worker.close.assert_called_once_with()

    def test_ten_limited_replies_still_send_exactly_ten_without_followup(self):
        def limited_worker():
            session = ProtocolSession()
            original_post = session.post

            def post(url, *, json, **kwargs):
                if url.endswith('/markSeckill/redeem'):
                    session.calls.append(('POST', url, json, kwargs.get('headers', {})))
                    return Response({'code': 'FAIL', 'status': 'GWT_LIMITED'})
                return original_post(url, json=json, **kwargs)

            session.post = post
            session.close = Mock()
            self.workers.append(session)
            return session

        self.client.worker_session_factory = limited_worker
        before_submit = Mock()
        result = self.client.flash(FLASH_PRODUCT,
            burst={'burst_count': 10, 'burst_interval': 0.01, 'burst_workers': 3},
            before_submit=before_submit)

        mutations = [c for worker in self.workers for c in worker.calls if c[1].endswith('/redeem')]
        self.assertEqual((result['state'], result['attempt_count']), ('skipped', 10))
        self.assertEqual(len(mutations), 10)
        self.assertEqual([attempt['index'] for attempt in result['attempts']], list(range(1, 11)))
        self.assertTrue(all(attempt['code'] == 'GWT_LIMITED' for attempt in result['attempts']))
        self.assertFalse(any(attempt['followup'] for attempt in result['attempts']))
        before_submit.assert_called()
        self.assertEqual(before_submit.call_count, 10)

    def test_changed_credentials_after_warmup_prevent_all_redeems(self):
        from checkin_flash import run_flash_burst
        prepared = PreparedFlash(self.client, self.primary, copy.deepcopy(SNAPSHOT))
        self.addCleanup(prepared.close)
        prepared.prepare(3, None)
        self.provider.return_value = {**SNAPSHOT, 'credential_revision': 2, 'cookie': 'JSESSIONID=new'}
        result = run_flash_burst(prepared, self.settings, Mock(), lambda: False)
        self.assertEqual((result['state'], result['attempt_count']), ('failed', 0))
        self.assertEqual(result['attempts'][0]['code'], 'APP_CREDENTIALS_CHANGED')
        self.assertFalse(any(c[1].endswith('/redeem') for w in self.workers for c in w.calls))

    def test_only_busy_or_not_started_replies_allow_following_requests(self):
        prepared = PreparedFlash(self.client, self.primary, copy.deepcopy(SNAPSHOT))
        self.addCleanup(prepared.close)
        prepared.prepare(1, None)
        worker = self.workers[0]
        for code, state in [('NET_ERROR', 'retryable'), ('NOT_TIME_IN', 'retryable'),
                            ('TRAFFIC_LIMITED', 'retryable'), ('GWT_LIMITED', 'retryable'),
                            ('PRIZE_NO_STOCK', 'terminal'), ('PRIZE_LIMIT_MONTH', 'terminal'),
                            ('UNKNOWN', 'terminal')]:
            worker.post = Mock(return_value=Response({'code': 'FAIL', 'status': code}))
            result = prepared.send(before_submit=Mock())
            self.assertEqual(result['state'], state)
            if code in {'TRAFFIC_LIMITED', 'GWT_LIMITED'}:
                self.assertEqual(result['message'], '当前参与用户太多啦，请稍后重试')
                self.assertNotIn(code, result['message'])
        worker.post = Mock(return_value=Response({'code': 'FAIL', 'status': 'RISK_VERIFY'}))
        with self.assertRaises(CheckinError) as raised:
            prepared.send(before_submit=Mock())
        self.assertEqual(raised.exception.code, 'VERIFICATION_REQUIRED')


if __name__ == '__main__':
    unittest.main()
