# -*- coding: utf-8 -*-
# 长江雨课堂（changjiang.yuketang.cn）自动刷课脚本
# version 5 —— 基于对官方前端 player.newHeartlog() 的逆向修复
#
# 关键修复（相对 v4）：
#   1. 请求头 xtbz 必须是 "ykt"，用 "cloud" 会被服务端判定为“未购买该课程”（80019 / 80005 错误）
#   2. 课程树改用 GET /mooc-api/v1/lms/learn/course/chapter?cid=（原 pub_new_pro / pub_news 对 MOOC 无效）
#   3. leaf_info 必须传 leaf_id，传 courseware_id 会 404
#   4. 心跳载荷按官方播放器字段构造，按 media.duration 驱动直到 completed=1
#   5. 全部请求带 timeout，用 json.loads 解析而非正则，键访问全部安全取值
#
# 用法：
#   python main.py --csrftoken=XXX --sessionid=YYY --uv-id=2803 --course="全球化与世界空间"
#   或设置环境变量 YKT_CSRFTOKEN / YKT_SESSIONID / YKT_UV_ID / YKT_COURSE
#   或不传参数，交互式输入。

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from typing import Dict, List, Optional

import requests

try:
    import colorama

    colorama.init(autoreset=True)
except ImportError:  # colorama 是可选的
    pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('ykt')

HOST = 'https://changjiang.yuketang.cn'

# 视频叶子类型
LEAF_TYPE_VIDEO = 0

# 每个心跳包覆盖的播放秒数（官方播放器约 1s 上报一次；批量补课时逐条 +20s）
STEP_SECONDS = 20
# 单个心跳包内的最大条数（官方前端 localStorage 上限为 50）
BATCH_SIZE = 50


