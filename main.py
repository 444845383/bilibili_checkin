import os
import sys
import time
import random
from datetime import datetime, timedelta, timezone
from loguru import logger
from bilibili import BilibiliTask
from push import format_push_message, send_to_pushplus

# 任务间随机间隔(1~2秒)，避免请求过快触发B站风控
def task_interval():
    time.sleep(random.uniform(1, 2))

class BeijingFormatter:
    @staticmethod
    def format(record):
        dt = datetime.fromtimestamp(record["time"].timestamp(), tz=timezone.utc)
        local_dt = dt + timedelta(hours=8)
        record["extra"]["local_time"] = local_dt.strftime('%H:%M:%S,%f')[:-3]
        return "{time:YYYY-MM-DD HH:mm:ss,SSS}(CST {extra[local_time]}) - {level} - {message}\n"

logger.remove()
logger.add(sys.stdout, format=BeijingFormatter.format, level="INFO", colorize=True)

def mask_string(s: str) -> str:
    if not isinstance(s, str) or len(s) == 0:
        return '*'
    return s[0] + '*' * (len(s) - 1)

def mask_uid(uid: str) -> str:
    uid_str = str(uid)
    if len(uid_str) <= 2:
        return uid_str[0] + '*'
    return uid_str[:2] + '*' * (len(uid_str) - 2)

def get_coin_target_videos(bili, source):
    """按配置的视频源获取投币目标，失败时自动回退(配置源->dynamic->popular->ranking保障可用)"""
    fetchers = {
        'dynamic': bili.get_dynamic_videos,
        'popular': bili.get_popular_videos,
        'ranking': bili.get_ranking_videos,
    }
    if source not in fetchers:
        logger.warning(f"未知的视频源 '{source}'(可选: dynamic/popular/ranking)，使用默认回退链。")
        order = list(fetchers.keys())
    else:
        order = [source] + [s for s in fetchers if s != source]
    for src in order:
        videos = fetchers[src]()
        if videos:
            return src, videos
        logger.warning(f"视频源 '{src}' 获取失败，尝试回退到其他视频源...")
    return None, []

def execute_coin_task(bili, user_info, config):
    coins_to_add = int(config.get('COIN_ADD_NUM'))
    if coins_to_add <= 0:
        return True, "配置为0，跳过"
    
    coin_balance = user_info.get('money', 0)
    if coin_balance < 1:
        return True, f"硬币不足({coin_balance})，跳过"
    
    coins_to_add = min(coins_to_add, int(coin_balance), 5)

    source = config.get('COIN_VIDEO_SOURCE')
    actual_source, video_list = get_coin_target_videos(bili, source)
    if actual_source != source:
        logger.warning(f"配置的视频源 '{source}' 不可用，已回退到 '{actual_source}'。")

    if not video_list:
        return False, "无法获取视频列表(所有视频源均失败)"

    added_coins = 0
    for bvid in video_list:
        if added_coins >= coins_to_add:
            break
        
        success, msg = bili.add_coin(bvid, 1, int(config.get('COIN_SELECT_LIKE')))
        if success:
            added_coins += 1
            logger.info(f"为视频 {bvid} 投币成功。")
        elif "上限" in msg or "已达到" in msg:
            logger.warning("今日投币上限已满，终止投币。")
            return True, f"今日投币已达上限，成功 {added_coins} 枚"
        else:
            logger.warning(f"为视频 {bvid} 投币失败: {msg}")
            if "硬币不足" in msg or "不足" in msg:
                break
        # 投币间随机间隔，避免连续快速操作触发风控
        time.sleep(random.uniform(1, 2))

    return True, f"尝试投币，最终成功 {added_coins} 枚"

def run_all_tasks_for_account(bili, config):
    tasks_to_run = [task.strip() for task in config.get('TASK_CONFIG', '').split(',') if task.strip()]
    if not tasks_to_run:
        # 直播签到已被B站官方下线，不再作为默认任务
        tasks_to_run = ['manga_sign', 'share_video', 'add_coin']

    user_info = bili.get_user_info()
    if not user_info:
        return {'登录检查': (False, 'Cookie失效或网络问题')}, None
        
    masked_uname = mask_string(user_info.get('uname'))
    logger.info(f"账号名称: {masked_uname}")
    tasks_result = {}
    
    video_list = bili.get_dynamic_videos()
    bvid_for_task = video_list[0] if video_list else 'BV1GJ411x7h7'

    if 'share_video' in tasks_to_run:
        tasks_result['分享视频'] = bili.share_video(bvid_for_task)
        task_interval()
    if 'live_sign' in tasks_to_run:
        tasks_result['直播签到'] = bili.live_sign()
        task_interval()
    if 'manga_sign' in tasks_to_run:
        tasks_result['漫画签到'] = bili.manga_sign()
        task_interval()
    if 'add_coin' in tasks_to_run:
        tasks_result['投币任务'] = execute_coin_task(bili, user_info, config)
        task_interval()

    tasks_result['观看视频'] = bili.watch_video(bvid_for_task)

    return tasks_result, user_info

