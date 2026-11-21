"""Official requests and benefit interpretation for the net-age activity."""
import json
import re
import time

from activities.coc_client import ORIGIN, CocClient, CocError
from http_transport import requests


PAGE_ID = '1879720710683729920'
CHANNEL = 'P00000003727'
ENTRY = f'{ORIGIN}/coc/web6/ageFeedbackV2/?pageId={PAGE_ID}&channelId={CHANNEL}'
CONFIG_URL = f'https://res.coc.10086.cn/res/cdn/coc1/fixedPath/{PAGE_ID}.js'
FLOW_IDS = ('1920407540822495232', '1920408316635406336', '1920408673400066048')
FLOW_CODES = ('130000006837', '130000006838', '130000006839')
MEMBER_ID = '1920404735896162304'
AI_IDS = ('1920409168738979840', '1920410150969118720')
BIRTH_IDS = ('1920398686957129728', '1920402448994754560')
BIRTH_COUPONS = (
    ('phone', '话费充值满减券', '130000006303', 5, {str(count) * 6: count for count in range(1, 7)}),
    ('cinema', '电影票抵扣券', '200000043066', 30, {'1111': 1, '2222': 2, '3333': 1, '4444': 2}),
)
SURPRISE_ID = '16096'
ORDER_IDS = [*FLOW_IDS, MEMBER_ID, *AI_IDS, *BIRTH_IDS]
BADGE_GROUPS = ('member', 'birthday')
BADGE_ORDER_IDS = [MEMBER_ID, *BIRTH_IDS]

# Product-code aliases also appear in order history when an offer is replaced.
AI_OFFERS = (
    ('tools', 'AI 工具', '1920409785622544384', ('200000043068', '200000046591')),
    ('create', 'AI 创作', '1943612386680303616', ('200000043097', '200000043072', '200000046593')),
    ('sport', 'AI 赛事', '1940714322694389760', ('200000043084', '200000034618', '200000046594')),
    ('call', 'AI 通话', '1940714322711166976', ('200000043095', '200000043067', '200000043069', '200000046592')),
)
AI_HIGH_SKUS = ('1920410151036227584', '1943612369417064448', '1940714346484817920', '1940714346509983744')
MEMBER_OFFERS = (
    ('new-call', '玩转新通话', '200000043094', '2102216125856206848', '67136', '1006'),
    ('migu-video', '咪咕视频', '200000043084', '2102310701592678400', '67164', '1007'),
    ('ringtone', '视频彩铃', '200000047332', '2102216125831041024', '67133', '1008'),
    ('migu-game', '咪咕游戏', '200000047333', '2102216125877178368', '67132', '1009'),
    ('cloud', '移动云盘', '200000047331', '2102216125889761280', '67134', '1010'),
    ('iqiyi', '爱奇艺', '200000034555', '1920407016246972416', '58876', '200000034555'),
    ('youku', '优酷视频', '200000034557', '1920407227344568320', '58878', '200000034557'),
    ('mango', '芒果 TV', '200000034564', '1920755073566576640', '59011', '200000034564'),
    ('bilibili', '哔哩哔哩', '200000034558', '2027191931370758144', '63992', '200000034558'),
    ('qq-music', 'QQ 音乐', '200000034571', '1920407115767119872', '58877', '200000034571'),
)


def _order_rows(data, ids):
    grouped = data.get('activityOrderDTO') if isinstance(data, dict) else None
    if grouped is None and isinstance(data, dict) and str(data.get('total')) == '0':
        grouped = {}
    if not isinstance(grouped, dict):
        raise CocError('官方未返回完整领取记录', 'ORDER_FORMAT')
    rows = []
    for activity in ids:
        values = grouped.get(activity) or []
        if not isinstance(values, list) or any(not isinstance(row, dict) for row in values):
            raise CocError('官方返回的领取记录格式异常', 'ORDER_FORMAT')
        rows.extend({**row, 'activity_id': activity} for row in values)
    return rows


def offer_received(orders, offer):
    return any(row['activity_id'] in offer.get('history_ids', (offer['activity'],)) and
               (str(row.get('prizeId')) in offer['prizes'] or str(row.get('skuCode')) in offer['products']) for row in orders)


