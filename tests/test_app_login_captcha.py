import time
import unittest
from unittest.mock import Mock, patch

from app_login import AppLoginError, AppLoginSession, LOGIN_CAPTCHA_APP_ID, LOGIN_PATH


PHONE = '19900000003'
OTP = '123456'
TOKEN = 'private-human-verified-token'


def flow():
    return AppLoginSession(phone=PHONE, sms_sent_at=time.monotonic(),
                           sms_request_id='sms-batch', initialized_at=time.monotonic())


def challenge(session):
    with patch.object(session, 'call', return_value=(200, {'retCode': '214100'}, {})) as send:
        with unittest.TestCase().assertRaises(AppLoginError) as raised:
            session.login(PHONE, OTP)
    return raised.exception.challenge, send.call_args.args[1]


class LoginCaptchaTests(unittest.TestCase):
    def test_sms_challenge_does_not_pretend_a_login_challenge_or_successful_send_exists(self):
        session = AppLoginSession(initialized_at=time.monotonic())
        with patch.object(session, 'call', return_value=(200, {'retCode': '214100'}, {})):
            with self.assertRaises(AppLoginError) as raised:
                session.send_sms(PHONE)
        self.assertIsNone(raised.exception.challenge)
        self.assertFalse(session.phone)
        self.assertNotIn('当前页面', str(raised.exception))

    def test_human_verification_resumes_original_request_with_risk_token_and_same_session(self):
        session = flow()
        config, original = challenge(session)
        self.assertEqual(config['app_id'], LOGIN_CAPTCHA_APP_ID)
        self.assertEqual(session.sms_status(reused=True)['challenge'], config)
        self.assertNotIn(OTP, repr(session))
        self.assertNotIn(OTP, str(session.sms_status()))
        self.assertNotIn('slider_login_body', session.export_context())

        def success(path, body):
            self.assertEqual(path, LOGIN_PATH)
            self.assertEqual(body, {**original, 'riskToken': TOKEN})
            session.cookie = 'JSESSIONID=verified-cookie'
            return 200, {'retCode': '000000'}, {}

        with patch.object(session, 'call', side_effect=success) as send, patch.object(session, 'send_sms') as sms:
            cookie = session.login(PHONE, '', risk_token=TOKEN, challenge_id=config['id'])
            self.assertEqual(session.login(PHONE, '', risk_token=TOKEN, challenge_id=config['id']), cookie)
        send.assert_called_once()
        sms.assert_not_called()
        self.assertTrue(session.app_authenticated)
        self.assertFalse(session.slider_required)
        self.assertIsNone(session.slider_challenge())
        self.assertEqual(session.slider_login_body, {})

    def test_other_account_stale_challenge_and_invalid_tokens_never_send_login(self):
        session = flow()
        config, _ = challenge(session)
        other, _ = challenge(flow())
        with patch.object(session, 'call') as send:
            for token, challenge_id in [(TOKEN, other['id']), (TOKEN, ''), (123, config['id']),
                                        ('x'*4097, config['id']), (TOKEN, [])]:
                with self.subTest(challenge=challenge_id), self.assertRaises(AppLoginError):
                    session.login(PHONE, '', risk_token=token, challenge_id=challenge_id)
            with self.assertRaises(AppLoginError) as raised:
                session.login('19900000004', '', risk_token=TOKEN, challenge_id=config['id'])
            self.assertNotEqual(raised.exception.code, '000000')
        send.assert_not_called()
        self.assertEqual(session.slider_challenge(), config)

    def test_expiry_and_user_requested_resend_invalidate_old_captcha(self):
        for expire in (False, True):
            with self.subTest(expire=expire):
                session = flow()
                config, _ = challenge(session)
                session.sms_sent_at -= 601 if expire else 61
                with patch.object(session, 'call', return_value=(200, {'retCode': '000000'}, {})) as send:
                    if expire:
                        with self.assertRaises(AppLoginError) as raised:
                            session.login(PHONE, '', risk_token=TOKEN, challenge_id=config['id'])
                        self.assertEqual(raised.exception.code, 'SMS_EXPIRED')
                        send.assert_not_called()
                    else:
                        session.send_sms(PHONE)
                        with self.assertRaises(AppLoginError) as raised:
                            session.login(PHONE, '', risk_token=TOKEN, challenge_id=config['id'])
                        self.assertEqual(raised.exception.code, 'CAPTCHA_FLOW_CHANGED')
                        self.assertEqual(send.call_count, 1)
                self.assertIsNone(session.slider_challenge())
                self.assertEqual(session.slider_login_body, {})

    def test_timeout_rejection_and_missing_cookie_do_not_replay_verified_login(self):
        for response in (AppLoginError('timeout'), (200, {'retCode': '214100'}, {}),
                         (200, {'retCode': '000000'}, {})):
            with self.subTest(response=response):
                session = flow()
                config, _ = challenge(session)
                kwargs = {'side_effect': response} if isinstance(response, Exception) else {'return_value': response}
                with patch.object(session, 'call', **kwargs) as send, patch.object(session, 'send_sms') as sms:
                    for _ in range(2):
                        with self.assertRaises(AppLoginError):
                            session.login(PHONE, '', risk_token=TOKEN, challenge_id=config['id'])
                    with self.assertRaises(AppLoginError):
                        session.login(PHONE, OTP)
                send.assert_called_once()
                sms.assert_not_called()
                self.assertIsNone(session.slider_challenge())
                self.assertEqual(session.slider_login_body, {})
                self.assertFalse(session.app_authenticated)

    def test_explicit_wrong_sms_response_allows_user_to_correct_code(self):
        session = flow()
        config, _ = challenge(session)
        with patch.object(session, 'call', return_value=(200, {'retCode': '213010'}, {})) as send:
            with self.assertRaises(AppLoginError) as raised:
                session.login(PHONE, '', risk_token=TOKEN, challenge_id=config['id'])
        self.assertEqual(raised.exception.code, '213010')
        self.assertFalse(session.slider_required)
        with patch.object(session, 'call', return_value=(200, {'retCode': '214100'}, {})):
            with self.assertRaises(AppLoginError):
                session.login(PHONE, '654321')
        self.assertNotEqual(session.slider_challenge()['id'], config['id'])
        self.assertEqual(session.slider_login_body['verifyCode'], '654321')


