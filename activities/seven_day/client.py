"""Official 7-day golden-autumn campaign; independent COC session per account.

Protocol: dailyLoginEvent index-BsvlzBlx / daily-login.service-CCdoEszk,
and official rules version 20261028112703. Request construction and crypto
belong to this Python module; no browser is part of business execution.
"""
from datetime import datetime
import json
from pathlib import Path
import re
import time

from activities.coc_client import API, ORIGIN, CocClient, CocError as SevenDayError
from http_transport import requests
from recharge.cmcc import SHANGHAI

PAGE_ID = '2090341662452445184'
CHANNEL = 'P00000129418'
ENTRY = ORIGIN + '/coc/web6/dailyLoginEvent/?pageId=' + PAGE_ID + '&channelId=' + CHANNEL
CONFIG_URL = 'https://res.coc.10086.cn/res/cdn/coc1/fixedPath/' + PAGE_ID + '.js'
SIGN_ID = '2110926553960751104'
DRAW_ID = '2110928691993350144'
REWARD_ID = '2110923957812281344'
REWARD_SKU = '2110933605165395968'
REWARD_CODE = '130000010667'
REWARD_RIGHT = '67425'
REWARDS = (1, .6, .5, .5, .6, .7, 1.1)
DRAW_PRIZES = dict(zip([str(i) for i in range(130000010668, 130000010676)],
                      ['88元话费兑换券', '66元话费兑换券', '0.5元话费兑换券', '1元话费兑换券',
                       '2元话费兑换券', '1GB流量日包券', '2GB流量日包券', '5GB7日流量券']))
DRAW_PRIZES['999999999999'] = '未中奖'
DEFAULT_CAMPAIGN = {
    'title': '7日打卡分百万', 'start_at': '2026-10-31T09:00:00+08:00',
    'end_at': '2026-11-30T23:59:59+08:00',
    'rules_text': '连续打卡7天合计5元，每号活动期内仅领取一次，跨月累计；漏打清零。每天夺宝一次，次日0点刷新，不保证中奖。第7天领奖需短信验证；卡券24小时内到账，7天内使用。红包领完则整个活动提前结束。',
    'notice': '规则正文至12月31日，当前官方页面配置11月30日下线；本页跟随官方页面时间。',
}
DEFAULT_CAMPAIGN['rules_text'] = (Path(__file__).resolve().parent/'static/seven-day-rules.txt').read_text('utf-8')


def campaign_config(*, session=None):
    """Read the same CDN time gate as the official page, without signing in."""
    own = session is None
    client = session or requests.Session(impersonate='chrome')
    try:
        response = client.get(CONFIG_URL, params={'v': int(time.time() // 300)}, timeout=(5, 15))
        response.raise_for_status()
        match = re.search(r'var configData\s*=\s*(\{.*\})\s*\n', response.text)
        if not match:
            raise SevenDayError('官方活动时间配置格式异常', 'CONFIG_FORMAT')
        items = json.loads(match[1])['configItemList']
        fields = {key: value for item in items for key, value in item.items() if key.startswith('configContent_')}
        start, end = (datetime.strptime(fields['configContent_'+str(i)], '%Y/%m/%d %H:%M:%S').replace(tzinfo=SHANGHAI) for i in (0, 1))
        return {**DEFAULT_CAMPAIGN, 'start_at': start.isoformat(), 'end_at': end.isoformat()}
    except (requests.RequestsError, ValueError, KeyError) as error:
        raise SevenDayError('暂时无法更新官方活动时间，沿用已确认的期限', 'CONFIG_FAILED') from error
    finally:
        if own:
            client.close()


class SevenDayClient(CocClient):
    def __init__(self, credentials_provider, *, session_factory=None, sdk_encrypt=None, credential_cache=None,
                 app_session_recover=None):
        super().__init__(credentials_provider, entry=ENTRY, session_factory=session_factory,
                         credential_cache=credential_cache, app_session_recover=app_session_recover,
                         sdk_encrypt=sdk_encrypt)

    def _authenticate(self):
        data = super()._authenticate()
        # The shipped Jl() returns a growthInfo object even for level="";
        # Ba() checks that object, not whether its level is in V1–V5.
        # post() already checks the official response code. An empty level
        # must not become a local rejection before querying campaign progress.
        self.post('activities/growth/query', {'touchCode': 'P00000001090', 'provinceCode': self.user.get('provinceCode', '')})
        return data

    def inspect(self, now=None):
        now = now or datetime.now(SHANGHAI)
        marks = self.post('activities/prize/consecutiveClockInQuery', {'activityId': SIGN_ID})
        if not isinstance(marks, dict) or not isinstance(marks.get('clockInDateList'), list):
            raise SevenDayError('官方未返回完整打卡进度', 'PROGRESS_FORMAT')
        days = [str(d) for d in marks['clockInDateList']]
        orders = self.post('activities/prize/activityOrderList', {'activityId': REWARD_ID})
        chances = self.post('activities/prize/drawDayNum', {'activityId': DRAW_ID})
        remaining = int(chances['dayRemainNum'])
        draws = self.post('activities/prize/activityOrderList', {'activityId': DRAW_ID})
        today = now.strftime('%Y%m%d')
        recent = [d for d in (draws.get('activityOrderDTO') or []) if re.sub(r'\D', '', str(d.get('createTime', '')))[:8] == today]
        return {'date': now.date().isoformat(), 'days': len(days), 'dates': days,
                'total_amount': round(sum(REWARDS[:len(days)]), 1), 'checked_today': today in days,
                'completed': str(marks.get('taskCompleted')) == '0' or len(days) >= 7,
                'claimed': bool(orders.get('activityOrderDTO')), 'chances': max(0, remaining),
                'draw_prize': str(recent[-1].get('skuName') or '') if recent else '', 'eligible': True,
                'message': '已查询官方最新进度'}

    def signin(self, before_submit=None):
        # Retained by the official daily-login service, and verified on the
        # production campaign: this route advances consecutiveClockInQuery.
        # The clockIn/Wheel branch returns code10 despite a valid device token.
        return self.post('activities/prize/templatePrize', {'activityId': SIGN_ID},
                         mutation=True, before_submit=before_submit)

    def stock(self):
        data = self.post('activities/prize/stock', {'activityId': REWARD_ID, 'rightSkuId': REWARD_RIGHT})
        return int(data['stock'])

    def draw(self, before_submit=None):
        return self.post('activities/prize/templatePrize', {'activityId': DRAW_ID, 'touchCode': CHANNEL},
                         mutation=True, before_submit=before_submit)

    def send_reward_sms(self):
        return self.post('user/smsCode', {'mobile': self.snapshot['phone'], 'userId': self.user.get('userId', ''),
                         'smsCodeTypeEnum': '505', 'productCode': REWARD_CODE, 'pageId': PAGE_ID, 'touchCode': CHANNEL},
                         encryption='aurora', mutation=True)

    def claim(self, code, before_submit=None):
        extra = json.dumps({'activitySkuId': REWARD_SKU, 'smsCode': code, 'productCode': REWARD_CODE,
                            'touchCode': CHANNEL, 'rightSkuId': REWARD_RIGHT}, separators=(',', ':'))
        return self.post('activities/prize/templatePrize/new', {'lotteryId': REWARD_ID, 'jsonExtParam': extra,
                         'provinceCode': self.user.get('provinceCode') or '100'},
                         encryption='wheel', mutation=True, before_submit=before_submit)
