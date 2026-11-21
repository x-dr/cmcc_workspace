"""China Mobile net-age benefits over the shared COC account session."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import json
import re
import secrets
from threading import RLock
import time

from flask import jsonify, render_template, request

from activities.coc_client import ORIGIN, CocError
from feature_runtime import feature_running
from http_transport import requests
from net_age_client import (BADGE_GROUPS, BIRTH_IDS, CHANNEL, PAGE_ID, SURPRISE_ID,
                            NetAgeClient, claim_config, offer_received)
from recharge.cmcc import SHANGHAI, mask_phone


SURPRISE_ENTRY = ORIGIN + '/coc3/canvas/rightsmarket-h5-canvas/online/wljxmh?channelCode=P00000122499'
SURPRISE_SCOPE = 'rights-market:net-age-surprise'
SURPRISE_LOTTERY = '/coc3/coc3-market-activity/cmpp-manage/lottery/'
AVAILABILITY_SCOPE = 'net-age-availability'
RETRY_GROUPS = ('flow', 'member')


@dataclass
class Verification:
    id: str
    client: object
    group: str
    offer: dict
    period: str
    deadline: float
    needs_slider: bool
    ordinate: int = 0
    challenge: str = ''
    access_token: str = ''
    sms_sent_at: float = 0
    retry: bool = False


class NetAge:
    def __init__(self, credentials, accounts, cache, storage, recover, *, now=None):
        self.credentials, self.accounts, self.cache, self.storage, self.recover = credentials, accounts, cache, storage, recover
        self.now = now or (lambda: datetime.now(SHANGHAI))
        self.lock = RLock()
        self.account_locks = {}
        self.pending = {}
        self.submissions = (storage.load_activity_data('net-age-submissions') or {}) if storage else {}
        self.availability = (storage.load_activity_data(AVAILABILITY_SCOPE) or {}) if storage else {}

    @contextmanager
    def connected(self, login_id):
        client = NetAgeClient(lambda: self.credentials(login_id), self.cache, self.recover)
        try:
            yield client.connect()
        finally:
            client.close()

    def account_lock(self, login_id):
        with self.lock:
            return self.account_locks.setdefault(login_id, RLock())

    @staticmethod
    def period(group, now):
        return now.strftime('%Y' if group == 'birthday' else '%Y-%m')

    def mark(self, login_id, group, record):
        with self.lock:
            pending = {**self.submissions.get(login_id, {})}
            if record is None:
                pending.pop(group, None)
            else:
                pending[group] = record
            updated = {**self.submissions, login_id: pending}
            if self.storage:
                self.storage.save_activity_data('net-age-submissions', updated)
            self.submissions = updated

    def busy_login(self, login_id):
        pending = self.pending.get(login_id)
        return pending is not None and pending.deadline > time.monotonic()

    def close_verification(self, login_id):
        pending = self.pending.pop(login_id, None)
        if pending is not None:
            pending.client.close()

    @staticmethod
    def _surprise_request(session, path, payload):
        try:
            response = session.post(ORIGIN + path, json=payload, timeout=(5, 12), allow_redirects=False)
        except requests.RequestsError as error:
            raise CocError('连接官方网龄惊喜礼失败，请稍后核对', 'SURPRISE_NETWORK') from error
        if response.status_code == 401:
            raise CocError('惊喜礼会话已失效', 'SURPRISE_SESSION_EXPIRED', 401)
        if response.status_code == 429:
            raise CocError('官方暂时限制查询频率，请稍后再试', 'SURPRISE_RATE_LIMITED', 429)
        if response.status_code != 200:
            raise CocError(f'官方惊喜礼接口 HTTP {response.status_code}', 'SURPRISE_HTTP')
        try:
            result = response.json()
        except ValueError as error:
            raise CocError('官方惊喜礼响应格式异常', 'SURPRISE_FORMAT') from error
        if not isinstance(result, dict):
            raise CocError('官方惊喜礼响应格式异常', 'SURPRISE_FORMAT')
        return result

    def _authorize_surprise(self, login_id, snapshot, session):
        phone = mask_phone(snapshot['phone'])
        with self.connected(login_id) as activity:
            if activity.user.get('maskedMobile') != phone:
                raise CocError('网龄会话账号与所选号码不一致', 'SURPRISE_ACCOUNT_MISMATCH', 409)
            token = activity.user.get('token')
            if not token:
                raise CocError('官方网龄会话未提供惊喜礼授权', 'SURPRISE_AUTH_REQUIRED', 401)
            activity.post('user/authenticationLogin', {'token': token}, mutation=True)
            result = activity.post('user/getCmPassPortTokenByUid', {'token': token, 'targetSourceId': '056002'}, mutation=True)
        ticket = result.get('cmPassPortToken') if isinstance(result, dict) else None
        if not ticket:
            raise CocError('官方未发放惊喜礼授权凭据', 'SURPRISE_AUTH_REQUIRED', 401)
        result = self._surprise_request(session, '/coc3/gr/login/ssoRights/userLoginV2', {
            'ticket': ticket, 'ticketType': 8, 'businessType': 'RIGHTS_MARKET', 'wxoaType': '',
            'm1': '', 'm2': '', 's1': '', 's2': '', 'from': '',
        })
        if str(result.get('code')) != '0':
            raise CocError('官方惊喜礼授权未通过，请在中国移动 APP 核对登录', 'SURPRISE_AUTH_REQUIRED', 401)
        user = result.get('data')
        if not isinstance(user, dict) or user.get('mobileMask') != phone or not user.get('token'):
            raise CocError('官方惊喜礼账号绑定未通过', 'SURPRISE_ACCOUNT_MISMATCH', 409)
        session.headers['phone'] = user['token']
        return {}

    def _read_surprise(self, session, path, payload):
        def read():
            response = self._surprise_request(session, SURPRISE_LOTTERY + path, payload)
            data = response.get('data')
            code = data.get('resultCode') if isinstance(data, dict) else None
            if str(code) == '-516' or str(response.get('code')) == '-516':
                raise CocError('惊喜礼会话已失效', 'SURPRISE_SESSION_EXPIRED', 401)
            if str(response.get('code')) != '200' or code not in (None, 0, '0'):
                raise CocError('官方尚未返回惊喜礼查询结果', 'SURPRISE_FAILED')
            return data

        try:
            return read()
        except CocError as error:
            if error.code != 'SURPRISE_SESSION_EXPIRED' or not self.cache.recover(session):
                raise
        return read()

    def surprise_details(self, login_id):
        snapshot = self.credentials(login_id)
        if snapshot is None:
            raise CocError('请先恢复该共享账号登录', 'APP_LOGIN_REQUIRED', 401)
        with requests.Session() as session:
            session.headers.update({'Origin': ORIGIN, 'Referer': SURPRISE_ENTRY, 'Accept': 'application/json'})
            self.cache.connect(SURPRISE_SCOPE, snapshot, session,
                               lambda: self._authorize_surprise(login_id, snapshot, session), headers=('phone',))
            remaining = self._read_surprise(session, 'residueLotteryCount', {'activityId': int(SURPRISE_ID)})
            if type(remaining) is not int or remaining < 0:
                raise CocError('官方抽奖次数格式异常', 'SURPRISE_FORMAT')
            awards = self._read_surprise(session, 'lotteryList', {'activityIds': [int(SURPRISE_ID)],
                                                                 'pageIndex': 1, 'pageSize': 10, 'lotteryFlag': False})
            self.cache.sync(session)
        if not isinstance(awards, dict) or not (isinstance(awards.get('list'), list)
                                                or awards.get('list') is None and str(awards.get('total')) == '0'):
            raise CocError('官方奖品记录格式异常', 'SURPRISE_FORMAT')
        prizes = [{'name': str(item.get('showName') or item.get('salesUnitName'))[:120],
                   'date': str(item.get('createTime') or '')[:30]}
                  for item in awards.get('list') or [] if isinstance(item, dict) and str(item.get('salesUnitId')) != '-1'
                  and (item.get('showName') or item.get('salesUnitName'))]
        return {'remaining': remaining, 'prizes': prizes}

    def _save_availability(self, login_id, record):
        if record == self.availability.get(login_id):
            return
        updated = {**self.availability, login_id: record}
        if self.storage:
            self.storage.save_activity_data(AVAILABILITY_SCOPE, updated)
        self.availability = updated

    def available_counts(self, login_ids):
        now = self.now()
        month = now.strftime('%Y-%m')
        with self.lock:
            return {
                login_id: sum(
                    self.submissions.get(login_id, {}).get(group, {}).get('period') != self.period(group, now)
                    for group in record['groups']
                )
                for login_id, record in self.availability.items()
                if login_id in login_ids and record.get('month') == month and 'groups' in record
            }

    def check_availability(self, login_id):
        now = self.now()
        day = now.date().isoformat()
        with self.lock:
            record = self.availability.get(login_id, {})
            if record.get('attempt_day') == day or self.busy_login(login_id):
                return self.available_counts({login_id}).get(login_id)
            self._save_availability(login_id, {**record, 'attempt_day': day})
        with self.connected(login_id) as client:
            rows = client.inspect(now, badges_only=True)['rows']
            return self._record_availability(login_id, now, rows)

    def _record_availability(self, login_id, now, rows):
        with self.lock:
            available = [group for group in BADGE_GROUPS if rows[group]['available']]
            for group, row in rows.items():
                record = self.submissions.get(login_id, {}).get(group)
                if record and record['period'] == self.period(group, now):
                    if any(offer['received'] and (group in ('flow', 'member', 'birthday') or offer['id'] == record['choice'])
                           for offer in row['offers']):
                        self.mark(login_id, group, None)
                    elif group in RETRY_GROUPS:
                        has_orders = row['has_period_orders']
                        row.update(status='结果待确认', description='已核对官方订单，发现本月订单，领取项目待确认' if has_orders
                                   else '已核对官方订单，本月暂未查到领取记录',
                                   retry_available=row['available'] and not has_orders, recheck=True, available=False)
                    elif group == 'birthday':
                        row.update(status='结果待确认', description='已查询官方订单，暂未查到领取记录', available=False)
                    else:
                        row.update(status='结果待核对', available=False)
            previous = self.availability.get(login_id, {})
            stamp = now.isoformat()
            if stamp >= previous.get('checked_at', ''):
                self._save_availability(login_id, {'month': now.strftime('%Y-%m'), 'checked_at': stamp,
                    'attempt_day': max(now.date().isoformat(), previous.get('attempt_day', '')), 'groups': available})
            return self.available_counts({login_id}).get(login_id, 0)

    def status(self, login_id, client):
        now = self.now()
        value = client.inspect(now)
        value['available_count'] = self._record_availability(login_id, now, value['rows'])
        for row in value['rows'].values():
            row['offers'] = [{key: offer[key] for key in ('id', 'name', 'received', 'mode')} for offer in row['offers']]
        try:
            remaining = client.surprise_chances()
            surprise = {'status': f'专区剩余 {remaining} 次', 'section_remaining': remaining,
                        'description': '实际次数以官方抽奖页为准，官方活动限 APP 内抽奖'}
        except CocError as error:
            surprise = {'status': '次数待查询', 'description': str(error)}
        value['rows']['surprise'] = surprise
        return value

    def _offer(self, client, login_id, group, choice, now, *, recover=True, retry=False):
        row = client.inspect(now, recover=recover)['rows'][group]
        offer = next((item for item in row['offers'] if item['id'] == choice and not item['received']), None)
        if not row['available'] or offer is None:
            raise CocError('当前选项不可领取，请刷新官方状态', 'NOT_AVAILABLE', 409)
        pending = self.submissions.get(login_id, {}).get(group, {})
        if (pending.get('period') == self.period(group, now)
                and not (retry and group in RETRY_GROUPS and not row['has_period_orders'])):
            raise CocError('该权益有一笔结果待核对，请先刷新', 'SUBMISSION_PENDING', 409)
        client.post('activities/growth/verify/status', {}, recover=recover)
        return offer

    def begin(self, login_id, group, choice, *, retry=False):
        with self.account_lock(login_id):
            if self.busy_login(login_id):
                raise CocError('该账号有一笔正在验证的领取，请先完成或取消', 'ACCOUNT_BUSY', 409)
            self.close_verification(login_id)
            client = NetAgeClient(lambda: self.credentials(login_id), self.cache, self.recover)
            try:
                client.connect()
                now = self.now()
                offer = self._offer(client, login_id, group, choice, now, retry=retry)
                if offer['mode'] == 'direct':
                    return self._submit(client, login_id, group, offer, self.period(group, now))
                needs_slider = group == 'member' or offer['activity'] == BIRTH_IDS[1]
                if group == 'flow':
                    provinces = str(claim_config().get('configContent_27', '')).split(',')
                    needs_slider = client.user.get('provinceCode') in {province.strip() for province in provinces}
                operation = Verification(secrets.token_urlsafe(18), client, group, offer, self.period(group, now),
                                         time.monotonic()+600, needs_slider, retry=retry)
                self.pending[login_id] = operation
                return {'operation': operation.id, 'needs_slider': needs_slider, 'offer': offer['name'],
                        'notice': '请确认当前手机号已绑定 QQ 音乐账号。领取会使用本月唯一名额。' if offer['id'] == 'qq-music' else ''}
            finally:
                if login_id not in self.pending:
                    client.close()

    def verification(self, login_id, operation_id, action, payload):
        with self.account_lock(login_id):
            operation = self.pending.get(login_id)
            if operation is None or operation.id != operation_id:
                raise CocError('请重新选择要领取的权益', 'VERIFICATION_MISSING', 409)
            if action == 'cancel' or not self.busy_login(login_id):
                self.close_verification(login_id)
                if action == 'cancel':
                    return {'ok': True}
                raise CocError('领取确认已超时，请重新选择权益', 'VERIFICATION_EXPIRED', 409)
            client = operation.client
            if action == 'challenge':
                data = client.post('user/auth/getSliderImg', {'mobilePhone': client.user.get('userId', '')}, mutation=True)
                if not isinstance(data, dict) or not all(data.get(key) for key in ('ground','patch','authToken')):
                    raise CocError('官方没有返回可用的滑块验证', 'SLIDER_FORMAT')
                if not re.fullmatch(r'[0-9]{1,3}', str(data.get('ordinate'))):
                    raise CocError('官方滑块位置格式异常', 'SLIDER_FORMAT')
                operation.ordinate = int(data['ordinate'])
                operation.challenge = data['authToken']
                operation.access_token = ''
                return {'ground': data['ground'], 'patch': data['patch'], 'ordinate': operation.ordinate}
            if action == 'slider':
                if not operation.challenge:
                    raise CocError('请先获取官方滑块', 'SLIDER_REQUIRED', 409)
                token, operation.challenge = operation.challenge, ''
                data = client.post('user/auth/checkSliderImg', {'authToken': token, 'ordinate': operation.ordinate,
                                   'abscissa': payload['position']}, mutation=True)
                access = (data.get('accessToken') or data.get('token') or data.get('authToken')) if isinstance(data, dict) else None
                if not access:
                    raise CocError('官方滑块验证凭证缺失，请重新验证', 'SLIDER_FORMAT')
                operation.access_token = access
                return {'message': '官方滑块验证通过'}
            if operation.needs_slider and not operation.access_token:
                raise CocError('请先完成官方滑块验证', 'SLIDER_REQUIRED', 409)
            if action == 'sms':
                elapsed = time.monotonic()-operation.sms_sent_at if operation.sms_sent_at else 60
                if elapsed < 60:
                    raise CocError('请等待 60 秒后重新获取验证码', 'SMS_COOLDOWN', 409)
                offer = operation.offer
                client.post('user/smsCode', {'mobile': client.user.get('maskedMobile', ''), 'userId': client.user.get('userId', ''),
                            'productCode': offer['sms_product'], 'pageId': PAGE_ID, 'touchCode': CHANNEL,
                            'smsCodeTypeEnum': '505'}, mutation=True,
                            before_submit=lambda: setattr(operation, 'sms_sent_at', time.monotonic()))
                return {'message': '官方已发送验证码'}
            if not operation.sms_sent_at:
                raise CocError('请先获取官方短信验证码', 'SMS_REQUIRED', 409)
            now = self.now()
            if operation.period != self.period(operation.group, now):
                self.close_verification(login_id)
                raise CocError('权益领取周期已变更，请重新查询', 'PERIOD_CHANGED', 409)
            offer = self._offer(client, login_id, operation.group, operation.offer['id'], now,
                                recover=False, retry=operation.retry)
            if offer != operation.offer:
                self.close_verification(login_id)
                raise CocError('官方权益已更新，请重新选择', 'OFFER_CHANGED', 409)
            try:
                return self._submit(client, login_id, operation.group, offer, operation.period,
                                    code=payload['code'], access=operation.access_token)
            finally:
                self.close_verification(login_id)

    def _submit(self, client, login_id, group, offer, period, *, code='', access=''):
        extra = {'touchCode': CHANNEL}
        if code:
            extra.update(smsCode=code, productCode=offer['sms_product'])
        if offer.get('sku'):
            extra['activitySkuId'] = offer['sku']
        if offer.get('right'):
            extra['rightSkuId'] = offer['right']
        if access:
            extra['accessToken'] = access
        wheel = bool(code) and offer['activity'] != BIRTH_IDS[0]
        path = 'activities/prize/templatePrize' + ('/new' if wheel else '')
        payload = {'lotteryId' if wheel else 'activityId': offer['activity'], 'provinceCode': client.user.get('provinceCode', ''),
                   'jsonExtParam': json.dumps(extra, separators=(',', ':'))}
        previous = self.submissions.get(login_id, {}).get(group)
        record = {'choice': offer['id'], 'period': period}
        submitted = False

        def before_submit():
            nonlocal submitted
            if not feature_running():
                raise CocError('网龄已关闭，领取流程已停止', 'FEATURE_DISABLED', 423)
            record['submitted_at'] = self.now().isoformat()
            self.mark(login_id, group, record)
            submitted = True

        try:
            client.post(path, payload, encryption='wheel' if wheel else 'aurora',
                        mutation=True, before_submit=before_submit)
        except CocError as error:
            if not submitted:
                raise
            ambiguous = error.code == '10' and str(error).startswith('领取失败')
            if not error.uncertain and error.code != 'UPSTREAM_FORMAT' and not ambiguous:
                self.mark(login_id, group, previous)
                raise
            self.mark(login_id, group, {**record, 'response_code': error.code})
        try:
            orders = client.orders([offer['activity']], recover=False)
        except CocError:
            return {'message': '领取结果待核对，请稍后刷新', 'confirmed': False}
        confirmed = [item for item in orders if str(item.get('createTime', '')).startswith(period)]
        if offer_received(confirmed, offer):
            with self.lock:
                available = self.availability.get(login_id, {})
                if group in available.get('groups', ()):
                    self._save_availability(login_id, {**available, 'checked_at': self.now().isoformat(),
                        'groups': [remaining for remaining in available['groups'] if remaining != group]})
                self.mark(login_id, group, None)
            message = '官方记录已确认领取'
            if offer['id'] == 'qq-music':
                try:
                    binding = client.post('activities/growth/account/query', {'brand': 'QQ音乐'}, recover=False)
                except CocError:
                    binding = None
                nickname = binding.get('nickname') if isinstance(binding, dict) else None
                if isinstance(nickname, str) and nickname:
                    nickname = nickname if len(nickname) <= 10 else nickname[:6]+'****'
                    message += f'。发放账号：{nickname}'
                else:
                    message += '。QQ 音乐账号绑定状态待核对，请在 QQ 音乐确认到账。'
            return {'message': message, 'confirmed': True}
        return {'message': '领取结果待核对，请稍后刷新', 'confirmed': False}


def register_net_age(app, credentials, accounts, cache, storage, recover):
    service = NetAge(credentials, accounts, cache, storage, recover)
    app.extensions['net_age'] = service

    @app.get('/net-age')
    def net_age_page():
        return render_template('net_age.html')

    @app.get('/api/net-age/accounts')
    def net_age_accounts():
        rows = service.accounts()
        return jsonify(accounts=rows, available_counts=service.available_counts({account['id'] for account in rows
                       if account['status'] == 'active'}))

    @app.get('/api/net-age/availability')
    def net_age_availability():
        login_id = request.args.get('id')
        if not any(account['id'] == login_id and account['status'] == 'active' for account in service.accounts()):
            return jsonify(error='请选择已登录的共享账号。'), 401
        try:
            return jsonify(count=service.check_availability(login_id))
        except CocError as error:
            return jsonify(error=str(error), code=error.code), error.status

    @app.get('/api/net-age/status')
    def net_age_status():
        login_id = request.args.get('id')
        if not login_id:
            return jsonify(error='请选择共享账号。'), 400
        try:
            with service.connected(login_id) as client:
                return jsonify(service.status(login_id, client))
        except CocError as error:
            return jsonify(error=str(error), code=error.code), error.status

    @app.get('/api/net-age/surprise')
    def net_age_surprise():
        login_id = request.args.get('id')
        if not login_id:
            return jsonify(error='请选择共享账号。'), 400
        try:
            return jsonify(service.surprise_details(login_id))
        except CocError as error:
            return jsonify(error=str(error), code=error.code), error.status

    @app.post('/api/net-age/claim')
    def net_age_claim():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or payload.get('group') not in ('flow', 'member', 'ai', 'birthday') or not all(
                isinstance(payload.get(key), str) and payload[key] for key in ('id', 'choice')):
            return jsonify(error='请选择账号和权益。'), 400
        retry = payload.get('retry', False)
        if type(retry) is not bool or retry and payload['group'] not in RETRY_GROUPS:
            return jsonify(error='请选择可重新准备的权益。'), 400
        try:
            return jsonify(service.begin(payload['id'], payload['group'], payload['choice'], retry=retry))
        except CocError as error:
            return jsonify(error=str(error), code=error.code), error.status

    @app.post('/api/net-age/verification/<action>')
    def net_age_verification(action):
        payload = request.get_json(silent=True)
        if (action not in ('challenge', 'slider', 'sms', 'submit', 'cancel') or not isinstance(payload, dict)
                or not all(isinstance(payload.get(key), str) and payload[key] for key in ('id', 'operation'))):
            return jsonify(error='请选择正在领取的权益。'), 400
        if action == 'slider' and (type(payload.get('position')) is not int or not 0 <= payload['position'] <= 360):
            return jsonify(error='请完成官方滑块验证。'), 400
        if action == 'submit' and not re.fullmatch(r'[0-9]{6}', str(payload.get('code', ''))):
            return jsonify(error='请输入 6 位短信验证码。'), 400
        try:
            return jsonify(service.verification(payload['id'], payload['operation'], action, payload))
        except CocError as error:
            return jsonify(error=str(error), code=error.code), error.status