class LoginCaptchaRouteTests(unittest.TestCase):
    def setUp(self):
        from app import create_app
        self.app = create_app()
        self.app.testing = True
        self.http = self.app.test_client()
        self.http.get('/')
        self.app.extensions['feature_control'].set_enabled('lingxi', False)
        self.session = flow()
        self.app.extensions['unified_logins'].pending[PHONE] = self.session

    def test_login_route_returns_challenge_and_remembers_account_only_after_official_success(self):
        body = {'phone': PHONE, 'code': OTP, 'sms_request_id': 'sms-batch'}
        with patch.object(self.session, 'call', return_value=(200, {'retCode': '214100'}, {})):
            response = self.http.post('/api/logins/login', json=body)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json['code'], '214100')
        self.assertEqual(self.http.get('/api/logins').json['accounts'], [])
        config = response.json['challenge']
        original = dict(self.session.slider_login_body)
        body.update(code='', risk_token=TOKEN, challenge_id=config['id'])

        def success(path, payload):
            self.assertEqual(payload, {**original, 'riskToken': TOKEN})
            self.session.cookie = 'JSESSIONID=private-cookie'
            return 200, {'retCode': '000000'}, {}

        with patch.object(self.session, 'call', side_effect=success) as send:
            response = self.http.post('/api/logins/login', json=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.http.get('/api/logins').json['accounts']), 1)
        send.assert_called_once()
        for secret in (TOKEN, 'private-cookie', OTP, PHONE):
            self.assertNotIn(secret, response.get_data(as_text=True))

    def test_changed_sms_batch_rejects_captcha_before_sending_login(self):
        config, _ = challenge(self.session)
        with patch.object(self.session, 'call') as send:
            response = self.http.post('/api/logins/login', json={
                'phone': PHONE, 'code': '', 'sms_request_id': 'old-batch',
                'risk_token': TOKEN, 'challenge_id': config['id']})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json['code'], 'SMS_FLOW_CHANGED')
        send.assert_not_called()

    def test_lingxi_exchange_failure_reuses_completed_app_login_without_replaying_captcha(self):
        from lingxi_client import LingxiError
        self.app.extensions['feature_control'].set_enabled('lingxi', True)
        config, _ = challenge(self.session)

        def success(path, payload):
            self.session.cookie = 'JSESSIONID=private-cookie'
            return 200, {'retCode': '000000'}, {}

        body = {'phone': PHONE, 'code': '', 'sms_request_id': 'sms-batch',
                'risk_token': TOKEN, 'challenge_id': config['id']}
        with patch.object(self.session, 'call', side_effect=success) as send, \
             patch('app.exchange_cookie', side_effect=[LingxiError('temporary exchange failure'), 'lingxi-token']), \
             patch('app.verify_token'):
            response = self.http.post('/api/logins/login', json=body)
            self.assertEqual(response.status_code, 400)
            self.assertTrue(response.json['app_login_complete'])
            self.assertTrue(self.session.sms_status()['app_login_complete'])
            body.pop('risk_token')
            body.pop('challenge_id')
            response = self.http.post('/api/logins/login', json=body)
            self.assertEqual(response.status_code, 200)
        send.assert_called_once()

    def test_login_widget_and_form_proxy_work_when_coupon_feature_is_disabled(self):
        self.app.extensions['feature_control'].set_enabled('coupons', False)
        response = self.http.get('/static/login-captcha.html')
        self.assertEqual(response.status_code, 200)
        self.assertIn("frame-ancestors 'self'", response.headers['Content-Security-Policy'])
        self.assertNotIn("script-src 'self' 'unsafe-inline'", response.headers['Content-Security-Policy'])
        response.close()
        upstream = Mock(status_code=200, content=b'official verification response', headers={})
        with patch('coupon_captcha.requests.Session.request', return_value=upstream) as send:
            response = self.http.post('/login-verification/mgt/decision-captcha/api/v1',
                                      data=b'token=human-input', content_type='application/x-www-form-urlencoded')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, upstream.content)
        self.assertEqual(send.call_args.kwargs['data'], b'token=human-input')
        self.assertNotIn('Cookie', send.call_args.kwargs['headers'])
        self.assertEqual(self.http.get('/login-verification/wallet/udid/c1').status_code, 404)
        self.assertEqual(self.http.post('/login-verification/mgt/decision-captcha/api/v1',
            data=b'token=x', content_type='application/x-www-form-urlencoded',
            headers={'Origin': 'https://other.example'}).status_code, 403)


if __name__ == '__main__':
    unittest.main()