def main():
    # ==================== 【核心修改点在这里】 ====================
    # 我们先用 .get() 获取值，如果值是空字符串，就用 or 操作符来选择默认值。
    # 这样既能处理"变量不存在"的情况，也能处理"变量存在但为空"的情况。
    config = {
        "BILIBILI_COOKIE": os.environ.get('BILIBILI_COOKIE'),
        "PUSH_PLUS_TOKEN": os.environ.get('PUSH_PLUS_TOKEN'),
        "TASK_CONFIG": os.environ.get('TASK_CONFIG') or 'manga_sign,share_video,add_coin',
        "COIN_ADD_NUM": os.environ.get('COIN_ADD_NUM') or '1',
        "COIN_SELECT_LIKE": os.environ.get('COIN_SELECT_LIKE') or '1',
        "COIN_VIDEO_SOURCE": os.environ.get('COIN_VIDEO_SOURCE') or 'dynamic'
    }
    # ============================================================
    
    if not config["BILIBILI_COOKIE"]:
        logger.error('环境变量 BILIBILI_COOKIE 未设置，程序终止')
        sys.exit(1)

    cookies = [c.strip() for c in config["BILIBILI_COOKIE"].split('###') if c.strip()]
    logger.info(f"检测到 {len(cookies)} 个账号，开始执行任务...")
    
    all_results = []
    any_failed = False  # 全局失败标志
    for i, cookie in enumerate(cookies, 1):
        masked_account_name = None
        logger.info(f"=== 账号{i} 任务完成情况 ===")
        bili = BilibiliTask(cookie)
        tasks_result, user_info = run_all_tasks_for_account(bili, config)
        final_user_info = bili.get_user_info() if user_info else None
        all_results.append({'account_index': i, 'tasks': tasks_result, 'user_info': final_user_info})
        # 多账号间额外间隔，降低风控风险
        if i < len(cookies):
            time.sleep(random.uniform(2, 4))

        # 账号任务成功标志
        account_failed = False
        valid_task_count = 0
        valid_success_count = 0

        # 任务日志输出
        for task_name, (success, msg) in tasks_result.items():
            if "push" in task_name or "推送" in task_name:
                continue
            level = logger.info if success else logger.error
            masked_account_name = mask_string(final_user_info.get('uname')) if final_user_info else f'账号{i}'
            if msg and any(k in msg for k in IGNORE_FAIL_KEYWORDS):
                # 跳过类消息(活动下线/已签到/资源不足等)统一用WARNING级别，不代表任务出错
                logger.warning(f"[账号{i}] {task_name}: 跳过，原因: {msg}")
                continue  # 跳过统计
            # 统计有效任务
            valid_task_count += 1
            if success:
                valid_success_count += 1
                level(f"[账号{i}] {task_name}: 成功")
            else:
                level(f"[账号{i}] {task_name}: 失败，原因: {msg}")

        # 判断账号是否失败
        if not user_info or valid_task_count == 0 or valid_success_count == 0:
            account_failed = True

        # 用户信息分段输出
        logger.info(f"=== 账号{i} 用户信息 ===")
        if final_user_info:
            logger.info(f"用户名: {mask_string(final_user_info.get('uname'))}")
            logger.info(f"UID: {mask_uid(final_user_info.get('mid'))}")
            logger.info(f"等级: {final_user_info.get('level_info', {}).get('current_level')}")
            logger.info(f"经验: {final_user_info.get('level_info', {}).get('current_exp')}")
            logger.info(f"硬币: {final_user_info.get('money')}")
        else:
            logger.error("用户信息获取失败")
        logger.info(f"--- 账号 {masked_account_name} 任务执行完毕 ---")
        logger.info("-" * 40)

        if account_failed:
            any_failed = True

    if config["PUSH_PLUS_TOKEN"] and all_results:
        logger.info('准备发送推送通知...')
        title = "Bilibili 任务通知"
        content = format_push_message(all_results)
        send_to_pushplus(config["PUSH_PLUS_TOKEN"], title, content)
    else:
        logger.info('未配置 PUSH_PLUS_TOKEN，跳过推送。')

    # 所有账号执行完毕，统一输出最终执行结果
    if any_failed:
        logger.error("有账号任务执行失败，整个任务失败！")
        sys.exit(1)
    else:
        logger.info("所有账号任务全部成功！")
        sys.exit(0)

IGNORE_FAIL_KEYWORDS = ["未配置", "跳过", "已下线", "已签到", "已领取", "非大会员", "不足", "上限", "已满", "已兑换"]

if __name__ == '__main__':
    main()