class YuKeTang:
    """长江雨课堂 MOOC 自动刷课客户端。"""

    def __init__(self, csrftoken: str, sessionid: str, uv_id: str):
        self.csrftoken = csrftoken.strip()
        self.sessionid = sessionid.strip()
        self.uv_id = uv_id.strip()

        self.session = requests.Session()
        self.session.headers.update(self._base_headers())

        # 登录用户 id（/v2/api/web/userinfo），读进度时使用
        self.login_user_id: Optional[int] = None
        # 当前课程上下文
        self.cid: Optional[str] = None
        self.course_id: Optional[str] = None
        self.sku_id: Optional[str] = None
        self._courseware_map: Dict[str, str] = {}
        # leaf_id -> 时长；leaf_id -> 同章 leaf_id 列表（用于时长缺失时的估算）
        self._durations: Dict[str, float] = {}
        self._chapter_members: Dict[str, List[str]] = {}

    # ------------------------------------------------------------------ 基础

    def _base_headers(self) -> Dict[str, str]:
        return {
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                           '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'),
            'Content-Type': 'application/json',
            'Accept': 'application/json, text/plain, */*',
            'Cookie': (f'csrftoken={self.csrftoken}; sessionid={self.sessionid}; '
                       f'university_id={self.uv_id}; platform_id=3'),
            'referer': f'{HOST}/v2/web/index',
            'x-csrftoken': self.csrftoken,
            'xt-agent': 'web',
            # !!! 必须是 ykt。用 cloud 会返回 80019“请购买该课程之后再学习”
            'xtbz': 'ykt',
            'university-id': self.uv_id,
            'uv-id': self.uv_id,
        }

    def set_course_context(self, cid: str) -> None:
        """切到某门课，补上该课程相关的请求头。"""
        self.cid = str(cid)
        self.session.headers.update({
            'classroom-id': str(cid),
            'referer': f'{HOST}/v2/web/studentLog/{cid}',
        })

    def request(self, method: str, url: str, *, retries: int = 3, timeout: int = 25,
                **kwargs):
        """带超时与重试的请求；失败返回 None。"""
        last_err = None
        for attempt in range(1, retries + 1):
            try:
                resp = self.session.request(method, url, timeout=timeout, **kwargs)
            except requests.RequestException as exc:
                last_err = exc
                log.warning('请求异常(%d/%d)：%s', attempt, retries, exc)
                time.sleep(1.5 * attempt)
                continue

            if resp.status_code >= 500:
                last_err = f'HTTP {resp.status_code}'
                log.warning('服务端错误(%d/%d)：HTTP %s', attempt, retries, resp.status_code)
                time.sleep(2.0 * attempt)
                continue
            return resp

        log.error('请求最终失败：%s（%s）', url, last_err)
        return None

    def get_json(self, url: str, **kwargs) -> Optional[dict]:
        resp = self.request('GET', url, **kwargs)
        if resp is None:
            return None
        try:
            return resp.json()
        except ValueError:
            log.error('返回不是 JSON：%s -> %s', url, resp.text[:200])
            return None

    # ------------------------------------------------------- 登录态 / 课程列表

    def fetch_login_user(self) -> bool:
        data = self.get_json(f'{HOST}/v2/api/web/userinfo')
        if not data or data.get('errmsg') != 'Success':
            log.error('认证失败：csrftoken 或 sessionid 无效（返回：%s）',
                      json.dumps(data, ensure_ascii=False)[:300] if data else 'None')
            return False
        info = (data.get('data') or [{}])[0]
        self.login_user_id = info.get('user_id')
        log.info('登录成功：%s（user_id=%s）', info.get('name'), self.login_user_id)
        return True

    def list_courses(self) -> List[dict]:
        data = self.get_json(f'{HOST}/v2/api/web/courses/list?identity=2')
        if not data or data.get('errmsg') != 'Success':
            log.error('获取课程列表失败：%s',
                      json.dumps(data, ensure_ascii=False)[:300] if data else 'None')
            return []
        out = []
        for item in (data.get('data') or {}).get('list') or []:
            course = item.get('course') or {}
            out.append({
                'classroom_id': item.get('classroom_id'),
                'name': course.get('name') or item.get('name'),
                'full_name': item.get('name'),
                'term': item.get('term'),
                'role': item.get('role'),
            })
        return out

    # ------------------------------------------------------------- 课程结构

    def fetch_chapters(self) -> List[dict]:
        """课程树。返回 course_chapter 列表。"""
        data = self.get_json(f'{HOST}/mooc-api/v1/lms/learn/course/chapter',
                             params={'cid': self.cid})
        if not data or not data.get('data'):
            err = json.dumps(data, ensure_ascii=False)[:300] if data else 'None'
            log.error('获取课程结构失败：%s', err)
            if data and data.get('error_code') in (80019, 80005):
                log.error('服务端认为本账号未购买/未加入该课程。请确认：'
                          '(1) 已在雨课堂网页端加入该 MOOC；(2) 请求头 xtbz=ykt。')
            return []
        return (data['data'] or {}).get('course_chapter') or []

    @staticmethod
    def iter_video_leaves(chapters: List[dict]):
        """遍历课程树，产出所有视频叶子。"""
        for chapter in chapters:
            for section in chapter.get('section_leaf_list') or []:
                for leaf in section.get('leaf_list') or []:
                    if leaf.get('leaf_type') != LEAF_TYPE_VIDEO:
                        continue
                    yield {
                        'leaf_id': leaf.get('id'),
                        'chapter': chapter.get('name'),
                        'section': section.get('name'),
                        'is_locked': bool(leaf.get('is_locked')),
                        'name': leaf.get('name'),
                    }

    def build_chapter_members(self, chapters: List[dict]) -> None:
        """记录每个视频属于哪一章的哪些兄弟视频（用于时长缺失时的估算）。"""
        members: Dict[str, List[str]] = {}
        for chapter in chapters:
            ids = []
            for section in chapter.get('section_leaf_list') or []:
                for leaf in section.get('leaf_list') or []:
                    if leaf.get('leaf_type') == LEAF_TYPE_VIDEO and leaf.get('id'):
                        ids.append(str(leaf['id']))
            for leaf_id in ids:
                members[leaf_id] = ids
        self._chapter_members = members

    def fetch_leaf_info(self, leaf_id) -> Optional[dict]:
        """视频详情：sku_id / user_id / course_id / 时长。"""
        data = self.get_json(f'{HOST}/mooc-api/v1/lms/learn/leaf_info/{self.cid}/{leaf_id}/')
        if not data or not data.get('success'):
            log.warning('leaf_info 失败 leaf_id=%s：%s', leaf_id,
                        json.dumps(data, ensure_ascii=False)[:200] if data else 'None')
            return None
        return data.get('data') or {}

    # ----------------------------------------------------------------- 进度

    def fetch_progress(self, video_id) -> dict:
        """读取单个视频的进度：{rate, completed, watch_length, video_length, last_point}。"""
        data = self.get_json(f'{HOST}/video-log/get_video_watch_progress/', params={
            'cid': self.course_id,
            'user_id': self.login_user_id,
            'classroom_id': self.cid,
            'video_type': 'video',
            'vtype': 'rate',
            'video_id': video_id,
            'snapshot': 1,
            'term': 'latest',
            'uv_id': self.uv_id,
        })
        if not data:
            return {}
        return (data.get('data') or {}).get(str(video_id)) or {}

    def fetch_all_progress(self) -> Dict[str, dict]:
        """一次性取回整门课所有视频的进度（比逐个查询快得多）。"""
        data = self.get_json(f'{HOST}/video-log/get_video_watched_record/', params={
            'cid': self.course_id,
            'user_id': self.login_user_id,
            'classroom_id': self.cid,
            'video_type': 'video',
            'term': 'latest',
            'uv_id': self.uv_id,
        })
        if not data:
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)}

    @staticmethod
    def _is_completed(progress: dict) -> bool:
        """服务端判定完成即算完成（completed=1）。

        注意：个别视频元数据 duration=0，本地上报时长会短于服务端实际时长，
        此时 rate<1 但 completed 仍会置 1，所以这里以 completed 为准。
        """
        return bool(progress) and str(progress.get('completed')) == '1'

    @staticmethod
    def _rate(progress: dict) -> float:
        try:
            return float(progress.get('rate') or 0)
        except (TypeError, ValueError):
            return 0.0

    # --------------------------------------------------------------- 心跳上报

    def _heartbeat_entry(self, *, et: str, cp: float, sq: int, ts_ms: int, pg: str,
                         leaf_id, courseware_id, api_user_id: int, duration: float) -> dict:
        """构造一条心跳记录，字段与官方播放器 player.newHeartlog() 一致。"""
        return {
            'i': 5,
            'et': et,
            'p': 'web',
            'n': 'ws',
            'lob': 'cloud4',
            'cp': round(float(cp), 1),
            'fp': 0,
            'tp': 0,
            'sp': 1,
            'ts': str(ts_ms),
            'u': api_user_id,
            'uip': '',
            'c': str(self.course_id),
            'v': leaf_id,
            'skuid': str(self.sku_id),
            'classroomid': str(self.cid),
            'cc': str(courseware_id),
            'd': float(duration),
            'pg': pg,
            'sq': sq,
            't': 'video',
            'cards_id': 0,
            'slide': 0,
            'v_url': '',
        }

    def send_heartbeat(self, entries: List[dict]) -> bool:
        resp = self.request('POST', f'{HOST}/video-log/heartbeat/',
                            data=json.dumps({'heart_data': entries}), timeout=30)
        return resp is not None and resp.status_code == 200

    # ------------------------------------------------------------ 单个视频

    def watch_video(self, leaf: dict, poll_timeout: float = 120.0) -> str:
        """把单个视频刷到 completed=1。返回 'done' | 'skipped' | 'failed'。"""
        leaf_id = leaf['leaf_id']
        title = leaf.get('section') or leaf.get('name') or '视频'

        info = self.fetch_leaf_info(leaf_id)
        if not info:
            return 'failed'

        # 该课程记录所属的 user_id（心跳里用这个）与 sku_id / course_id
        api_user_id = info.get('user_id')
        self.sku_id = str(info.get('sku_id') or '')
        self.course_id = str(info.get('course_id') or '')
        media = ((info.get('content_info') or {}).get('media') or {})
        duration = self.resolve_duration(leaf_id, media)

        if not api_user_id or not self.course_id or not self.sku_id:
            log.warning('《%s》缺少必要参数，跳过（leaf_id=%s）', title, leaf_id)
            return 'failed'
        if duration <= 0:
            log.warning('《%s》拿不到视频时长，跳过（leaf_id=%s）', title, leaf_id)
            return 'failed'

        # 课件 id：取活动日志里的 courseware_id，缺省回落到 leaf_id
        courseware_id = self._courseware_map.get(str(leaf_id)) or leaf_id

        # 已完成的直接跳过
        progress = self.fetch_progress(leaf_id)
        if self._is_completed(progress):
            log.info('《%s》已学完（%.1f%%），跳过', title, self._rate(progress) * 100)
            return 'skipped'

        start_rate = self._rate(progress)
        log.info('《%s》开始学习（时长 %.0fs，当前 %.1f%%，leaf_id=%s）',
                 title, duration, start_rate * 100, leaf_id)

        pg = f'{leaf_id}_{self._rand_token()}'
        sq = 1
        ts_base = int(time.time() * 1000)
        cp = 0.0
        # 目标时长：先用元数据时长，若服务端认为更长（duration=0 的视频），
        # 后续用服务端返回的 video_length 追加补齐。
        target = duration
        batches = max(1, int(target / (BATCH_SIZE * STEP_SECONDS)) + 1)
        timeout = max(poll_timeout, batches * 12 + 30)
        deadline = time.time() + timeout

        while time.time() < deadline:
            # 本批从当前进度推进到目标末尾
            batch = []
            while cp < target and len(batch) < BATCH_SIZE:
                cp = min(cp + STEP_SECONDS, target)
                batch.append(self._heartbeat_entry(
                    et='videoend' if cp >= target else 'loadeddata',
                    cp=cp, sq=sq, ts_ms=ts_base + sq * 300, pg=pg,
                    leaf_id=leaf_id, courseware_id=courseware_id,
                    api_user_id=api_user_id, duration=target,
                ))
                sq += 1

            if batch and not self.send_heartbeat(batch):
                log.warning('《%s》心跳上报失败，重试一次', title)
                time.sleep(2.0)
                if not self.send_heartbeat(batch):
                    return 'failed'

            # 服务端写入有延迟：轮询直到判定完成，或确认进度确实前进了
            current = progress
            advanced = False
            for _ in range(6):
                time.sleep(1.2)
                current = self.fetch_progress(leaf_id)
                if self._is_completed(current):
                    log.info('《%s》学习完成 ✅（%.1f%%，已看 %.0fs/%.0fs）', title,
                             self._rate(current) * 100,
                             float(current.get('watch_length') or 0), duration)
                    return 'done'
                if self._rate(current) > self._rate(progress):
                    advanced = True
                    break

            progress = current
            rate = self._rate(progress)
            log.info('《%s》进度 %.1f%%（已看 %.0fs/%.0fs，last_point=%s）',
                     title, rate * 100, float(progress.get('watch_length') or 0), duration,
                     progress.get('last_point'))

            if not advanced:
                # 服务端没接受这一批，别空转
                break

            # 已到目标末尾且服务端认为视频更长（个别视频 duration=0），按服务端长度补齐
            try:
                server_len = float(progress.get('video_length') or 0)
            except (TypeError, ValueError):
                server_len = 0.0
            if cp >= target and server_len > target:
                log.info('《%s》服务端时长 %.0fs 大于元数据 %.0fs，补齐剩余部分',
                         title, server_len, target)
                target = server_len
                continue

            if cp >= target:
                # 整段已覆盖且进度在前进，等它自然判定完成
                break

        progress = self.fetch_progress(leaf_id)
        if self._is_completed(progress):
            log.info('《%s》学习完成 ✅（%.1f%%）', title, self._rate(progress) * 100)
            return 'done'
        log.error('《%s》超时未完成（%.1f%%）', title, self._rate(progress) * 100)
        return 'failed'

    # -------------------------------------------------------------- 工具方法

    @staticmethod
    def _rand_token(length: int = 5) -> str:
        alphabet = 'abcdefghijklmnopqrstuvwxyz0123456789'
        return ''.join(random.choice(alphabet) for _ in range(length))

    def resolve_duration(self, leaf_id, media: dict) -> float:
        """取视频时长。个别视频服务端 duration=0（元数据缺失），用同章其它视频估算。"""
        try:
            duration = float(media.get('duration') or 0)
        except (TypeError, ValueError):
            duration = 0.0
        if duration > 0:
            self._durations[str(leaf_id)] = duration
            return duration

        leaf_key = str(leaf_id)
        log.warning('《leaf_id=%s》服务端未提供时长（duration=0），尝试用同章视频估算', leaf_id)

        # 同章其它视频已知的时长
        candidates = [self._durations[lid] for lid in self._chapter_members.get(leaf_key, [])
                      if lid in self._durations and self._durations[lid] > 0]
        if not candidates:
            # 还没刷到同章的视频，就地查它们的元数据
            for lid in self._chapter_members.get(leaf_key, []):
                if lid == leaf_key or lid in self._durations:
                    continue
                info = self.fetch_leaf_info(lid)
                if not info:
                    continue
                d = ((info.get('content_info') or {}).get('media') or {}).get('duration') or 0
                try:
                    d = float(d)
                except (TypeError, ValueError):
                    d = 0.0
                if d > 0:
                    self._durations[lid] = d
                    candidates.append(d)

        if not candidates:
            candidates = [d for d in self._durations.values() if d > 0]

        if candidates:
            est = max(candidates)
            log.warning('《leaf_id=%s》按参照视频估算时长为 %.0fs', leaf_id, est)
            self._durations[leaf_key] = est
            return est

        log.warning('《leaf_id=%s》无法确定时长', leaf_id)
        return 0.0

    def load_courseware_map(self) -> Dict[str, str]:
        """从活动日志取 leaf_id -> courseware_id 映射（心跳 cc 字段用）。"""
        mapping: Dict[str, str] = {}
        try:
            data = self.get_json(
                f'{HOST}/v2/api/web/logs/learn/{self.cid}',
                params={'actype': -1, 'page': 0, 'offset': 100, 'sort': -1})
            for act in ((data or {}).get('data') or {}).get('activities') or []:
                if act.get('type') != 17:
                    continue
                leaf_id = (act.get('content') or {}).get('leaf_id')
                courseware_id = act.get('courseware_id')
                if leaf_id and courseware_id:
                    mapping[str(leaf_id)] = str(courseware_id)
            log.info('活动日志：映射 %d 个视频课件 ID', len(mapping))
        except Exception as exc:  # 映射失败不影响主流程
            log.warning('获取课件映射失败（cc 将回落到 leaf_id）：%s', exc)
        self._courseware_map = mapping
        return mapping

    # ----------------------------------------------------------------- 主流程

    def run(self, course_keyword: Optional[str] = None, course_index: Optional[int] = None) -> int:
        if not self.fetch_login_user():
            return 1

        courses = self.list_courses()
        if not courses:
            log.error('课程列表为空')
            return 1

        target = self._pick_course(courses, course_keyword, course_index)
        if target is None:
            return 1

        self.set_course_context(target['classroom_id'])
        log.info('已选择课程：《%s》（classroom_id=%s）', target['name'], target['classroom_id'])

        chapters = self.fetch_chapters()
        if not chapters:
            return 1

        leaves = list(self.iter_video_leaves(chapters))
        log.info('课程结构：%d 章，%d 个视频', len(chapters), len(leaves))
        if not leaves:
            log.warning('该课程没有可刷的视频')
            return 0

        self.build_chapter_members(chapters)
        self.load_courseware_map()

        stats = {'done': 0, 'skipped': 0, 'failed': 0}
        for idx, leaf in enumerate(leaves, 1):
            if leaf.get('is_locked'):
                log.warning('[%d/%d]《%s》未解锁，跳过', idx, len(leaves), leaf.get('section'))
                stats['failed'] += 1
                continue
            log.info('---- [%d/%d] %s / %s ----', idx, len(leaves),
                     leaf.get('chapter'), leaf.get('section'))
            try:
                result = self.watch_video(leaf)
            except Exception:
                log.exception('《%s》处理异常', leaf.get('section'))
                result = 'failed'
            stats[result] = stats.get(result, 0) + 1
            time.sleep(random.uniform(0.8, 1.8))

        log.info('全部完成：成功 %d，已跳过 %d，失败 %d',
                 stats['done'], stats['skipped'], stats['failed'])
        return 0 if stats['failed'] == 0 else 2

    def _pick_course(self, courses: List[dict], keyword: Optional[str],
                     index: Optional[int]) -> Optional[dict]:
        if index is not None:
            if 1 <= index <= len(courses):
                return courses[index - 1]
            log.error('课程编号 %s 超出范围（1-%d）', index, len(courses))
            return None

        if keyword:
            pattern = re.compile(re.escape(keyword), re.IGNORECASE)
            matches = [c for c in courses
                       if pattern.search(c.get('name') or '')
                       or pattern.search(c.get('full_name') or '')]
            if len(matches) == 1:
                return matches[0]
            if not matches:
                log.error('没有找到匹配「%s」的课程', keyword)
            else:
                log.error('「%s」匹配到多门课程：%s', keyword,
                          '、'.join(m['name'] or '' for m in matches))
            return None

        for i, c in enumerate(courses, 1):
            print(f'编号：{i:<3} 课程：{c["name"]}（{c["full_name"]}）')
        try:
            number = int(input('\n请输入要刷的课程编号：').strip())
        except (ValueError, EOFError):
            log.error('输入无效')
            return None
        return self._pick_course(courses, None, number)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='长江雨课堂自动刷课脚本')
    parser.add_argument('--csrftoken', default=os.environ.get('YKT_CSRFTOKEN'),
                        help='浏览器 Cookie 中的 csrftoken（或环境变量 YKT_CSRFTOKEN）')
    parser.add_argument('--sessionid', default=os.environ.get('YKT_SESSIONID'),
                        help='浏览器 Cookie 中的 sessionid（或环境变量 YKT_SESSIONID）')
    parser.add_argument('--uv-id', dest='uv_id', default=os.environ.get('YKT_UV_ID'),
                        help='university_id，长江大学为 2803（或环境变量 YKT_UV_ID）')
    parser.add_argument('--course', default=os.environ.get('YKT_COURSE'),
                        help='课程名关键词，可只写一部分（或环境变量 YKT_COURSE）')
    parser.add_argument('--index', type=int, default=None, help='按列表编号直接选课')
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    csrftoken = args.csrftoken or input('请输入 csrftoken: ').strip()
    sessionid = args.sessionid or input('请输入 sessionid: ').strip()
    uv_id = args.uv_id or (input('请输入 uv_id（长江大学填 2803）: ').strip() or '2803')

    if not csrftoken or not sessionid:
        log.error('csrftoken / sessionid 不能为空')
        return 1

    client = YuKeTang(csrftoken, sessionid, uv_id)
    return client.run(course_keyword=args.course, course_index=args.index)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\n已中断')
        sys.exit(130)
    except Exception:
        import traceback

        traceback.print_exc()
        try:
            input('按回车退出...')
        except EOFError:
            pass
        sys.exit(1)
