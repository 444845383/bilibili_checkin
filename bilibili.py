import time
import requests
from loguru import logger

class BilibiliTask:
    def __init__(self, cookie):
        self.cookie = cookie
        self.csrf = self._get_csrf()
        # 使用 Session 复用 TCP 连接，减少握手开销，提升效率与稳定性
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Referer': 'https://www.bilibili.com/',
            'Cookie': cookie
        })

    def _get_csrf(self):
        for item in self.cookie.split(';'):
            if item.strip().startswith('bili_jct'):
                return item.split('=')[1]
        return None

    def _request(self, method, url, retries=3, **kwargs):
        """统一请求入口：固定15秒超时，网络异常/风控拦截(-412)自动重试，返回JSON字典"""
        kwargs.setdefault('timeout', 15)
        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                res = self.session.request(method, url, **kwargs)
                res.raise_for_status()
                data = res.json()
                if data.get('code') == -412 and attempt < retries:
                    # 请求被风控拦截(GitHub Actions数据中心IP常见)，稍候重试
                    raise requests.HTTPError('请求被风控拦截(code -412)')
                return data
            except (requests.Timeout, requests.ConnectionError, requests.HTTPError, ValueError) as e:
                last_exc = e
                if attempt < retries:
                    wait = attempt * 2
                    logger.warning(f"请求 {url.split('?')[0]} 失败({e})，{wait}秒后重试({attempt}/{retries - 1})...")
                    time.sleep(wait)
        raise last_exc

    def get_user_info(self):
        try:
            data = self._request('GET', 'https://api.bilibili.com/x/web-interface/nav')
            if data['code'] == 0:
                return data['data']
            logger.warning(f"获取用户信息失败: {data.get('message')}")
            return None
        except Exception as e:
            logger.error(f"请求用户信息API异常: {e}")
            return None

    def get_dynamic_videos(self):
        # 原接口 x/web-interface/dynamic/region 已下线(返回-404 啥都木有)
        # 改用分区最新视频接口 newlist，返回结构同为 data.archives
        # ps=20: 扩大候选池，跳过已投过硬币的视频后仍能投满目标枚数
        url = 'https://api.bilibili.com/x/web-interface/newlist?rid=1&type=0&ps=20&pn=1'
        try:
            data = self._request('GET', url)
            if data['code'] == 0:
                return [video['bvid'] for video in data.get('data', {}).get('archives', [])]
            logger.warning(f"获取分区最新视频失败: {data.get('code')} {data.get('message')}")
            return []
        except Exception as e:
            logger.error(f"请求分区最新视频API异常: {e}")
            return []

    def get_popular_videos(self):
        # 综合热门视频(投币视频源回退备选)，ps=20 扩大候选池
        url = 'https://api.bilibili.com/x/web-interface/popular?ps=20&pn=1'
        try:
            data = self._request('GET', url)
            if data['code'] == 0:
                return [video['bvid'] for video in data.get('data', {}).get('list', [])]
            logger.warning(f"获取热门视频失败: {data.get('code')} {data.get('message')}")
            return []
        except Exception as e:
            logger.error(f"请求热门视频API异常: {e}")
            return []

    def get_ranking_videos(self):
        url = 'https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all'
        try:
            data = self._request('GET', url)
            if data['code'] == 0:
                return [video['bvid'] for video in data.get('data', {}).get('list', [])]
            logger.warning(f"获取排行榜视频失败: {data.get('code')} {data.get('message')}")
            return []
        except Exception as e:
            logger.error(f"请求排行榜视频API异常: {e}")
            return []

    def check_video_coin_status(self, bvid):
        url = f'https://api.bilibili.com/x/web-interface/archive/coins?bvid={bvid}'
        try:
            data = self._request('GET', url)
            if data['code'] == 0:
                return data['data']['multiply'] > 0
            return False
        except Exception:
            return False

    def get_today_coin_exp(self):
        """查询今日已获得的投币经验(x/member/web/exp/reward 的 data.coin，上限50=每日5枚)
        返回 int；查询失败返回 None"""
        try:
            data = self._request('GET', 'https://api.bilibili.com/x/member/web/exp/reward')
            if data['code'] == 0:
                return int(data.get('data', {}).get('coin', 0))
            logger.warning(f"查询今日投币经验失败: {data.get('message')}")
            return None
        except Exception as e:
            logger.error(f"请求今日经验API异常: {e}")
            return None

    def add_coin(self, bvid, num=1, select_like=1):
        if not self.csrf: return False, "Bili_jct(csrf) 未找到"
        url = 'https://api.bilibili.com/x/web-interface/coin/add'
        data = {'bvid': bvid, 'multiply': num, 'select_like': select_like, 'csrf': self.csrf}
        try:
            res_data = self._request('POST', url, data=data)
            if res_data['code'] == 0:
                return True, "投币成功"
            return False, res_data.get('message', '投币失败')
        except Exception as e:
            return False, str(e)

    def share_video(self, bvid):
        if not self.csrf: return False, "Bili_jct(csrf) 未找到"
        url = 'https://api.bilibili.com/x/web-interface/share/add'
        try:
            res_data = self._request('POST', url, data={'bvid': bvid, 'csrf': self.csrf})
            if res_data['code'] == 0:
                return True, "分享成功"
            return False, res_data.get('message', '分享失败')
        except Exception as e:
            return False, str(e)

    def watch_video(self, bvid):
        url = 'https://api.bilibili.com/x/click-interface/web/heartbeat'
        data = {'bvid': bvid, 'played_time': 30, 'csrf': self.csrf}
        try:
            res_data = self._request('POST', url, data=data)
            if res_data['code'] == 0:
                return True, "观看成功"
            return False, res_data.get('message', '观看失败')
        except Exception as e:
            return False, str(e)

    def live_sign(self):
        url = 'https://api.live.bilibili.com/xlive/web-ucenter/v1/sign/DoSign'
        try:
            data = self._request('GET', url)
            if data['code'] == 0:
                return True, data.get('data', {}).get('text', '直播签到成功')
            message = data.get('message', '直播签到失败')
            if data.get('code') == 1011040:
                return True, "今日已签到"
            if '下线' in message:
                # B站官方已于2023年下线直播签到活动，接口永久返回该提示
                return False, "签到活动已下线，无法使用（官方已停止该活动）"
            return False, message
        except Exception as e:
            return False, str(e)

    def manga_sign(self):
        url = 'https://manga.bilibili.com/twirp/activity.v1.Activity/ClockIn'
        try:
            data = self._request('POST', url, data={'platform': 'ios'})
            if data['code'] == 0:
                return True, "漫画签到成功"
            # 幂等处理：今日已签到(返回 code=invalid_argument, msg=clockin clockin is duplicate)视为成功
            msg = str(data.get('msg') or data.get('message') or '')
            if data.get('code') == 'invalid_argument' and 'duplicate' in msg:
                return True, "今日已签到"
            return False, msg or '漫画签到失败'
        except Exception as e:
            return False, str(e)

    # 说明：银瓜子兑换(ExchangeSilver2Coin/silver2coin)和大会员每日经验(vip/experience/clock)
    # 均已被B站官方下线(网关404/WebProxyNeedLess)，不再实现。
    # 参考项目 ZH1300/bilibili-auto-checkin 中的同名功能亦已失效。