def claim_config():
    try:
        with requests.Session() as session:
            response = session.get(CONFIG_URL, params={'t': int(time.time() // 300)}, timeout=(5, 12))
            response.raise_for_status()
            match = re.search(r'\bconfigData\s*=\s*(\{[^\n]+\})', response.text)
        value = json.loads(match[1]) if match else {}
        if str(value.get('pageId')) != PAGE_ID:
            raise ValueError('unexpected page')
        return {key: item[key] for item in value['configItemList'] for key in item if key.startswith('configContent_')}
    except (requests.RequestsError, ValueError, KeyError, TypeError) as error:
        raise CocError('暂时无法核对官方领取配置，请稍后重试', 'CONFIG_FAILED') from error


class NetAgeClient(CocClient):
    def __init__(self, provider, cache, recover):
        super().__init__(provider, entry=ENTRY, credential_cache=cache, app_session_recover=recover)

    def orders(self, ids, *, recover=True):
        return _order_rows(self.post('activities/prize/activityOrderListNew', {'activityIds': ids}, recover=recover), ids)

    def surprise_chances(self):
        data = self.post('activities/prize/remainingQueryTimes', {'activityIds': [SURPRISE_ID]})
        try:
            body = data['contractRoot']['body']
            count = body['data'][0]['remainNum']
            if str(body['resultCode']) == '0' and re.fullmatch(r'\d+', str(count)):
                return int(count)
        except (KeyError, TypeError, IndexError):
            pass
        raise CocError('官方未返回有效的惊喜礼次数', 'SURPRISE_FORMAT')

    def inspect(self, now, *, recover=True, badges_only=False):
        profile = self.post('activities/common/queryUserInfo', {}, recover=recover)
        growth = self.post('activities/growth/query', {'touchCode': CHANNEL, 'provinceCode': self.user.get('provinceCode', '')},
                           recover=recover)
        level = growth.get('level') if isinstance(growth, dict) else None
        if not isinstance(profile, dict) or not isinstance(level, str):
            raise CocError('官方未返回完整的网龄资格', 'PROFILE_FORMAT')
        orders = self.orders(BADGE_ORDER_IDS if badges_only else ORDER_IDS, recover=recover)
        month = now.strftime('%Y-%m')
        year = now.strftime('%Y-')
        current = [row for row in orders if str(row.get('createTime', '')).startswith(month)]
        annual = [row for row in orders if str(row.get('createTime', '')).startswith(year)]
        rank = int(level[1]) if level in ('V1', 'V2', 'V3', 'V4', 'V5') else 0
        years = re.match(r'(\d+)年', str(profile.get('userChinaMobileage', '')))
        birth_date = str(profile.get('userBegin', ''))
        rows = self.benefits(rank, years, profile.get('isBirthMonth'), current, annual)
        if badges_only:
            rows = {group: rows[group] for group in BADGE_GROUPS}
        return {'level': level, 'age': str(profile.get('userChinaMobileage', '')),
                'birthday': f'{birth_date[4:6]}月{birth_date[6:8]}日' if re.fullmatch(r'\d{8}', birth_date) else '',
                'rows': rows}

    def benefits(self, rank, years, birth_month, current, annual):
        flow_tier = 0 if rank < 3 else 1 if rank < 5 else 2
        flow_claimed = any(row['activity_id'] in FLOW_IDS and str(row.get('skuCode')) in FLOW_CODES for row in current)
        member_offers = [dict(id=key, name=name, prizes=(sku,), products=(product,), sms_product=sms,
                              sku=sku, right=right, activity=MEMBER_ID, mode='sms')
                         for key, name, product, sku, right, sms in MEMBER_OFFERS]
        if self.user.get('provinceCode') in ('898','431','871','531','351','791','471','230'):
            member_offers[2].update(sku='2084941388956573696', right='66096', sms_product='1003',
                                    prizes=('2084941388956573696','2102216125831041024'),
                                    products=('200000034817','200000047332'))
        member_claimed = any(offer_received(current, offer) for offer in member_offers)
        ai_tier = int(rank >= 3)
        ai_offers = [dict(id=key, name=name, activity=AI_IDS[ai_tier],
                          sku=AI_HIGH_SKUS[index] if ai_tier else sku, prizes=(sku, AI_HIGH_SKUS[index]),
                          products=products, mode='direct', history_ids=AI_IDS)
                     for index, (key, name, sku, products) in enumerate(AI_OFFERS)]
        ai_limit = (1 + ai_tier) if rank else 0
        ai_used = sum(offer_received(current, offer) for offer in ai_offers)
        birthday_claimed = False
        birthday_ready = bool(years and int(years[1]) >= 1 and birth_month in (True, '1'))
        birthday_offers = []
        for activity, (key, name, sms, amount, counts) in zip(BIRTH_IDS, BIRTH_COUPONS):
            orders = (order for order in annual if order['activity_id'] == activity and str(order.get('skuCode')) in counts)
            latest = max(orders, key=lambda order: order['createTime'], default=None)
            if latest is not None:
                count = counts[str(latest['skuCode'])]
                name = f'{count} 张 {amount} 元{name}'
                minimum = re.search(r'充值\s*(\d+(?:\.\d+)?)\s*元?\s*立减', str(latest.get('skuName', ''))) if key == 'phone' else None
                if minimum:
                    name += f'（满 {minimum[1]} 元可用）'
                birthday_claimed = True
            birthday_offers.append(dict(id=key, name=name, activity=activity, mode='sms', prizes=(),
                                        sms_product=sms, products=tuple(counts)))
        return {
            'flow': self.row(f'{flow_tier + 1}GB 流量月包券', rank > 0, flow_claimed, current, [
                dict(id='monthly', name=f'{flow_tier + 1}GB 流量月包券', activity=FLOW_IDS[flow_tier],
                     prizes=(), products=FLOW_CODES, sms_product=FLOW_CODES[flow_tier], mode='sms', history_ids=FLOW_IDS)],
                has_period_orders=any(row['activity_id'] in FLOW_IDS for row in current)),
            'member': self.row('每月任选 1 款', rank == 5, member_claimed, current, member_offers,
                               unavailable='成长等级 V5 可领', has_period_orders=any(row['activity_id'] == MEMBER_ID for row in current)),
            'ai': self.row(f'本月已选 {ai_used}/{ai_limit} 款', rank > 0, bool(ai_limit and ai_used >= ai_limit),
                           current, ai_offers),
            'birthday': self.row('生日月任选一款', birthday_ready, birthday_claimed, annual, birthday_offers,
                                 unavailable='本月不可领取'),
        }

    @staticmethod
    def row(description, eligible, claimed, orders, offers, *, unavailable='当前账号暂无资格', has_period_orders=False):
        confirmed = [offer for offer in offers if offer_received(orders, offer)]
        return {'description': description, 'status': '已领取' if claimed else '可领取' if eligible else unavailable,
                'offers': [{**offer, 'received': offer in confirmed} for offer in offers] if eligible or claimed else [],
                'available': eligible and not claimed, 'has_period_orders': has_period_orders}
