import sys
from typing import Any, List, Dict, Optional
import asyncio
import json
import os
import re
import base64
import tempfile
import sqlite3
import time as _time


def _load_dotenv():
    """读取同目录 .env（KEY=VALUE，# 注释），不覆盖已有环境变量。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_dotenv()
from contextlib import asynccontextmanager as _asynccontextmanager
import pandas as pd
from datetime import datetime
from urllib.parse import quote
from playwright.async_api import async_playwright
from fastmcp import FastMCP
import requests

# Gemini 图片分析（超过4张时使用）
def _load_gemini_key() -> str:
    """从 ~/.env 读取 GEMINI_API_KEY"""
    env_path = os.path.expanduser("~/.env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("GEMINI_API_KEY="):
                    return line.split("=", 1)[1].strip()
    return os.environ.get("GEMINI_API_KEY", "")

def _analyze_images_with_gemini(image_paths: list, note_title: str = "") -> str:
    """用 Gemini 3.6 Flash 分析图片列表，返回文字描述。"""
    try:
        from google import genai
        from google.genai import types as gtypes
    except ImportError:
        return "（Gemini 未安装，请 pip install google-genai）"

    api_key = _load_gemini_key()
    if not api_key:
        return "（未配置 GEMINI_API_KEY）"

    client = genai.Client(api_key=api_key)

    # 构建多图片请求
    parts = []
    for path in image_paths:
        try:
            with open(path, "rb") as f:
                data = f.read()
            # 根据后缀判断 mime_type
            ext = os.path.splitext(path)[1].lower()
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "png": "image/png", "webp": "image/webp"}.get(ext.lstrip("."), "image/jpeg")
            parts.append(gtypes.Part.from_bytes(data=data, mime_type=mime))
        except Exception as e:
            parts.append(gtypes.Part.from_text(text=f"[图片 {path} 读取失败: {e}]"))

    prompt = f"这是小红书帖子「{note_title}」的配图（共{len(image_paths)}张）。请依次描述每张图片的主要内容，包括画面场景、文字信息、重要细节，用简洁的中文。"
    parts.append(gtypes.Part.from_text(text=prompt))

    try:
        resp = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=[gtypes.Content(parts=parts, role="user")]
        )
        return resp.text
    except Exception as e:
        return f"（Gemini 分析失败：{e}）"

# ── 反风控辅助函数 ────────────────────────────────────────────────────
import random as _random


class _RiskBlocked(Exception):
    """触发风控熔断时抛出，工具层会把消息原样返回。"""


_RISK_URL_KEYS = ('website-login/captcha', '/captcha', 'verifytype=', 'web-login/captcha', '/website-login/error')
_RISK_TITLE_KEYS = ('安全限制', '安全验证', '验证码', '访问频繁', '账号异常')
_RISK_TEXT_KEYS = ('操作频繁', '访问频繁', '安全验证', '账号存在异常', '账号异常', '请完成验证', '拖动滑块')


def _rl_db():
    conn = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "xhs_actions.db"), timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS rate_events (action_type TEXT NOT NULL, ts REAL NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rate_events ON rate_events(action_type, ts)")
    conn.execute("""CREATE TABLE IF NOT EXISTS risk_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        until_ts REAL, reason TEXT, tripped_at REAL, trip_count INTEGER DEFAULT 0)""")
    return conn


class _RateLimit:
    """持久化速率限制 + 风控熔断。记录存在 xhs_actions.db，服务重启不清零。
    max_per_hour 为 None 表示只限间隔、不限次数。"""
    _limits = {
        'comment':     {'min_interval': 120, 'max_per_hour': None},
        'reply':       {'min_interval': 120, 'max_per_hour': None},
        'like':        {'min_interval': 30,  'max_per_hour': 20},
        'search':      {'min_interval': 10,  'max_per_hour': 30},
        'image_fetch': {'min_interval': 8,   'max_per_hour': 60},
        'follow':      {'min_interval': 60,  'max_per_hour': 10},
    }

    # ── 熔断 ──
    @classmethod
    def breaker_message(cls):
        """熔断中返回提示字符串，否则 None。"""
        try:
            with _rl_db() as conn:
                row = conn.execute("SELECT until_ts, reason FROM risk_state WHERE id=1").fetchone()
        except Exception as e:
            print(f"[risk] 读熔断状态失败: {e}", file=sys.stderr, flush=True)
            return None
        if row and row[0] and row[0] > _time.time():
            until = datetime.fromtimestamp(row[0]).strftime('%m-%d %H:%M')
            return f"🛑 小红书风控熔断中，暂停所有操作到 {until}。触发原因：{row[1]}。如已手动在浏览器里过了验证，可用 risk_status(clear=True) 解除。"
        return None

    @classmethod
    def trip(cls, reason: str):
        """触发熔断：24h 内第一次停 6 小时，24h 内再次触发停 24 小时。"""
        now = _time.time()
        try:
            with _rl_db() as conn:
                row = conn.execute("SELECT until_ts, tripped_at, trip_count FROM risk_state WHERE id=1").fetchone()
                if row and row[0] and row[0] > now:
                    return  # 已在熔断中，不重复推送
                repeat = bool(row and row[1] and now - row[1] < 24 * 3600)
                hours = 24 if repeat else 6
                count = (row[2] or 0) + 1 if row else 1
                conn.execute("INSERT OR REPLACE INTO risk_state (id, until_ts, reason, tripped_at, trip_count) VALUES (1, ?, ?, ?, ?)",
                             (now + hours * 3600, reason, now, count))
        except Exception as e:
            print(f"[risk] 写熔断状态失败: {e}", file=sys.stderr, flush=True)
            hours = 6
        print(f"[risk] 🛑 熔断 {hours}h：{reason}", file=sys.stderr, flush=True)

    @classmethod
    def clear(cls):
        with _rl_db() as conn:
            conn.execute("UPDATE risk_state SET until_ts = NULL WHERE id=1")

    # ── 限速 ──
    @classmethod
    def check(cls, action_type: str):
        """返回 None 表示允许，返回字符串表示拒绝原因（应直接 return 给调用方）。"""
        b = cls.breaker_message()
        if b:
            return b
        cfg = cls._limits.get(action_type)
        if not cfg:
            return None
        now = _time.time()
        try:
            with _rl_db() as conn:
                conn.execute("DELETE FROM rate_events WHERE ts < ?", (now - 7 * 86400,))
                history = [r[0] for r in conn.execute(
                    "SELECT ts FROM rate_events WHERE action_type=? AND ts > ? ORDER BY ts",
                    (action_type, now - 3600))]
        except Exception as e:
            print(f"[ratelimit] 读记录失败: {e}", file=sys.stderr, flush=True)
            return None
        if history and (now - history[-1]) < cfg['min_interval']:
            wait = int(cfg['min_interval'] - (now - history[-1]))
            return f"⏳ 操作过于频繁，请等待约 {wait} 秒后再操作。（防风控：{action_type} 最短间隔 {cfg['min_interval']}s）"
        if cfg['max_per_hour'] and len(history) >= cfg['max_per_hour']:
            wait = int(3600 - (now - history[0]))
            return f"⏳ 本小时 {action_type} 次数已达上限（{cfg['max_per_hour']} 次），请等待约 {wait} 秒后再试。"
        return None

    @classmethod
    def record(cls, action_type: str):
        try:
            with _rl_db() as conn:
                conn.execute("INSERT INTO rate_events (action_type, ts) VALUES (?, ?)", (action_type, _time.time()))
        except Exception as e:
            print(f"[ratelimit] 写记录失败: {e}", file=sys.stderr, flush=True)


def _url_is_risky(url: str) -> bool:
    u = (url or '').lower()
    return any(k in u for k in _RISK_URL_KEYS)


async def _detect_risk(page):
    """检查页面是否落入风控（验证码页 / 安全限制 / 频繁提示 toast）。命中返回原因字符串。
    正文关键词只在短页面或 toast/弹窗里查，避免笔记内容本身提到"验证"之类误判。"""
    try:
        if page.is_closed():
            return None
        if _url_is_risky(page.url):
            return f"跳转到验证页 {page.url[:120]}"
        title = await page.title()
        for k in _RISK_TITLE_KEYS:
            if k in title:
                return f"页面标题「{title}」"
        hit = await page.evaluate("""(keys) => {
            const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
            const cap = [...document.querySelectorAll('[class*="captcha" i], [id*="captcha" i], .red-captcha')].find(vis);
            if (cap) return '页面出现验证码组件';
            const pools = [...document.querySelectorAll('[class*="toast" i], [class*="message" i][role], [role="alert"]')]
                .filter(vis).map(e => (e.innerText || '').slice(0, 200));
            const body = (document.body && document.body.innerText) || '';
            if (body.length < 600) pools.push(body);
            for (const t of pools) for (const k of keys) if (t.includes(k)) return '提示：' + t.trim().slice(0, 60);
            return null;
        }""", list(_RISK_TEXT_KEYS))
        return hit
    except Exception:
        return None


def _arm_page(page):
    """给 page 装上风控守卫：goto 前查熔断，goto 后查风控；任何主框架跳到验证页也会触发熔断。幂等。"""
    if page is None or getattr(page, '_risk_armed', False):
        return
    _orig_goto = page.goto

    async def _guarded_goto(url, *args, **kwargs):
        b = _RateLimit.breaker_message()
        if b:
            raise _RiskBlocked(b)
        resp = await _orig_goto(url, *args, **kwargs)
        reason = await _detect_risk(page)
        if reason:
            _RateLimit.trip(reason)
            raise _RiskBlocked(f"🛑 检测到风控（{reason}），已熔断并暂停所有小红书操作。")
        return resp

    def _on_nav(frame):
        try:
            if frame == page.main_frame and _url_is_risky(frame.url):
                _RateLimit.trip(f"跳转到验证页 {frame.url[:120]}")
        except Exception:
            pass

    page.goto = _guarded_goto
    page.on("framenavigated", _on_nav)
    page._risk_armed = True


async def _skim(page):
    """打开笔记后先看一会儿：随机滚两三下再回来，避免"一打开就评论"。"""
    try:
        vp = page.viewport_size or {'width': 1280, 'height': 800}
        await page.mouse.move(vp['width'] * _random.uniform(0.35, 0.6), vp['height'] * _random.uniform(0.35, 0.6),
                              steps=_random.randint(6, 14))
        await _rand_sleep(_random.uniform(3, 6))
        for _ in range(_random.randint(2, 3)):
            await page.mouse.wheel(0, _random.randint(200, 550))
            await _rand_sleep(_random.uniform(1.5, 4))
        if _random.random() < 0.6:
            await page.mouse.wheel(0, -_random.randint(150, 400))
            await _rand_sleep(_random.uniform(1, 2.5))
    except Exception:
        pass


async def _human_click(page, element=None, x: float = None, y: float = None):
    """模拟人类鼠标轨迹后点击，降低自动化特征。元素不可见时降级为普通 click。"""
    try:
        if element is not None:
            box = await element.bounding_box()
            if box is None:
                await element.click()
                return
            cx = box['x'] + box['width'] * _random.uniform(0.3, 0.7)
            cy = box['y'] + box['height'] * _random.uniform(0.3, 0.7)
        elif x is not None and y is not None:
            cx, cy = float(x), float(y)
        else:
            return
        # 先移到附近偏移点，再缓慢移到目标，模拟自然轨迹
        await page.mouse.move(cx + _random.uniform(-40, 40), cy + _random.uniform(-20, 20))
        await asyncio.sleep(_random.uniform(0.05, 0.15))
        await page.mouse.move(cx, cy, steps=_random.randint(5, 12))
        await asyncio.sleep(_random.uniform(0.05, 0.12))
        await page.mouse.click(cx, cy)
    except Exception:
        if element is not None:
            try:
                await element.click()
            except Exception:
                pass


async def _rand_sleep(base: float, jitter: float = 0.4):
    """带随机抖动的等待，模拟人类操作节奏。
    实际等待时间 = base ± (base × jitter)
    """
    delta = base * jitter
    await asyncio.sleep(max(0.1, base + _random.uniform(-delta, delta)))

_CJK_RE = re.compile(r'[㐀-鿿豈-﫿]')


_NL_MARK = "\u2424"  # ␤：评论里的换行占位符。网页端发送时会吞掉真换行，所以先打占位符，发请求时再换回 \n


async def _ensure_nl_route(page):
    """给页面装一个只作用于 comment/post 请求的拦截：把请求体里的占位符换成真换行。
    网页端 JS 提交评论时会把输入框里的换行去掉，但服务器认 \\n（手机 App 就是这么发的）。
    每个页面只装一次；请求体里没有占位符时原样放行。"""
    if getattr(page, "_nl_route_on", False):
        return
    async def _h(route):
        try:
            body = route.request.post_data
            if body and _NL_MARK in body:
                await route.continue_(post_data=body.replace(_NL_MARK, "\\n"))
                return
        except Exception:
            pass
        await route.continue_()
    await page.route("**/api/sns/web/v1/comment/post*", _h)
    page._nl_route_on = True


async def _human_type(page, text: str, wpm: int = None, soft_newline: bool = False):
    """模拟真人输入中文。
    - 汉字按 1~4 字一组用 insert_text 上屏（像输入法选词提交），每组之间 0.5~1.5 秒，约 2~3 字/秒
    - 英文/数字/符号逐键敲，换行按 Enter
    - 偶尔停下来"想一想"
    - 超过 200 字的长文按段落上屏（像从草稿粘贴过来），避免一篇笔记敲十几分钟
    soft_newline=True 时换行打占位符 _NL_MARK（评论框里单按 Enter 会直接发送，网页端也会吞真换行；调用方需先 _ensure_nl_route）。
    wpm 参数保留只为兼容旧调用，已不使用。"""
    if len(text) > 200:
        lines = text.split('\n')
        for i, line in enumerate(lines):
            if line:
                await page.keyboard.insert_text(line)
                await asyncio.sleep(_random.uniform(0.8, 2.5))
            if i < len(lines) - 1:
                await (page.keyboard.insert_text(_NL_MARK) if soft_newline else page.keyboard.press('Enter'))
                await asyncio.sleep(_random.uniform(0.2, 0.6))
        return

    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '\n':
            await (page.keyboard.insert_text(_NL_MARK) if soft_newline else page.keyboard.press('Enter'))
            await asyncio.sleep(_random.uniform(0.5, 1.2))
            i += 1
        elif _CJK_RE.match(ch):
            j = i + 1
            target = _random.choice((1, 2, 2, 2, 3, 3, 4))
            while j < n and j - i < target and _CJK_RE.match(text[j]):
                j += 1
            await page.keyboard.insert_text(text[i:j])
            await asyncio.sleep(_random.uniform(0.5, 1.5))
            i = j
        elif ch in '。，！？、；：…“”‘’（）《》～—':
            await page.keyboard.insert_text(ch)
            await asyncio.sleep(_random.uniform(0.4, 1.0))
            i += 1
        else:
            await page.keyboard.type(ch)
            await asyncio.sleep(_random.uniform(0.08, 0.25) if ch not in '.,!?' else _random.uniform(0.3, 0.7))
            i += 1
        if _random.random() < 0.06:
            await asyncio.sleep(_random.uniform(1.5, 3.5))

async def _get_user_avatar_key(page, user_hex_id: str) -> str:
    """导航到用户主页，提取其头像 CDN key（用于 picker 精确匹配）。"""
    try:
        url = f"https://www.xiaohongshu.com/user/profile/{user_hex_id}"
        await page.goto(url, timeout=30000)
        await asyncio.sleep(2)
        key = await page.evaluate("""
            () => {
                const imgs = [...document.querySelectorAll('img')];
                for (const img of imgs) {
                    const m = img.src.match(/avatar\\/([^?]+)/);
                    if (m) return m[1];
                }
                const state = window.__INITIAL_STATE__;
                const u = state?.user?.userInfo || state?.userPageNote?.user;
                const av = u?.imageBigUrl || u?.image || u?.avatar || '';
                const m2 = av.match(/avatar\\/([^?]+)/);
                return m2 ? m2[1] : '';
            }
        """)
        return key or ''
    except Exception:
        return ''


async def _type_with_at_mention(page, text: str, avatar_map: dict | None = None):
    """输入评论文字；遇到行首或空格后的 @username 片段时触发小红书 @picker，
    选中用户后继续输入。若 picker 未出现则降级为纯文字。
    avatar_map: {nickname: cdn_key}，用于同名情况下的精确匹配。
    调用前确保输入框已获得焦点。
    """
    import re
    # 只拆分行首或空白后的 @xxx，避免误匹配"测试@xxx"中的 @
    parts = re.split(r'((?:(?<=\s)|(?<=^))@\S+)', text)
    # 若上面没拆到（text 本身以 @ 开头），用宽松匹配兜底
    if len(parts) == 1:
        parts = re.split(r'(@\S+)', text)

    for part in parts:
        if not part:
            continue
        m = re.fullmatch(r'@(\S+)', part)
        if m:
            search_term = m.group(1)
            await page.keyboard.type('@')
            await asyncio.sleep(1.5)
            for ch in search_term:
                await page.keyboard.type(ch)
                await asyncio.sleep(0.25)
            # 等待 picker 出现（最多 3 秒），然后一次性在 JS 里找到目标项坐标
            await asyncio.sleep(0.3)
            # 优先用 avatar_map 里预加载的 CDN key；没有则留空
            avatar_key_for_term = (avatar_map or {}).get(search_term, '')
            if not avatar_key_for_term:
                # 尝试从 __INITIAL_STATE__ 拿当前登录用户头像（@自己时有用）
                avatar_key_for_term = await page.evaluate("""
                    () => {
                        try {
                            const state = window.__INITIAL_STATE__;
                            const u = state?.user?.userinfo || state?.user?.user
                                    || state?.reader?.userInfo || state?.me;
                            const url = u?.imageBigUrl || u?.avatar || u?.image || u?.avatarUrl || '';
                            const m = url.match(/avatar\\/([^?]+)/);
                            return m ? m[1] : '';
                        } catch(e) { return ''; }
                    }
                """)
            my_avatar_key = avatar_key_for_term
            item_pos = None
            for _attempt in range(6):          # 最多轮询 3 秒
                item_pos = await page.evaluate("""
                    ([term, avatarKey]) => {
                        const container = document.querySelector('.mention-select-container');
                        if (!container) return null;

                        let items = [...container.querySelectorAll('li')];
                        if (!items.length) items = [...container.children];

                        let target = null;

                        // 优先：用头像 CDN key 精确匹配（避免同名误选）
                        if (avatarKey) {
                            target = items.find(item => {
                                const img = item.querySelector('img');
                                return img && img.src && img.src.includes(avatarKey);
                            }) || null;
                        }

                        // 次选：nickname 文本精确匹配（span.name）
                        if (!target) {
                            target = items.find(item => {
                                const nick = (item.querySelector('span.name') || item.querySelector('span') || item).textContent?.trim();
                                return nick === term;
                            }) || null;
                        }

                        // 包含匹配
                        if (!target) {
                            target = items.find(item => (item.textContent?.trim() || '').includes(term)) || null;
                        }

                        // 兜底：第一项
                        if (!target && items.length) target = items[0];
                        if (!target) return null;

                        const rect = target.getBoundingClientRect();
                        if (!rect.width || !rect.height) return null;
                        return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
                    }
                """, [search_term, my_avatar_key])
                if item_pos:
                    break
                await asyncio.sleep(0.5)

            if item_pos:
                await _human_click(page, x=item_pos['x'], y=item_pos['y'])
                await asyncio.sleep(0.8)
            else:
                await page.keyboard.press('ArrowDown')
                await asyncio.sleep(0.3)
                await page.keyboard.press('Enter')
                await asyncio.sleep(0.5)
        else:
            await _human_type(page, part, soft_newline=True)


async def _quick_comments(page, limit: int = 15) -> str:
    """从当前已加载的笔记页快速提取前 N 条评论，供 get_note_content 内联调用。"""
    try:
        # 先尝试从 __INITIAL_STATE__ 直接拿（最快）
        data = await page.evaluate("""(limit) => {
            const state = window.__INITIAL_STATE__;
            const list = state?.comment?.comments
                      || state?.commentModule?.commentList
                      || [];
            if (list.length > 0) {
                return list.slice(0, limit).map(c => ({
                    user: c.userInfo?.nickname || c.user?.nickname || '?',
                    content: c.content || '',
                    id: c.id || c.commentId || ''
                }));
            }
            return null;
        }""", limit)

        if data:
            lines = [f"{i+1}. {c['user']}: {c['content']}" for i, c in enumerate(data) if c['content']]
            return "\n".join(lines)

        # 降级：滚动一下再 DOM 抓
        for _ in range(3):
            await page.evaluate("window.scrollBy(0, 600)")
            await asyncio.sleep(0.7)

        items = await page.evaluate("""(limit) => {
            const results = [];
            const selectors = ['div.comment-item', 'div.commentItem', 'div.feed-comment'];
            for (const sel of selectors) {
                const els = [...document.querySelectorAll(sel)].slice(0, limit);
                if (els.length > 0) {
                    for (const el of els) {
                        const user = el.querySelector('span.user-name,a.name,span.nickname')?.textContent?.trim() || '?';
                        const content = el.querySelector('div.content,p.content,div.text')?.textContent?.trim()
                                     || el.textContent?.trim() || '';
                        if (content.length > 1) results.push(user + ': ' + content);
                    }
                    break;
                }
            }
            return results;
        }""", limit)

        if items:
            return "\n".join(f"{i+1}. {c}" for i, c in enumerate(items))
        return ""
    except Exception:
        return ""


# 初始化 FastMCP 服务器
mcp = FastMCP("xiaohongshu_scraper", lifespan=lambda server: _lifespan(server))

# 全局变量
_DIR = os.path.dirname(os.path.abspath(__file__))
BROWSER_DATA_DIR = os.path.join(_DIR, "browser_data")
DATA_DIR = os.path.join(_DIR, "data")
XHS_MCP_DB = os.path.expanduser("~/.xhs-mcp/data.db")
ACTIONS_DB = os.path.join(_DIR, "xhs_actions.db")
CDP_PORT  = 9222
CDP_URL   = f"http://localhost:{CDP_PORT}"

# 确保目录存在
os.makedirs(BROWSER_DATA_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)


def _init_actions_db():
    try:
        conn = sqlite3.connect(ACTIONS_DB)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS comment_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT NOT NULL,
                note_id TEXT NOT NULL,
                comment_id TEXT DEFAULT '',
                content TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comment_note ON comment_history(note_id, action_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comment_reply ON comment_history(comment_id, action_type)")
        conn.commit()
        conn.close()
    except Exception:
        pass

_init_actions_db()


def _record_comment(action_type: str, note_id: str, comment_id: str = '', content: str = ''):
    try:
        conn = sqlite3.connect(ACTIONS_DB)
        conn.execute(
            "INSERT INTO comment_history (action_type, note_id, comment_id, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (action_type, note_id, comment_id, content, datetime.now().isoformat(timespec='seconds'))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[comment_history] 写入失败 {action_type} note={note_id} cid={comment_id}: {e}", file=sys.stderr, flush=True)


def _get_comment_history(note_id: str = '', action_type: str = None, comment_id: str = None) -> list:
    try:
        conn = sqlite3.connect(ACTIONS_DB)
        conn.row_factory = sqlite3.Row
        query = "SELECT action_type, note_id, comment_id, content, created_at FROM comment_history WHERE 1=1"
        params = []
        if note_id:
            query += " AND note_id = ?"
            params.append(note_id)
        if action_type:
            query += " AND action_type = ?"
            params.append(action_type)
        if comment_id:
            query += " AND comment_id = ?"
            params.append(comment_id)
        query += " ORDER BY created_at DESC"
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


# 用于存储浏览器上下文，以便在不同方法之间共享
browser_instance = None   # playwright 实例
browser_obj = None        # chromium browser 对象
browser_context = None
main_page = None
notifications_page = None  # 通知标签页，常驻不关闭
is_logged_in = False
_user_token_cache: dict = {}  # {hex_id: xsec_token}，search_user 自动填充
class _PersistentTokenCache(dict):
    """{note_id: xsec_token}，内存 + xhs_actions.db 双份，服务重启不丢。"""
    def _db(self):
        conn = sqlite3.connect(ACTIONS_DB, timeout=10)
        conn.execute("CREATE TABLE IF NOT EXISTS note_tokens (note_id TEXT PRIMARY KEY, token TEXT, updated_at REAL)")
        return conn

    def _load(self, key):
        try:
            with self._db() as c:
                r = c.execute("SELECT token FROM note_tokens WHERE note_id=?", (key,)).fetchone()
            if r and r[0]:
                dict.__setitem__(self, key, r[0])
                return r[0]
        except Exception as e:
            print(f"[token_cache] 读失败: {e}", file=sys.stderr, flush=True)
        return None

    def __setitem__(self, key, value):
        dict.__setitem__(self, key, value)
        try:
            with self._db() as c:
                c.execute("INSERT OR REPLACE INTO note_tokens (note_id, token, updated_at) VALUES (?, ?, ?)",
                          (key, value, _time.time()))
        except Exception as e:
            print(f"[token_cache] 写失败: {e}", file=sys.stderr, flush=True)

    def __contains__(self, key):
        return dict.__contains__(self, key) or self._load(key) is not None

    def __getitem__(self, key):
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        v = self._load(key)
        if v is None:
            raise KeyError(key)
        return v

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


_note_token_cache = _PersistentTokenCache()  # 通知/搜索/feed/打开笔记时自动填充


def _remember_note_token(note_id: str, token: str):
    if note_id and token and dict.get(_note_token_cache, note_id) != token:
        _note_token_cache[note_id] = token


def _remember_token_from_url(url: str):
    """从 URL 里解析 note_id 和 xsec_token 并缓存。"""
    import re as _re
    m = _re.search(r'/(?:explore|discovery/item)/([0-9a-f]{24})', url or '')
    t = _re.search(r'xsec_token=([^&#]+)', url or '')
    if m and t:
        _remember_note_token(m.group(1), t.group(1))


def _add_cached_token(url: str) -> str:
    """explore 链接没带 xsec_token 时，用缓存补上（缓存里没有就原样返回）。"""
    import re as _re
    if 'xsec_token=' in url:
        return url
    m = _re.search(r'xiaohongshu\.com/(?:explore|discovery/item)/([0-9a-f]{24})', url)
    if m and m.group(1) in _note_token_cache:
        sep = '&' if '?' in url else '?'
        return f"{url}{sep}xsec_token={_note_token_cache[m.group(1)]}&xsec_source=pc_feed"
    return url


_EXPAND_REPLY_RE = "text=/展开\\s*\\d*\\s*条回复|展开更多回复|查看更多回复/"


async def _expand_replies(page, rounds: int = 6) -> int:
    """点开所有折叠的楼中楼（"展开 N 条回复"）。返回点击次数。"""
    clicked = 0
    for _ in range(rounds):
        btns = page.locator(_EXPAND_REPLY_RE)
        try:
            n = await btns.count()
        except Exception:
            break
        if n == 0:
            break
        progressed = False
        for i in range(n):
            try:
                b = btns.nth(i)
                await b.scroll_into_view_if_needed(timeout=2000)
                await b.click(timeout=2000)
                clicked += 1
                progressed = True
                await asyncio.sleep(0.8)
            except Exception:
                continue
        if not progressed:
            break
    return clicked


_EXTRACT_COMMENTS_JS = """() => {
    const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
    const read = (el) => {
        const id = (el.id || '').replace(/^comment-/, '');
        const name = clean(el.querySelector('.author .name, .author-wrapper .name, .name')?.innerText
                         || el.querySelector('a[href*="/user/profile/"]')?.innerText);
        const isAuthor = !!el.querySelector('.tag, .author-tag') && /作者/.test(el.innerText.slice(0, 80));
        const content = clean(el.querySelector('.content, .note-text')?.innerText);
        const time = clean(el.querySelector('.date, .time')?.innerText);
        return { id, user: name, content, time, is_author: isAuthor };
    };
    const out = [];
    document.querySelectorAll('.parent-comment').forEach(pc => {
        const top = pc.querySelector(':scope > .comment-item, .comment-item:not(.comment-item-sub)');
        if (!top) return;
        const item = read(top);
        item.replies = [...pc.querySelectorAll('.comment-item-sub')].map(read);
        out.push(item);
    });
    return out;
}"""


def process_url(url: str) -> str:
    """处理URL，确保格式正确并保留所有参数
    
    Args:
        url: 原始URL
    
    Returns:
        str: 处理后的URL
    """
    processed_url = url.strip()
    
    # 移除可能的@符号前缀
    if processed_url.startswith('@'):
        processed_url = processed_url[1:]
    
    # 确保URL使用https协议
    if processed_url.startswith('http://'):
        processed_url = 'https://' + processed_url[7:]
    elif not processed_url.startswith('https://'):
        processed_url = 'https://' + processed_url
        
    # 主站 URL 补 www，但子域名（creator. 等）不动
    if 'xiaohongshu.com' in processed_url and 'www.xiaohongshu.com' not in processed_url:
        from urllib.parse import urlparse
        _host = urlparse(processed_url).hostname or ''
        if _host == 'xiaohongshu.com':
            processed_url = processed_url.replace('xiaohongshu.com', 'www.xiaohongshu.com', 1)
    
    # 笔记链接没带 xsec_token 时，用缓存补上（缓存没有就原样返回）
    processed_url = _add_cached_token(processed_url)
    return processed_url

def _cdp_alive() -> bool:
    """检查 Chrome 是否已在监听 CDP 端口。"""
    import urllib.request
    try:
        urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=2)
        return True
    except Exception:
        return False


async def _ensure_chrome_running():
    """确保 Chrome 以远程调试模式运行；若未运行则自动启动。"""
    import subprocess
    if _cdp_alive():
        return
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":0")
    subprocess.Popen(
        [
            "/usr/bin/google-chrome",
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={BROWSER_DATA_DIR}",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(20):
        await asyncio.sleep(0.5)
        if _cdp_alive():
            return
    raise RuntimeError("Chrome 启动超时，请手动确认 Chrome 是否可以运行")


async def ensure_browser(_from_idle: bool = False):
    """确保浏览器已连接（CDP 模式连接真实 Chrome）。
    若连接已断开则自动重连，无需手动重启 MCP。"""
    global browser_instance, browser_obj, browser_context, main_page, is_logged_in
    global _last_tool_ts, _idle_abort
    if not _from_idle:
        _last_tool_ts = _time.time()
        _idle_abort = True  # 有正经操作进来，正在闲逛的话马上收手

    global notifications_page
    # 健康检查：Chrome 被手动关掉 / CDP 断开时自动重置
    # 注意：browser_context.pages 是本地缓存，Chrome 关了也不报错，必须看 is_connected() 和 CDP 端口
    if browser_context is not None:
        _dead = False
        try:
            if browser_obj is not None and not browser_obj.is_connected():
                _dead = True
            elif not _cdp_alive():
                _dead = True
        except Exception:
            _dead = True
        if _dead:
            try:
                if browser_instance is not None:
                    await browser_instance.stop()
            except Exception:
                pass
            browser_instance = None
            browser_obj = None
            browser_context = None
            main_page = None
            notifications_page = None
            is_logged_in = False

    # main_page 标签页被单独关掉：换一个可用页面
    if browser_context is not None and (main_page is None or main_page.is_closed()):
        _open = [pg for pg in browser_context.pages if not pg.is_closed()]
        main_page = _open[0] if _open else await browser_context.new_page()
        main_page.set_default_timeout(60000)
        is_logged_in = False

    if notifications_page is not None and notifications_page.is_closed():
        notifications_page = None

    if browser_context is None:
        await _ensure_chrome_running()

        browser_instance = await async_playwright().start()
        # 连接到真实 Chrome（使用其现有 profile，无需 storage_state）
        browser_obj = await browser_instance.chromium.connect_over_cdp(CDP_URL)

        # 取第一个已有 context（真实 Chrome 始终有一个 default context）
        if browser_obj.contexts:
            browser_context = browser_obj.contexts[0]
        else:
            browser_context = await browser_obj.new_context()

        # 风控守卫：之后新开的每个标签页都自动装上
        browser_context.on("page", _arm_page)
        browser_context.on("response", _watch_on_response)

        # 注入 attachShadow 拦截器，使 closed shadow root 也可被访问
        await browser_context.add_init_script("""
            window.__shadowRoots = new WeakMap();
            const _orig = Element.prototype.attachShadow;
            Element.prototype.attachShadow = function(init) {
                const shadow = _orig.call(this, init);
                window.__shadowRoots.set(this, shadow);
                return shadow;
            };
        """)

        if browser_context.pages:
            main_page = browser_context.pages[0]
        else:
            main_page = await browser_context.new_page()

        main_page.set_default_timeout(60000)

    # 已有标签页补装风控守卫（幂等）
    if browser_context is not None:
        for _pg in browser_context.pages:
            _arm_page(_pg)
    _b = _RateLimit.breaker_message()
    if _b:
        raise _RiskBlocked(_b)

    # 检查登录状态
    if not is_logged_in:
        if main_page:
            try:
                await main_page.goto("https://www.xiaohongshu.com", timeout=60000)
                await _rand_sleep(3)
                login_elements = await main_page.query_selector_all('text="登录"')
                if login_elements:
                    return False
                else:
                    is_logged_in = True
                    return True
            except _RiskBlocked:
                raise
            except Exception:
                return False
        return False

    return True

# ── 通知看门狗 ────────────────────────────────────────────────────────────
# 小红书页面自己每 ~30 秒轮询一次 unread_count，这里只被动监听，不额外请求。
# 发现新的评论/@/关注 → 打开通知页取详情 → 新内容写进唤醒日志（JSON 一行一条，格式同花园唤醒桥）。
#   普通通知：攒着，每 30~60 分钟（只在 XHS_WATCH_HOURS 时段内）合并写一次
#   关联号（XHS_PRIORITY_USERS）的评论/@：立刻写，顺带把攒着的一起写出
# 环境变量：XHS_WATCH=0 关闭；XHS_WAKE_LOG 日志路径；XHS_PRIORITY_USERS 关联号 user_id 或昵称，逗号分隔；
#           XHS_WATCH_HOURS 普通通知的叫醒时段，默认 8-23
XHS_WATCH_ON = os.environ.get("XHS_WATCH", "1") != "0"
XHS_WAKE_LOG = os.path.expanduser(os.environ.get("XHS_WAKE_LOG", "~/xhs-wake-events.log"))
XHS_PRIORITY = {x.strip() for x in os.environ.get("XHS_PRIORITY_USERS", "").split(",") if x.strip()}
try:
    _wh = os.environ.get("XHS_WATCH_HOURS", "8-23").split("-")
    XHS_WATCH_HOURS = (int(_wh[0]), int(_wh[1]))
except Exception:
    XHS_WATCH_HOURS = (8, 23)
_WATCH_FETCH_GAP_DAY = 10 * 60     # 白天两次打开通知页至少隔 10 分钟
_WATCH_FETCH_GAP_NIGHT = 30 * 60   # 夜里至少隔 30 分钟
_watch_pending: list = []
_watch_last_fetch = 0.0
_watch_fetch_scheduled = False
_watch_started = False


def _watch_in_hours() -> bool:
    return XHS_WATCH_HOURS[0] <= datetime.now().hour < XHS_WATCH_HOURS[1]


def _watch_db():
    conn = sqlite3.connect(ACTIONS_DB, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS notif_seen (msg_id TEXT PRIMARY KEY, seen_at REAL)")
    return conn


def _wake_write(tag: str, message: str, reason: str):
    try:
        with open(XHS_WAKE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"tag": tag, "message": message, "reason": reason,
                                "ts": datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')}, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[watch] 写唤醒日志失败: {e}", file=sys.stderr, flush=True)


def _watch_flush(priority_lines=None):
    """把攒着的普通通知（和关联号通知）一次写出去。"""
    global _watch_pending
    lines = list(priority_lines or []) + _watch_pending
    if not lines:
        return
    _watch_pending = []
    tag = "📕 小红书·关联号" if priority_lines else "📕 小红书通知"
    reason = "xhs_priority_mention" if priority_lines else "xhs_notifications"
    _wake_write(tag, f"小红书有 {len(lines)} 条新通知：\n" + "\n".join(lines) +
                "\n（回复用 reply_comment(note_id, comment_id, content)，token 自动补；关注用 follow_user）", reason)
    print(f"[watch] 已写唤醒日志：{len(lines)} 条", file=sys.stderr, flush=True)


def _watch_process(kind: str, msgs: list):
    """处理通知接口返回的 message_list。kind: mentions / connections。任何地方打开通知页都会经过这里，
    所以我自己用 get_notifications 看过的，也会被记为已见、不再叫醒。"""
    if not XHS_WATCH_ON or not msgs:
        return
    try:
        with _watch_db() as conn:
            # 每种通知各自做首次基线（id 加 kind 前缀），避免先看到关注、后看到评论时把旧评论全当新的
            first_run = conn.execute("SELECT COUNT(*) FROM notif_seen WHERE msg_id LIKE ?", (f"{kind}:%",)).fetchone()[0] == 0
            keys = [f"{kind}:{m.get('id', '')}" for m in msgs]
            seen = {r[0] for r in conn.execute(
                f"SELECT msg_id FROM notif_seen WHERE msg_id IN ({','.join('?' * len(keys))})", keys)}
            new = [m for m in msgs if m.get('id') and f"{kind}:{m['id']}" not in seen]
            conn.executemany("INSERT OR IGNORE INTO notif_seen (msg_id, seen_at) VALUES (?, ?)",
                             [(f"{kind}:{m['id']}", _time.time()) for m in new])
            replied = {r[0] for r in conn.execute(
                "SELECT comment_id FROM comment_history WHERE action_type='replied' AND comment_id IS NOT NULL")}
    except Exception as e:
        print(f"[watch] 读写已见记录失败: {e}", file=sys.stderr, flush=True)
        return
    if first_run:
        print(f"[watch] 首次运行，{len(new)} 条旧通知记为已见，不叫醒", file=sys.stderr, flush=True)
        return
    prio, normal = [], []
    for m in reversed(new):  # 旧的在前
        u = m.get('user_info') or m.get('user') or {}
        uid, nick = u.get('userid', '') or u.get('user_id', ''), u.get('nickname', '?')
        is_prio = uid in XHS_PRIORITY or nick in XHS_PRIORITY
        if kind == 'connections':
            line = f"➕ {nick} 关注了你（user_id: {uid}）"
            normal.append(line)
            continue
        ci = m.get('comment_info') or {}
        cid = ci.get('id', '')
        if cid and cid in replied:
            continue
        note = m.get('item_info') or {}
        nid = note.get('id', '')
        _remember_note_token(nid, note.get('xsec_token', ''))
        ntitle = (note.get('content') or note.get('display_title') or '')[:20]
        text = (ci.get('content') or '').replace('\n', ' ')
        line = f"💬 {nick} {m.get('title', '')}《{ntitle}》：{text}"
        tgt = ((ci.get('target_comment') or {}).get('content') or '').replace('\n', ' ')
        if tgt:
            line += f"\n   ↪ 回的是：{tgt[:60]}{'…' if len(tgt) > 60 else ''}"
        line += f"\n   note_id: {nid}  comment_id: {cid}"
        (prio if is_prio else normal).append(("⚡" + line) if is_prio else line)
    _watch_pending.extend(normal)
    if prio:
        _watch_flush(prio)


async def _watch_open_notifications(page) -> bool:
    """像真人一样：在站内页面上点侧栏的"通知"入口进去（单页内跳转，带来源页）。
    点不到入口就退回直接 goto。返回是否成功进入通知页。"""
    for sel in ('a[href*="/notification"]', 'text="通知"'):
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                await _human_click(page, await loc.element_handle())
                await page.wait_for_url("**/notification**", timeout=8000)
                return True
        except Exception:
            continue
    await page.goto("https://www.xiaohongshu.com/notification", timeout=30000)
    return True


_notif_page_lock = asyncio.Lock()  # 常驻通知页同一时间只给一个人用（看门狗 / 回复）


async def _enter_notifications(page):
    """先让常驻页面停在站内普通页面，再点侧栏通知入口进通知页（看门狗和回复共用）。"""
    if 'xiaohongshu.com' not in page.url or '/notification' in page.url:
        await page.goto("https://www.xiaohongshu.com/explore", timeout=30000)
        await asyncio.sleep(_random.uniform(3, 6))
    else:
        await asyncio.sleep(_random.uniform(1, 3))
    await _watch_open_notifications(page)


async def _watch_leave_notifications(page):
    """看完回到发现页（点侧栏入口，点不到就 goto），让常驻页面停在站内普通页面，下次再点通知入口进来。"""
    try:
        loc = page.locator('a.link-wrapper[href^="/explore"]').first  # 侧栏"首页"入口，别匹配到笔记卡片
        if await loc.count() and await loc.is_visible():
            await _human_click(page, await loc.element_handle())
            await asyncio.sleep(_random.uniform(1.5, 3))
            if '/notification' not in page.url:
                return
        await page.goto("https://www.xiaohongshu.com/explore", timeout=30000)
    except Exception:
        pass


async def _watch_fetch(delay: float = 0, include_follows: bool = True):
    """用常驻通知标签页看通知（评论@ + 新增关注），由 response 监听器负责处理数据。
    和 get_notifications 共用 notifications_page：先停在站内普通页面，再点侧栏通知入口进去。"""
    global _watch_last_fetch, _watch_fetch_scheduled, notifications_page
    try:
        if delay:
            await asyncio.sleep(delay)
        if _RateLimit.breaker_message() or not browser_context:
            return
        if _time.time() - _last_tool_ts < 60:  # 刚有工具在用浏览器，别抢常驻页面，下次再看
            return
        if _notif_page_lock.locked():  # 回复正在用常驻页面，这轮不抢
            return
        async with _notif_page_lock:
            _watch_last_fetch = _time.time()
            if notifications_page is None or notifications_page.is_closed():
                notifications_page = await browser_context.new_page()
                notifications_page.set_default_timeout(30000)
            page = notifications_page
            await _enter_notifications(page)
            await asyncio.sleep(_random.uniform(3, 6))
            if include_follows:
                tab = page.locator('text="新增关注"').first
                if await tab.count():
                    await _human_click(page, await tab.element_handle())
                    await asyncio.sleep(_random.uniform(2, 4))
            await asyncio.sleep(_random.uniform(0.5, 1.5))
            await _watch_leave_notifications(page)
    except _RiskBlocked:
        pass
    except Exception as e:
        print(f"[watch] 看通知出错：{e}", file=sys.stderr, flush=True)
    finally:
        _watch_fetch_scheduled = False


async def _watch_on_response(resp):
    """挂在 browser_context 上的 response 监听器。"""
    global _watch_fetch_scheduled
    url = resp.url
    if 'xiaohongshu.com' not in url:
        return
    try:
        if '/api/sns/web/unread_count' in url:
            d = ((await resp.json()) or {}).get('data') or {}
            if not (d.get('mentions') or d.get('connections')) or _watch_fetch_scheduled:
                return
            gap = _WATCH_FETCH_GAP_DAY if _watch_in_hours() else _WATCH_FETCH_GAP_NIGHT
            if _time.time() - _watch_last_fetch < gap:
                return
            _watch_fetch_scheduled = True
            # 像人一样：看到小红点过一会儿才点开
            asyncio.get_running_loop().create_task(
                _watch_fetch(delay=_random.uniform(40, 180), include_follows=bool(d.get('connections'))))
        elif '/you/mentions' in url:
            _watch_process('mentions', (((await resp.json()) or {}).get('data') or {}).get('message_list') or [])
        elif '/you/connections' in url:
            _watch_process('connections', (((await resp.json()) or {}).get('data') or {}).get('message_list') or [])
    except Exception:
        pass


async def _watch_loop():
    """兜底巡查：每 30~60 分钟看一次通知并把攒着的写出去（只在 XHS_WATCH_HOURS 时段内）。
    顺带保证浏览器开着，这样页面自己的 unread_count 轮询才能跑起来。"""
    await asyncio.sleep(_random.uniform(2, 6) * 60)
    while True:
        try:
            if _watch_in_hours() and not _RateLimit.breaker_message():
                if await ensure_browser(_from_idle=True):
                    if _time.time() - _watch_last_fetch > _WATCH_FETCH_GAP_DAY:
                        await _watch_fetch()
                    _watch_flush()
        except _RiskBlocked:
            pass
        except Exception as e:
            print(f"[watch] 巡查出错：{e}", file=sys.stderr, flush=True)
        await asyncio.sleep(_random.uniform(30, 60) * 60)


# ── 闲逛（养号）：后台定时随机刷首页、点开几篇看看，不经过 LLM，不点赞不评论 ──────
IDLE_HOURS = (9, 23)            # 只在 9:00~23:00 之间闲逛
IDLE_GAP_HOURS = (1.5, 5.0)     # 两次闲逛之间隔 1.5~5 小时（随机），一天大约 3~5 次
IDLE_QUIET_AFTER_TOOL = 15 * 60 # 工具刚用过 15 分钟内不闲逛
_last_tool_ts = 0.0
_idle_abort = False
_idle_started = False


def _idle_should_stop() -> bool:
    return _idle_abort or bool(_RateLimit.breaker_message())


async def _idle_session():
    global _idle_abort
    _idle_abort = False
    ok = await ensure_browser(_from_idle=True)
    if not ok or not browser_context:
        print("[idle] 浏览器未就绪或未登录，跳过本次闲逛", file=sys.stderr, flush=True)
        return
    page = await browser_context.new_page()
    page.set_default_timeout(30000)
    opened = 0
    target_open = _random.randint(1, 3)
    try:
        await page.goto("https://www.xiaohongshu.com/explore", timeout=30000)
        await _rand_sleep(_random.uniform(3, 6))
        for _ in range(_random.randint(4, 9)):
            if _idle_should_stop():
                break
            vp = page.viewport_size or {'width': 1280, 'height': 800}
            await page.mouse.move(vp['width'] * _random.uniform(0.2, 0.8), vp['height'] * _random.uniform(0.3, 0.7),
                                  steps=_random.randint(5, 12))
            await page.mouse.wheel(0, _random.randint(300, 900))
            await asyncio.sleep(_random.uniform(2, 7))
            if opened >= target_open or _random.random() > 0.45:
                continue
            # 在当前视口里随便挑一张卡片点开
            idxs = await page.evaluate("""() => [...document.querySelectorAll('section.note-item a.cover')]
                .map((a, i) => { const r = a.getBoundingClientRect(); return (r.top > 60 && r.bottom < innerHeight - 20) ? i : -1; })
                .filter(i => i >= 0)""")
            if not idxs:
                continue
            card = await page.locator('section.note-item a.cover').nth(_random.choice(idxs)).element_handle()
            await _human_click(page, card)
            await asyncio.sleep(_random.uniform(3, 6))
            for _ in range(_random.randint(1, 4)):  # 在笔记里看看图文和评论
                if _idle_should_stop():
                    break
                await page.mouse.wheel(0, _random.randint(150, 500))
                await asyncio.sleep(_random.uniform(2, 6))
            await asyncio.sleep(_random.uniform(4, 20))
            await page.keyboard.press('Escape')
            await asyncio.sleep(_random.uniform(1.5, 3.5))
            if '/explore/' in page.url:  # 万一不是弹窗而是整页跳转
                await page.go_back()
                await asyncio.sleep(_random.uniform(2, 4))
            opened += 1
        print(f"[idle] 闲逛结束：点开 {opened} 篇{'（被打断）' if _idle_abort else ''}", file=sys.stderr, flush=True)
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def _idle_loop():
    await asyncio.sleep(_random.uniform(10, 30) * 60)  # 服务启动后先等一阵
    while True:
        try:
            hour = datetime.now().hour
            quiet = _time.time() - _last_tool_ts < IDLE_QUIET_AFTER_TOOL
            if IDLE_HOURS[0] <= hour < IDLE_HOURS[1] and not quiet and not _RateLimit.breaker_message():
                await _idle_session()
            else:
                print(f"[idle] 跳过（{'时段外' if not (IDLE_HOURS[0] <= hour < IDLE_HOURS[1]) else '刚用过工具' if quiet else '熔断中'}）",
                      file=sys.stderr, flush=True)
        except _RiskBlocked:
            pass
        except Exception as e:
            print(f"[idle] 闲逛出错：{e}", file=sys.stderr, flush=True)
        await asyncio.sleep(_random.uniform(*IDLE_GAP_HOURS) * 3600)


@_asynccontextmanager
async def _lifespan(server):
    global _idle_started
    if not _idle_started and os.environ.get("XHS_IDLE", "1") != "0":
        _idle_started = True
        asyncio.get_running_loop().create_task(_idle_loop())
        print("[idle] 闲逛任务已启动", file=sys.stderr, flush=True)
    global _watch_started
    if not _watch_started and XHS_WATCH_ON:
        _watch_started = True
        asyncio.get_running_loop().create_task(_watch_loop())
        print(f"[watch] 通知看门狗已启动，日志 {XHS_WAKE_LOG}，关联号 {len(XHS_PRIORITY)} 个", file=sys.stderr, flush=True)
    yield


@mcp.tool()
async def follow_user(user_id: str, xsec_token: str = "") -> str:
    """关注一个用户。会先在对方主页看一会儿笔记再点关注，已关注的不会重复点。

    Args:
        user_id: 用户 hex ID（如 63a6d4e70000000026007f5e，来自 search_user / 通知 / 笔记作者）
        xsec_token: 用户主页的 xsec_token，可留空（search_user 查过的会自动用缓存）
    """
    _rl = _RateLimit.check('follow')
    if _rl:
        return _rl
    if not await ensure_browser():
        return "请先登录小红书账号"
    token = xsec_token or _user_token_cache.get(user_id, '')
    url = f"https://www.xiaohongshu.com/user/profile/{user_id}"
    if token:
        url += f"?xsec_token={token}&xsec_source=pc_note"
    page = await browser_context.new_page()
    page.set_default_timeout(30000)
    try:
        await page.goto(url, timeout=30000, referer="https://www.xiaohongshu.com/explore")
        await asyncio.sleep(_random.uniform(3, 5))
        nick = await page.evaluate("() => (document.querySelector('.user-nickname, [class*=\"user-name\"]')||{}).innerText || ''")
        # 先看看对方的笔记，再回到顶部点关注
        for _ in range(_random.randint(1, 3)):
            await page.mouse.wheel(0, _random.randint(300, 700))
            await asyncio.sleep(_random.uniform(2, 5))
        await page.mouse.wheel(0, -3000)
        await asyncio.sleep(_random.uniform(1.5, 3))

        btn = page.locator('button.follow-button').first
        if not await btn.count():
            return f"找不到关注按钮（{nick or user_id}），可能是自己的主页或页面没加载出来"
        state = (await btn.inner_text()).strip()
        if '已关注' in state or '互相关注' in state:
            return f"已经关注过 {nick or user_id} 了（{state}）"
        await _human_click(page, await btn.element_handle())
        await _rand_sleep(2)
        risk = await _detect_risk(page)
        if risk:
            _RateLimit.trip(risk)
            return f"🛑 点关注后检测到风控（{risk}），已熔断。请到浏览器确认是否关注成功。"
        after = (await btn.inner_text()).strip()
        if '已关注' in after or '互相关注' in after:
            _RateLimit.record('follow')
            _record_comment('followed', user_id, content=nick)
            return f"✅ 已关注 {nick or user_id}（{after}）"
        return f"⚠️ 点了关注，但按钮还是「{after}」，请到浏览器确认"
    finally:
        await asyncio.sleep(_random.uniform(0.5, 1.5))
        await page.close()


@mcp.tool()
def risk_status(clear: bool = False) -> str:
    """查看小红书风控熔断状态。clear=True 时解除熔断（仅在已于浏览器里手动通过验证后使用）。"""
    if clear:
        _RateLimit.clear()
        return "✅ 已解除风控熔断。建议先慢一点，过一阵再评论/点赞。"
    msg = _RateLimit.breaker_message()
    if msg:
        return msg
    try:
        with _rl_db() as conn:
            row = conn.execute("SELECT reason, tripped_at, trip_count FROM risk_state WHERE id=1").fetchone()
    except Exception:
        row = None
    if row and row[1]:
        return f"🟢 当前未熔断。上次触发：{datetime.fromtimestamp(row[1]).strftime('%m-%d %H:%M')}（{row[0]}），累计 {row[2]} 次。"
    return "🟢 当前未熔断，从未触发过。"


@mcp.tool()
async def login() -> str:
    """登录小红书账号。首次使用或 session 过期时调用，其他工具会自动检测登录状态，无需每次手动调用。"""
    global is_logged_in

    await ensure_browser()

    if not main_page:
        return "浏览器初始化失败，请重试"

    # 始终导航到首页验证实际登录状态（不信任内存里的 is_logged_in，cookie 可能已过期）
    await main_page.goto("https://www.xiaohongshu.com", timeout=60000)
    await _rand_sleep(3)

    # 查找登录按钮，不存在说明 cookie 有效、已经登录
    login_elements = await main_page.query_selector_all('text="登录"')
    if not login_elements:
        is_logged_in = True
        return "已登录小红书账号"

    # 需要登录，等待用户在浏览器里扫码完成
    max_wait_time = 180  # 最多等 3 分钟
    wait_interval = 5
    waited_time = 0

    while waited_time < max_wait_time:
        still_login = await main_page.query_selector_all('text="登录"')
        if not still_login:
            is_logged_in = True
            await _rand_sleep(2)
            return "登录成功！"
        await asyncio.sleep(wait_interval)
        waited_time += wait_interval

    return "登录等待超时，请重试。"

@mcp.tool()
async def search_notes(keywords: str, limit: int = 20, verbose: bool = False) -> str:
    """根据关键词搜索笔记。没有直链、只有关键词时使用。已有笔记链接时直接用 get_note_content，不要用搜索代替。

    Args:
        keywords: 搜索关键词
        limit: 返回结果数量限制（默认20，最多100）
        verbose: False（默认）只返回标题、作者、点赞、链接；
                 True 额外返回笔记ID和xsecToken（需要对该笔记点赞/收藏/查看作者时用）
    """
    _rl = _RateLimit.check('search')
    if _rl:
        return _rl

    login_status = await ensure_browser()
    if not login_status:
        return "请先登录小红书账号"

    if not main_page:
        return "浏览器初始化失败，请重试"

    target_count = min(limit, 100)
    search_url = f"https://www.xiaohongshu.com/search_result?keyword={quote(keywords)}&source=web_explore_feed"

    try:
        await main_page.goto(search_url, timeout=60000)

        # 等待 __INITIAL_STATE__ 加载
        await main_page.wait_for_function(
            "() => window.__INITIAL_STATE__ !== undefined",
            timeout=30000
        )
        await _rand_sleep(2)

        def extract_feeds_js():
            return """
            () => {
                const state = window.__INITIAL_STATE__;
                if (!state?.search?.feeds) return '[]';
                const feeds = state.search.feeds;
                const feedsData = feeds.value !== undefined ? feeds.value : feeds._value;
                return feedsData ? JSON.stringify(feedsData) : '[]';
            }
            """

        unique_items = {}

        async def collect_feeds():
            raw = await main_page.evaluate(extract_feeds_js())
            feeds = json.loads(raw) if raw else []
            for item in feeds:
                item_id = item.get('id')
                if item_id and item_id not in unique_items:
                    note_card = item.get('noteCard') or item.get('note_card') or {}
                    user = note_card.get('user') or {}
                    interact = note_card.get('interactInfo') or note_card.get('interact_info') or {}
                    cover = note_card.get('cover') or {}
                    _remember_note_token(item_id, item.get('xsec_token') or item.get('xsecToken') or '')
                    unique_items[item_id] = {
                        'id': item_id,
                        'xsecToken': item.get('xsec_token') or item.get('xsecToken') or '',
                        'title': note_card.get('displayTitle') or note_card.get('display_title') or note_card.get('title') or '',
                        'type': note_card.get('type') or 'normal',
                        'author': user.get('nickname') or '',
                        'likes': interact.get('likedCount') or interact.get('liked_count') or '0',
                    }

        # 首屏
        await collect_feeds()

        # 滚动加载更多
        no_new = 0
        while len(unique_items) < target_count and no_new < 3:
            prev = len(unique_items)
            await main_page.evaluate("window.scrollBy(0, 800)")
            await _rand_sleep(1.5)
            await collect_feeds()
            if len(unique_items) == prev:
                no_new += 1
            else:
                no_new = 0

        items = list(unique_items.values())[:target_count]

        if not items:
            return f"未找到与\"{keywords}\"相关的笔记"

        result = f"搜索「{keywords}」，共找到 {len(items)} 条笔记：\n\n"
        for i, item in enumerate(items, 1):
            url = f"https://www.xiaohongshu.com/explore/{item['id']}?xsec_token={item['xsecToken']}&xsec_source=pc_search"
            if verbose:
                result += (
                    f"{i}. 【{item['title'] or '无标题'}】  {item['type']}\n"
                    f"   作者: {item['author']}  ❤️{item['likes']}\n"
                    f"   笔记ID: {item['id']}\n"
                    f"   xsecToken: {item['xsecToken']}\n"
                    f"   链接: {url}\n\n"
                )
            else:
                result += (
                    f"{i}. 【{item['title'] or '无标题'}】  {item['author']}  ❤️{item['likes']}\n"
                    f"   {url}\n\n"
                )
        _RateLimit.record('search')
        return result

    except Exception as e:
        return f"搜索笔记时出错: {str(e)}"

@mcp.tool()
async def get_note_content(url: str, include_images: bool = True) -> str:
    """获取笔记正文、配图和前15条评论，一次调用返回完整内容。有笔记链接时首选此工具，不要用 search_notes 代替。
    支持完整链接（xiaohongshu.com/explore/...）和短链（xhslink.cn/...）。
    如需获取更多评论，再单独调用 get_note_comments。

    Args:
        url: 笔记 URL
        include_images: 是否处理配图（默认 True）。
            ≤4张：下载到本地，返回路径供 AI 直接读取图片内容；
            >4张：调用 Gemini 3.6 Flash 分析图片，返回文字描述。
    """
    login_status = await ensure_browser()
    if not login_status:
        return "请先登录小红书账号"
    
    if not main_page:  # 添加空检查
        return "浏览器初始化失败，请重试"
        
    try:
        # 使用通用URL处理函数
        processed_url = process_url(url)
        print(f"处理后的URL: {processed_url}")
        
        # 访问帖子链接，保留完整参数
        await main_page.goto(processed_url, timeout=60000)
        await asyncio.sleep(_random.uniform(12, 18))
        
        # 检查是否加载了错误页面
        if not main_page:  # 添加空检查
            return "浏览器初始化失败，请重试"
            
        error_page = await main_page.evaluate('''
            () => {
                // 检查常见的错误信息
                const errorTexts = [
                    "当前笔记暂时无法浏览",
                    "内容不存在",
                    "页面不存在",
                    "内容已被删除"
                ];
                
                for (const text of errorTexts) {
                    if (document.body.innerText.includes(text)) {
                        return {
                            isError: true,
                            errorText: text
                        };
                    }
                }
                
                return { isError: false };
            }
        ''')
        
        if error_page.get("isError", False):
            return f"无法获取笔记内容: {error_page.get('errorText', '未知错误')}\n请检查链接是否有效或尝试使用带有有效token的完整URL。"
        
        # 增强滚动操作以确保所有内容加载
        if not main_page:  # 添加空检查
            return "浏览器初始化失败，请重试"
            
        await main_page.evaluate('''
            () => {
                // 先滚动到页面底部
                window.scrollTo(0, document.body.scrollHeight);
                setTimeout(() => { 
                    // 然后滚动到中间
                    window.scrollTo(0, document.body.scrollHeight / 2); 
                }, 1000);
                setTimeout(() => { 
                    // 最后回到顶部
                    window.scrollTo(0, 0); 
                }, 2000);
            }
        ''')
        await _rand_sleep(3)  # 等待滚动完成和内容加载
        
        # 打印页面结构片段用于分析
        try:
            print("打印页面结构片段用于分析")
            if not main_page:  # 添加空检查
                return "浏览器初始化失败，请重试"
                
            page_structure = await main_page.evaluate('''
                () => {
                    // 获取笔记内容区域
                    const noteContent = document.querySelector('.note-content');
                    const detailDesc = document.querySelector('#detail-desc');
                    const commentArea = document.querySelector('.comments-container, .comment-list');
                    
                    return {
                        hasNoteContent: !!noteContent,
                        hasDetailDesc: !!detailDesc,
                        hasCommentArea: !!commentArea,
                        noteContentHtml: noteContent ? noteContent.outerHTML.slice(0, 500) : null,
                        detailDescHtml: detailDesc ? detailDesc.outerHTML.slice(0, 500) : null,
                        commentAreaFirstChild: commentArea ? 
                            (commentArea.firstElementChild ? commentArea.firstElementChild.outerHTML.slice(0, 500) : null) : null,
                        pageTitle: document.title,
                        bodyText: document.body.innerText.slice(0, 500)
                    };
                }
            ''')
            print(f"页面结构分析: {json.dumps(page_structure, ensure_ascii=False, indent=2)}")
            
            # 再次检查内容是否可见
            if "当前笔记暂时无法浏览" in page_structure.get("bodyText", ""):
                return "无法获取笔记内容: 当前笔记暂时无法浏览\n请检查链接是否有效或尝试使用带有有效token的完整URL。"
        except Exception as e:
            print(f"打印页面结构时出错: {str(e)}")
        
        # 获取帖子内容
        post_content = {}
        
        # 获取帖子标题 - 方法1：使用id选择器
        try:
            print("尝试获取标题 - 方法1：使用id选择器")
            if not main_page:  # 添加空检查
                return "浏览器初始化失败，请重试"
                
            title_element = await main_page.query_selector('#detail-title')
            if title_element:
                title = await title_element.text_content()
                post_content["标题"] = title.strip() if title else "未知标题"
                print(f"方法1获取到标题: {post_content['标题']}")
            else:
                print("方法1未找到标题元素")
                post_content["标题"] = "未知标题"
        except Exception as e:
            print(f"方法1获取标题出错: {str(e)}")
            post_content["标题"] = "未知标题"
        
        # 获取帖子标题 - 方法2：使用class选择器
        if post_content["标题"] == "未知标题":
            try:
                print("尝试获取标题 - 方法2：使用class选择器")
                if not main_page:  # 添加空检查
                    return "浏览器初始化失败，请重试"
                    
                title_element = await main_page.query_selector('div.title')
                if title_element:
                    title = await title_element.text_content()
                    post_content["标题"] = title.strip() if title else "未知标题"
                    print(f"方法2获取到标题: {post_content['标题']}")
                else:
                    print("方法2未找到标题元素")
            except Exception as e:
                print(f"方法2获取标题出错: {str(e)}")
        
        # 获取帖子标题 - 方法3：使用JavaScript
        if post_content["标题"] == "未知标题":
            try:
                print("尝试获取标题 - 方法3：使用JavaScript")
                if not main_page:  # 添加空检查
                    return "浏览器初始化失败，请重试"
                    
                title = await main_page.evaluate('''
                    () => {
                        // 尝试多种可能的标题选择器
                        const selectors = [
                            '#detail-title',
                            'div.title',
                            'h1',
                            'div.note-content div.title'
                        ];
                        
                        for (const selector of selectors) {
                            const el = document.querySelector(selector);
                            if (el && el.textContent.trim()) {
                                return el.textContent.trim();
                            }
                        }
                        return null;
                    }
                ''')
                if title:
                    post_content["标题"] = title
                    print(f"方法3获取到标题: {post_content['标题']}")
                else:
                    print("方法3未找到标题元素")
            except Exception as e:
                print(f"方法3获取标题出错: {str(e)}")
        
        # 获取作者 - 方法1：使用username类选择器
        try:
            print("尝试获取作者 - 方法1：使用username类选择器")
            author_element = await main_page.query_selector('span.username')
            if author_element:
                author = await author_element.text_content()
                post_content["作者"] = author.strip() if author else "未知作者"
                print(f"方法1获取到作者: {post_content['作者']}")
            else:
                print("方法1未找到作者元素")
                post_content["作者"] = "未知作者"
        except Exception as e:
            print(f"方法1获取作者出错: {str(e)}")
            post_content["作者"] = "未知作者"
        
        # 获取作者 - 方法2：使用链接选择器
        if post_content["作者"] == "未知作者":
            try:
                print("尝试获取作者 - 方法2：使用链接选择器")
                author_element = await main_page.query_selector('a.name')
                if author_element:
                    author = await author_element.text_content()
                    post_content["作者"] = author.strip() if author else "未知作者"
                    print(f"方法2获取到作者: {post_content['作者']}")
                else:
                    print("方法2未找到作者元素")
            except Exception as e:
                print(f"方法2获取作者出错: {str(e)}")
        
        # 获取作者 - 方法3：使用JavaScript
        if post_content["作者"] == "未知作者":
            try:
                print("尝试获取作者 - 方法3：使用JavaScript")
                author = await main_page.evaluate('''
                    () => {
                        // 尝试多种可能的作者选择器
                        const selectors = [
                            'span.username',
                            'a.name',
                            '.author-wrapper .username',
                            '.info .name'
                        ];
                        
                        for (const selector of selectors) {
                            const el = document.querySelector(selector);
                            if (el && el.textContent.trim()) {
                                return el.textContent.trim();
                            }
                        }
                        return null;
                    }
                ''')
                if author:
                    post_content["作者"] = author
                    print(f"方法3获取到作者: {post_content['作者']}")
                else:
                    print("方法3未找到作者元素")
            except Exception as e:
                print(f"方法3获取作者出错: {str(e)}")
        
        # 获取发布时间 - 方法1：使用date类选择器
        try:
            print("尝试获取发布时间 - 方法1：使用date类选择器")
            time_element = await main_page.query_selector('span.date')
            if time_element:
                time_text = await time_element.text_content()
                post_content["发布时间"] = time_text.strip() if time_text else "未知"
                print(f"方法1获取到发布时间: {post_content['发布时间']}")
            else:
                print("方法1未找到发布时间元素")
                post_content["发布时间"] = "未知"
        except Exception as e:
            print(f"方法1获取发布时间出错: {str(e)}")
            post_content["发布时间"] = "未知"
        
        # 获取发布时间 - 方法2：使用正则表达式匹配
        if post_content["发布时间"] == "未知":
            try:
                print("尝试获取发布时间 - 方法2：使用正则表达式匹配")
                time_selectors = [
                    'text=/编辑于/',
                    r'text=/\d{2}-\d{2}/',
                    r'text=/\d{4}-\d{2}-\d{2}/',
                    r'text=/\d+月\d+日/',
                    r'text=/\d+天前/',
                    r'text=/\d+小时前/',
                    'text=/今天/',
                    'text=/昨天/'
                ]
                
                for selector in time_selectors:
                    time_element = await main_page.query_selector(selector)
                    if time_element:
                        time_text = await time_element.text_content()
                        post_content["发布时间"] = time_text.strip() if time_text else "未知"
                        print(f"方法2获取到发布时间: {post_content['发布时间']}")
                        break
                    else:
                        print(f"方法2未找到发布时间元素: {selector}")
            except Exception as e:
                print(f"方法2获取发布时间出错: {str(e)}")
        
        # 获取发布时间 - 方法3：使用JavaScript
        if post_content["发布时间"] == "未知":
            try:
                print("尝试获取发布时间 - 方法3：使用JavaScript")
                time_text = await main_page.evaluate('''
                    () => {
                        // 尝试多种可能的时间选择器
                        const selectors = [
                            'span.date',
                            '.bottom-container .date',
                            '.date'
                        ];
                        
                        for (const selector of selectors) {
                            const el = document.querySelector(selector);
                            if (el && el.textContent.trim()) {
                                return el.textContent.trim();
                            }
                        }
                        
                        // 尝试查找包含日期格式的文本
                        const dateRegexes = [
                            /编辑于\\s*([\\d-]+)/,
                            /(\\d{2}-\\d{2})/,
                            /(\\d{4}-\\d{2}-\\d{2})/,
                            /(\\d+月\\d+日)/,
                            /(\\d+天前)/,
                            /(\\d+小时前)/,
                            /(今天)/,
                            /(昨天)/
                        ];
                        
                        const allText = document.body.textContent;
                        for (const regex of dateRegexes) {
                            const match = allText.match(regex);
                            if (match) {
                                return match[0];
                            }
                        }
                        
                        return null;
                    }
                ''')
                if time_text:
                    post_content["发布时间"] = time_text
                    print(f"方法3获取到发布时间: {post_content['发布时间']}")
                else:
                    print("方法3未找到发布时间元素")
            except Exception as e:
                print(f"方法3获取发布时间出错: {str(e)}")
        
        # 获取帖子正文内容 - 方法1：使用精确的ID和class选择器
        try:
            print("尝试获取正文内容 - 方法1：使用精确的ID和class选择器")
            
            # 先明确标记评论区域
            await main_page.evaluate('''
                () => {
                    const commentSelectors = [
                        '.comments-container', 
                        '.comment-list',
                        '.feed-comment',
                        'div[data-v-aed4aacc]',  // 根据您提供的评论HTML结构
                        '.content span.note-text'  // 评论中的note-text结构
                    ];
                    
                    for (const selector of commentSelectors) {
                        const elements = document.querySelectorAll(selector);
                        elements.forEach(el => {
                            if (el) {
                                el.setAttribute('data-is-comment', 'true');
                                console.log('标记评论区域:', el.tagName, el.className);
                            }
                        });
                    }
                }
            ''')
            
            # 先尝试获取detail-desc和note-text组合
            content_element = await main_page.query_selector('#detail-desc .note-text')
            if content_element:
                # 检查是否在评论区域内
                is_in_comment = await content_element.evaluate('(el) => !!el.closest("[data-is-comment=\'true\']") || false')
                if not is_in_comment:
                    content_text = await content_element.text_content()
                    if content_text and len(content_text.strip()) > 50:  # 增加长度阈值
                        post_content["内容"] = content_text.strip()
                        print(f"方法1获取到正文内容，长度: {len(post_content['内容'])}")
                    else:
                        print(f"方法1获取到的内容太短: {len(content_text.strip()) if content_text else 0}")
                        post_content["内容"] = "未能获取内容"
                else:
                    print("方法1找到的元素在评论区域内，跳过")
                    post_content["内容"] = "未能获取内容"
            else:
                print("方法1未找到正文内容元素")
                post_content["内容"] = "未能获取内容"
        except Exception as e:
            print(f"方法1获取正文内容出错: {str(e)}")
            post_content["内容"] = "未能获取内容"
        
        # 获取帖子正文内容 - 方法2：使用XPath选择器
        if post_content["内容"] == "未能获取内容":
            try:
                print("尝试获取正文内容 - 方法2：使用XPath选择器")
                # 使用XPath获取笔记内容区域
                content_text = await main_page.evaluate('''
                    () => {
                        const xpath = '//div[@id="detail-desc"]/span[@class="note-text"]';
                        const result = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
                        const element = result.singleNodeValue;
                        return element ? element.textContent.trim() : null;
                    }
                ''')
                
                if content_text and len(content_text) > 20:
                    post_content["内容"] = content_text
                    print(f"方法2获取到正文内容，长度: {len(post_content['内容'])}")
                else:
                    print(f"方法2获取到的内容太短或为空: {len(content_text) if content_text else 0}")
            except Exception as e:
                print(f"方法2获取正文内容出错: {str(e)}")
        
        # 获取帖子正文内容 - 方法3：使用JavaScript获取最长文本
        if post_content["内容"] == "未能获取内容":
            try:
                print("尝试获取正文内容 - 方法3：使用JavaScript获取最长文本")
                content_text = await main_page.evaluate('''
                    () => {
                        // 定义评论区域选择器
                        const commentSelectors = [
                            '.comments-container', 
                            '.comment-list',
                            '.feed-comment',
                            'div[data-v-aed4aacc]',
                            '.comment-item',
                            '[data-is-comment="true"]'
                        ];
                        
                        // 找到所有评论区域
                        let commentAreas = [];
                        for (const selector of commentSelectors) {
                            const elements = document.querySelectorAll(selector);
                            elements.forEach(el => commentAreas.push(el));
                        }
                        
                        // 查找可能的内容元素，排除评论区
                        const contentElements = Array.from(document.querySelectorAll('div#detail-desc, div.note-content, div.desc, span.note-text'))
                            .filter(el => {
                                // 检查是否在评论区域内
                                const isInComment = commentAreas.some(commentArea => 
                                    commentArea && commentArea.contains(el));
                                
                                if (isInComment) {
                                    console.log('排除评论区域内容:', el.tagName, el.className);
                                    return false;
                                }
                                
                                const text = el.textContent.trim();
                                return text.length > 100 && text.length < 10000;
                            })
                            .sort((a, b) => b.textContent.length - a.textContent.length);
                        
                        if (contentElements.length > 0) {
                            console.log('找到内容元素:', contentElements[0].tagName, contentElements[0].className);
                            return contentElements[0].textContent.trim();
                        }
                        
                        return null;
                    }
                ''')
                
                if content_text and len(content_text) > 100:  # 增加长度阈值
                    post_content["内容"] = content_text
                    print(f"方法3获取到正文内容，长度: {len(post_content['内容'])}")
                else:
                    print(f"方法3获取到的内容太短或为空: {len(content_text) if content_text else 0}")
            except Exception as e:
                print(f"方法3获取正文内容出错: {str(e)}")
        
        # 获取帖子正文内容 - 方法4：区分正文和评论内容
        if post_content["内容"] == "未能获取内容":
            try:
                print("尝试获取正文内容 - 方法4：区分正文和评论内容")
                content_text = await main_page.evaluate('''
                    () => {
                        // 首先尝试获取note-content区域
                        const noteContent = document.querySelector('.note-content');
                        if (noteContent) {
                            // 查找note-text，这通常包含主要内容
                            const noteText = noteContent.querySelector('.note-text');
                            if (noteText && noteText.textContent.trim().length > 50) {
                                return noteText.textContent.trim();
                            }
                            
                            // 如果没有找到note-text或内容太短，返回整个note-content
                            if (noteContent.textContent.trim().length > 50) {
                                return noteContent.textContent.trim();
                            }
                        }
                        
                        // 如果上面的方法都失败了，尝试获取所有段落并拼接
                        const paragraphs = Array.from(document.querySelectorAll('p'))
                            .filter(p => {
                                // 排除评论区段落
                                const isInComments = p.closest('.comments-container, .comment-list');
                                return !isInComments && p.textContent.trim().length > 10;
                            });
                            
                        if (paragraphs.length > 0) {
                            return paragraphs.map(p => p.textContent.trim()).join('\n\n');
                        }
                        
                        return null;
                    }
                ''')
                
                if content_text and len(content_text) > 50:
                    post_content["内容"] = content_text
                    print(f"方法4获取到正文内容，长度: {len(post_content['内容'])}")
                else:
                    print(f"方法4获取到的内容太短或为空: {len(content_text) if content_text else 0}")
            except Exception as e:
                print(f"方法4获取正文内容出错: {str(e)}")
        
        # 获取帖子正文内容 - 方法5：直接通过DOM结构定位
        if post_content["内容"] == "未能获取内容":
            try:
                print("尝试获取正文内容 - 方法5：直接通过DOM结构定位")
                content_text = await main_page.evaluate('''
                    () => {
                        // 根据您提供的HTML结构直接定位
                        const noteContent = document.querySelector('div.note-content');
                        if (noteContent) {
                            const detailTitle = noteContent.querySelector('#detail-title');
                            const detailDesc = noteContent.querySelector('#detail-desc');
                            
                            if (detailDesc) {
                                const noteText = detailDesc.querySelector('span.note-text');
                                if (noteText) {
                                    return noteText.textContent.trim();
                                }
                                return detailDesc.textContent.trim();
                            }
                        }
                        
                        // 尝试其他可能的结构
                        const descElements = document.querySelectorAll('div.desc');
                        for (const desc of descElements) {
                            // 检查是否在评论区
                            const isInComment = desc.closest('.comments-container, .comment-list, .feed-comment');
                            if (!isInComment && desc.textContent.trim().length > 100) {
                                return desc.textContent.trim();
                            }
                        }
                        
                        return null;
                    }
                ''')
                
                if content_text and len(content_text) > 100:
                    post_content["内容"] = content_text
                    print(f"方法5获取到正文内容，长度: {len(post_content['内容'])}")
                else:
                    print(f"方法5获取到的内容太短或为空: {len(content_text) if content_text else 0}")
            except Exception as e:
                print(f"方法5获取正文内容出错: {str(e)}")
        
        # 格式化返回结果
        result = f"标题: {post_content['标题']}\n"
        result += f"作者: {post_content['作者']}\n"
        result += f"发布时间: {post_content['发布时间']}\n"
        result += f"链接: {url}\n\n"
        result += f"内容:\n{post_content['内容']}"

        # ── 配图处理 ──────────────────────────────────────────────
        if include_images:
            try:
                # 从页面抓图片 URL
                img_urls = await main_page.evaluate("""() => {
                    const imgs = [];
                    // 笔记详情页的图片通常在 swiper 或 .note-slider 里
                    const selectors = [
                        '.swiper-slide img', '.note-slider img',
                        '.media-container img', '#detail-content img',
                        '.note-content img'
                    ];
                    for (const sel of selectors) {
                        for (const img of document.querySelectorAll(sel)) {
                            const src = img.src || img.dataset.src || '';
                            if (src && src.startsWith('http') && !imgs.includes(src))
                                imgs.push(src);
                        }
                    }
                    return imgs;
                }""")

                if img_urls:
                    img_dir = tempfile.mkdtemp(prefix="xhs_imgs_")
                    saved = []
                    headers = {"Referer": "https://www.xiaohongshu.com/"}
                    for i, u in enumerate(img_urls):
                        try:
                            r = requests.get(u, headers=headers, timeout=10)
                            ext = "jpg"
                            ct = r.headers.get("content-type", "")
                            if "png" in ct: ext = "png"
                            elif "webp" in ct: ext = "webp"
                            p = os.path.join(img_dir, f"img_{i+1}.{ext}")
                            with open(p, "wb") as f:
                                f.write(r.content)
                            saved.append(p)
                        except Exception:
                            pass

                    if saved:
                        if len(saved) <= 4:
                            result += f"\n\n📷 配图（{len(saved)} 张，路径如下，可直接读取）：\n"
                            for p in saved:
                                result += f"  {p}\n"
                        else:
                            result += f"\n\n📷 配图共 {len(saved)} 张（超过4张，已用 Gemini 分析）：\n"
                            analysis = _analyze_images_with_gemini(saved, post_content['标题'])
                            result += analysis
            except Exception as e:
                result += f"\n\n（图片处理出错：{e}）"

        # ── 前15条评论（页面已加载，顺带抓，省一次调用）────────────
        try:
            comments_text = await _quick_comments(main_page, limit=15)
            if comments_text:
                result += f"\n\n💬 前15条评论：\n{comments_text}"
        except Exception:
            pass

        return result

    except Exception as e:
        return f"获取笔记内容时出错: {str(e)}"

@mcp.tool()
async def get_note_comments(url: str) -> str:
    """获取笔记评论列表。返回的 comment_id 可用于 reply_comment 和 like_comment。

    Args:
        url: 笔记 URL
    """
    login_status = await ensure_browser()
    if not login_status:
        return "请先登录小红书账号"
    
    if not main_page:  # 添加空检查
        return "浏览器初始化失败，请重试"
        
    try:
        # 处理URL
        processed_url = _add_cached_token(process_url(url))
        print(f"处理后的评论URL: {processed_url}")
        
        # 访问帖子链接
        await main_page.goto(processed_url, timeout=60000)
        await asyncio.sleep(5)  # 等待页面加载
        _remember_token_from_url(main_page.url)
        
        # 检查是否加载了错误页面
        if not main_page:  # 添加空检查
            return "浏览器初始化失败，请重试"
            
        error_page = await main_page.evaluate('''
            () => {
                // 检查常见的错误信息
                const errorTexts = [
                    "当前笔记暂时无法浏览",
                    "内容不存在",
                    "页面不存在",
                    "内容已被删除"
                ];
                
                for (const text of errorTexts) {
                    if (document.body.innerText.includes(text)) {
                        return {
                            isError: true,
                            errorText: text
                        };
                    }
                }
                
                return { isError: false };
            }
        ''')
        
        if error_page.get("isError", False):
            return f"无法获取笔记评论: {error_page.get('errorText', '未知错误')}\n请检查链接是否有效或尝试使用带有有效token的完整URL。"
        
        # 先滚动到评论区
        if not main_page:  # 添加空检查
            return "浏览器初始化失败，请重试"
            
        comment_section_locators = []
        try:
            comment_section_locators = [
                main_page.get_by_text("条评论", exact=False),
                main_page.get_by_text("评论", exact=False),
                main_page.locator("text=评论").first
            ]
        except Exception as e:
            print(f"创建评论区定位器时出错: {str(e)}")
            # 继续执行，不阻断程序
        
        for locator in comment_section_locators:
            try:
                if locator and await locator.count() > 0:  # 添加空检查
                    await locator.scroll_into_view_if_needed(timeout=5000)
                    await _rand_sleep(2)
                    break
            except Exception as e:
                print(f"滚动到评论区时出错: {str(e)}")
                continue
        
        # 滚动页面以加载更多评论
        for i in range(8):
            try:
                if not main_page:  # 添加空检查
                    break
                    
                await main_page.evaluate("window.scrollBy(0, 500)")
                await _rand_sleep(1)
                
                # 尝试点击"查看更多评论"按钮
                more_comment_selectors = [
                    "text=查看更多评论",
                    "text=展开更多评论",
                    "text=加载更多",
                    "text=查看全部"
                ]
                
                for selector in more_comment_selectors:
                    try:
                        if not main_page:  # 添加空检查
                            break
                            
                        more_btn = main_page.locator(selector).first
                        if more_btn and await more_btn.count() > 0 and await more_btn.is_visible():  # 添加空检查
                            await more_btn.click()
                            await _rand_sleep(2)
                    except Exception as e:
                        print(f"点击查看更多按钮时出错: {str(e)}")
                        continue
            except Exception as e:
                print(f"滚动页面加载更多评论时出错: {str(e)}")
                pass
        
        # 获取评论
        comments = []

        if not main_page:
            return "浏览器初始化失败，请重试"

        # ── 展开所有楼中楼，按层级提取（一级评论 + 其下回复）─────────────
        try:
            await _expand_replies(main_page)
            await _rand_sleep(1)
            threads = await main_page.evaluate(_EXTRACT_COMMENTS_JS)
        except Exception as e:
            print(f"层级提取评论出错: {e}")
            threads = []
        if threads:
            import re as _re
            _m = _re.search(r'/explore/([0-9a-f]{24})', main_page.url)
            _nid = _m.group(1) if _m else ''
            _tk = _note_token_cache.get(_nid, '')
            _total = sum(1 + len(t['replies']) for t in threads)
            result = f"笔记 note_id: {_nid}"
            if _tk:
                result += f"  xsec_token: {_tk}"
            result += f"\n共 {len(threads)} 条一级评论、{_total - len(threads)} 条楼中楼：\n\n"
            for i, t in enumerate(threads, 1):
                au = "[作者]" if t['is_author'] else ""
                result += f"{i}. {t['user']}{au}（{t['time']}）: {t['content']}  [comment_id: {t['id']}]\n"
                for r in t['replies']:
                    au = "[作者]" if r['is_author'] else ""
                    result += f"    ↳ {r['user']}{au}（{r['time']}）: {r['content']}  [comment_id: {r['id']}]\n"
                result += "\n"
            return result

        # ── 降级：从 __INITIAL_STATE__ 提取（速度快且有完整 comment_id）────
        try:
            state_comments = await main_page.evaluate("""() => {
                const state = window.__INITIAL_STATE__;
                const list = state?.comment?.comments
                          || state?.commentModule?.commentList
                          || [];
                return list.map(c => ({
                    id:      c.id || c.commentId || '',
                    user:    c.userInfo?.nickname || c.user?.nickname || '?',
                    content: c.content || '',
                    time:    c.createTime || c.time || ''
                }));
            }""")
            if state_comments:
                for c in state_comments:
                    if c['content']:
                        comments.append({
                            "comment_id": c['id'],
                            "用户名": c['user'],
                            "内容": c['content'],
                            "时间": c['time'],
                        })
        except Exception as e:
            print(f"从 __INITIAL_STATE__ 提取评论出错: {e}")

        # ── 降级：DOM 抓取，从元素 id 属性拿 comment_id ──────────────────
        if not comments:
            comment_selectors = [
                "div.comment-item",
                "div.commentItem",
                "div.comment-content",
                "div.comment-wrapper",
                "section.comment",
                "div.feed-comment"
            ]
            for selector in comment_selectors:
                try:
                    comment_elements = main_page.locator(selector)
                    count = await comment_elements.count()
                    if count == 0:
                        continue
                    for i in range(count):
                        try:
                            el = comment_elements.nth(i)

                            # comment_id：从元素 id="comment-{id}" 提取
                            comment_id = ""
                            try:
                                el_id = await el.get_attribute('id') or ""
                                if el_id.startswith('comment-'):
                                    comment_id = el_id[len('comment-'):]
                                if not comment_id:
                                    comment_id = await el.get_attribute('data-id') or ""
                            except Exception:
                                pass

                            # 用户名
                            username = "未知用户"
                            for usel in ["span.user-name", "a.name", "div.username", "span.nickname", "a.user-nickname", 'a[href*="/user/profile/"]']:
                                try:
                                    uel = el.locator(usel).first
                                    if await uel.count() > 0:
                                        t = await uel.text_content()
                                        if t and t.strip():
                                            username = t.strip()
                                            break
                                except Exception:
                                    continue

                            # 内容
                            content = ""
                            for csel in ["div.content", "p.content", "div.text", "span.content", "div.comment-text"]:
                                try:
                                    cel = el.locator(csel).first
                                    if await cel.count() > 0:
                                        t = await cel.text_content()
                                        if t and t.strip():
                                            content = t.strip()
                                            break
                                except Exception:
                                    continue
                            if not content:
                                try:
                                    full = await el.text_content() or ""
                                    content = full.replace(username, "").strip() if username != "未知用户" else full.strip()
                                except Exception:
                                    pass

                            # 时间
                            time_val = "未知时间"
                            for tsel in ["span.time", "div.time", "span.date", "div.date", "time"]:
                                try:
                                    tel = el.locator(tsel).first
                                    if await tel.count() > 0:
                                        t = await tel.text_content()
                                        if t and t.strip():
                                            time_val = t.strip()
                                            break
                                except Exception:
                                    continue

                            if username != "未知用户" and content and len(content) > 2:
                                comments.append({
                                    "comment_id": comment_id,
                                    "用户名": username,
                                    "内容": content,
                                    "时间": time_val,
                                })
                        except Exception as e:
                            print(f"处理单个评论出错: {e}")
                            continue
                    if comments:
                        break
                except Exception as e:
                    print(f"处理评论选择器出错: {e}")
                    continue

        # 格式化返回结果（包含 comment_id，供 reply_comment/like_comment 使用）
        if comments:
            result = f"共获取到 {len(comments)} 条评论：\n\n"
            for i, comment in enumerate(comments, 1):
                cid = comment['comment_id']
                cid_hint = f"  [comment_id: {cid}]" if cid else ""
                result += f"{i}. {comment['用户名']}（{comment['时间']}）: {comment['内容']}{cid_hint}\n\n"
            return result
        else:
            return "未找到任何评论，可能是帖子没有评论或评论区无法访问。"
    
    except Exception as e:
        return f"获取评论时出错: {str(e)}"

@mcp.tool()
async def post_comment(url: str, comment: str, force: bool = False) -> str:
    """发布评论到指定笔记。
    支持 @mention：
    - @昵称          → picker 昵称匹配（适合唯一昵称）
    - @昵称:hexid    → 先访问用户主页取头像，picker 头像精确匹配（适合同名场景）
      hexid 从 get_user_notes/search_notes 等工具的 URL 末段获取

    Args:
        url: 笔记 URL（支持短链）
        comment: 评论内容，支持 @昵称 或 @昵称:hexid 格式
        force: 该笔记已评论过时默认拦截并提醒；确实要再评论一次时传 true
    """
    _rl = _RateLimit.check('comment')
    if _rl:
        return _rl

    login_status = await ensure_browser()
    if not login_status:
        return "请先登录小红书账号，才能发布评论"

    if not main_page:
        return "浏览器初始化失败，请重试"

    try:
        import re as _re
        # 解析 @昵称:hexid 格式，预加载头像 CDN key
        avatar_map: dict[str, str] = {}
        at_with_id = _re.findall(r'@([^:\s]+):([0-9a-f]{16,32})', comment)
        for nickname, hexid in at_with_id:
            key = await _get_user_avatar_key(main_page, hexid)
            if key:
                avatar_map[nickname] = key
            # 将评论里的 @昵称:hexid 替换成 @昵称（picker 搜索只用昵称）
            comment = comment.replace(f'@{nickname}:{hexid}', f'@{nickname}')
    except Exception:
        avatar_map = {}

    try:
        # 处理URL
        processed_url = process_url(url)
        print(f"处理后的评论URL: {processed_url}")

        # 访问帖子链接（短链会跳转，所以 note_id 必须在跳转后从最终 URL 取）
        await main_page.goto(processed_url, timeout=60000, referer="https://www.xiaohongshu.com/explore")
        await asyncio.sleep(5)  # 等待页面加载
        _remember_token_from_url(main_page.url)
        await _skim(main_page)  # 先看一会儿再评论

        _note_id_match = re.search(r'/(?:explore|discovery/item)/([0-9a-f]{24})', main_page.url) \
            or re.search(r'/explore/([^/?#]+)', processed_url)
        _note_id = _note_id_match.group(1) if _note_id_match else ''
        if not _note_id:
            print(f"[comment_history] 无法解析 note_id: {processed_url} -> {main_page.url}", file=sys.stderr, flush=True)

        # 查历史：已评论过则拦截，除非 force=True
        _history_prefix = ''
        if _note_id:
            _prev = _get_comment_history(_note_id, action_type='commented')
            if _prev:
                _lines = [f"  · {_h['created_at']}  「{_h['content'][:60]}{'…' if len(_h['content']) > 60 else ''}」" for _h in _prev]
                _msg = "你已在该笔记下评论过：\n" + "\n".join(_lines)
                if not force:
                    return f"⚠️ {_msg}\n未发送。确实要再评论一次，请传 force=true。"
                _history_prefix = f"⚠️ {_msg}\n（force=true，本次仍已发送）\n\n"

        # 检查是否加载了错误页面
        if not main_page:  # 添加空检查
            return "浏览器初始化失败，请重试"
            
        error_page = await main_page.evaluate('''
            () => {
                // 检查常见的错误信息
                const errorTexts = [
                    "当前笔记暂时无法浏览",
                    "内容不存在",
                    "页面不存在",
                    "内容已被删除"
                ];
                
                for (const text of errorTexts) {
                    if (document.body.innerText.includes(text)) {
                        return {
                            isError: true,
                            errorText: text
                        };
                    }
                }
                
                return { isError: false };
            }
        ''')
        
        if error_page.get("isError", False):
            return f"无法发布评论: {error_page.get('errorText', '未知错误')}\n请检查链接是否有效或尝试使用带有有效token的完整URL。"
        
        # 定位评论区域并滚动到该区域
        comment_area_found = False
        comment_area_selectors = [
            'text="条评论"',
            'text="共 " >> xpath=..',
            'text=/\\d+ 条评论/',
            'text="评论"',
            'div.comment-container'
        ]
        
        for selector in comment_area_selectors:
            try:
                if not main_page:  # 添加空检查
                    return "浏览器初始化失败，请重试"
                    
                element = await main_page.query_selector(selector)
                if element:
                    await element.scroll_into_view_if_needed()
                    await _rand_sleep(2)
                    comment_area_found = True
                    break
            except Exception as e:
                print(f"定位评论区域时出错: {str(e)}")
                continue
        
        if not comment_area_found:
            # 如果没有找到评论区域，尝试滚动到页面底部
            if not main_page:  # 添加空检查
                return "浏览器初始化失败，请重试"
                
            await main_page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            await _rand_sleep(2)
        
        # 定位评论输入框（简化选择器列表）
        comment_input = None
        input_selectors = [
            'div[contenteditable="true"]',
            'p:has-text("说点什么...")',
            'text="说点什么..."',
            'text="评论发布后所有人都能看到"'
        ]
        
        # 尝试常规选择器
        for selector in input_selectors:
            try:
                if not main_page:  # 添加空检查
                    return "浏览器初始化失败，请重试"
                    
                element = await main_page.query_selector(selector)
                if element and await element.is_visible():
                    await element.scroll_into_view_if_needed()
                    await _rand_sleep(1)
                    comment_input = element
                    break
            except Exception as e:
                print(f"定位评论输入框时出错: {str(e)}")
                continue
        
        # 如果常规选择器失败，使用JavaScript查找
        if not comment_input:
            # 使用更精简的JavaScript查找输入框
            if not main_page:  # 添加空检查
                return "浏览器初始化失败，请重试"
                
            js_result = await main_page.evaluate('''
                () => {
                    // 查找可编辑元素
                    const editableElements = Array.from(document.querySelectorAll('[contenteditable="true"]'));
                    if (editableElements.length > 0) return true;
                    
                    // 查找包含"说点什么"的元素
                    const placeholderElements = Array.from(document.querySelectorAll('*'))
                        .filter(el => el.textContent && el.textContent.includes('说点什么'));
                    return placeholderElements.length > 0;
                }
            ''')
            
            if js_result:
                # 如果JS检测到输入框，尝试点击页面底部
                if not main_page:  # 添加空检查
                    return "浏览器初始化失败，请重试"
                    
                await main_page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                await _rand_sleep(1)
                
                # 尝试再次查找输入框
                for selector in input_selectors:
                    try:
                        if not main_page:  # 添加空检查
                            return "浏览器初始化失败，请重试"
                            
                        element = await main_page.query_selector(selector)
                        if element and await element.is_visible():
                            comment_input = element
                            break
                    except Exception as e:
                        print(f"尝试再次查找输入框时出错: {str(e)}")
                        continue
        
        if not comment_input:
            return "未能找到评论输入框，无法发布评论"
        
        # 输入评论内容
        await _human_click(main_page, comment_input)
        await _rand_sleep(1)
        
        if not main_page:  # 添加空检查
            return "浏览器初始化失败，请重试"
            
        await _ensure_nl_route(main_page)
        await _type_with_at_mention(main_page, comment, avatar_map=avatar_map)
        await _rand_sleep(1)

        # 发送评论（简化发送逻辑）
        send_success = False
        
        # 方法1: 尝试点击发送按钮
        try:
            if not main_page:  # 添加空检查
                return "浏览器初始化失败，请重试"
                
            send_button = await main_page.query_selector('button:has-text("发送")')
            if send_button and await send_button.is_visible():
                await _human_click(main_page, send_button)
                await _rand_sleep(2)
                send_success = True
        except Exception as e:
            print(f"点击发送按钮出错: {str(e)}")
        
        # 方法2: 如果方法1失败，尝试使用Enter键
        if not send_success:
            try:
                if not main_page:  # 添加空检查
                    return "浏览器初始化失败，请重试"
                    
                await main_page.keyboard.press("Enter")
                await _rand_sleep(2)
                send_success = True
            except Exception as e:
                print(f"使用Enter键发送出错: {str(e)}")
        
        # 方法3: 如果方法2失败，尝试使用JavaScript点击发送按钮
        if not send_success:
            try:
                if not main_page:  # 添加空检查
                    return "浏览器初始化失败，请重试"
                    
                js_send_result = await main_page.evaluate('''
                    () => {
                        const sendButtons = Array.from(document.querySelectorAll('button'))
                            .filter(btn => btn.textContent && btn.textContent.includes('发送'));
                        if (sendButtons.length > 0) {
                            sendButtons[0].click();
                            return true;
                        }
                        return false;
                    }
                ''')
                await _rand_sleep(2)
                send_success = js_send_result
            except Exception as e:
                print(f"使用JavaScript点击发送按钮出错: {str(e)}")
        
        if send_success:
            _warn = ''
            _risk = await _detect_risk(main_page)
            if _risk:
                _RateLimit.trip(_risk)
                return f"🛑 发送后检测到风控（{_risk}），已熔断。评论可能没发出去，请到浏览器里确认。"
            if _note_id:
                _record_comment('commented', _note_id, content=comment)
            else:
                _warn = "\n⚠️ 未能解析笔记 ID，这条评论没有写入历史，请用 add_comment_history 手动补录。"
            _RateLimit.record('comment')
            try:  # 发送后输入框应已清空，还有残留说明只发出去了一部分或没发出去
                _left = await main_page.evaluate(
                    "() => { const e=document.querySelector('[contenteditable=\"true\"]#content-textarea')||document.querySelector('#content-textarea')||document.querySelector('div[contenteditable=\"true\"]'); return e? (e.innerText||e.value||'').trim():''; }")
                if _left:
                    _warn += f"\n⚠️ 发送后输入框里还剩内容（{_left[:30]}…），可能只发出去一部分，请到浏览器确认。"
            except Exception:
                pass
            return f"{_history_prefix}已成功发布评论：{comment}{_warn}"
        else:
            return f"发布评论失败，请检查评论内容或网络连接"

    except Exception as e:
        return f"发布评论时出错: {str(e)}"

@mcp.tool()
def get_note_images(url: str) -> dict:
    """仅获取笔记图片（不含正文）。通常不需要单独调用——get_note_content 已内置图片处理。
    适用场景：只需要图片、不需要正文时。不需要登录，通过手机 UA 直接解析页面数据。

    Args:
        url: 笔记 URL（支持短链 xhslink.cn 和完整链接）

    Returns:
        dict: 包含笔记基本信息和本地图片路径列表
    """
    _rl = _RateLimit.check('image_fetch')
    if _rl:
        return {"error": _rl}
    _RateLimit.record('image_fetch')

    # 手机 UA，不用这个拿不到 __INITIAL_STATE__
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                      "Mobile/15E148 Safari/604.1",
        "Referer": "https://www.xiaohongshu.com/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    try:
        # 跟随短链跳转
        resp = requests.get(url.strip(), headers=headers, timeout=15, allow_redirects=True)
        resp.raise_for_status()
        html = resp.text

        # 提取 __INITIAL_STATE__
        match = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.+?\});?\s*</script', html, re.S)
        if not match:
            # 有时 unicode 转义
            html_unescaped = html.replace('\\u002F', '/').replace('\\u0026', '&')
            match = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.+?\});?\s*</script', html_unescaped, re.S)

        if not match:
            return {"error": "未找到 __INITIAL_STATE__，页面结构可能已变更或该链接需要登录"}

        raw = match.group(1)
        # 修复 undefined → null
        raw = re.sub(r'\bundefined\b', 'null', raw)
        raw = raw.replace('\\u002F', '/').replace('\\u0026', '&')

        state = json.loads(raw)

        # 兼容两种路径
        note_data = None
        try:
            note_data = state['noteData']['data']['noteData']
        except (KeyError, TypeError):
            pass
        if not note_data:
            try:
                note_data = state['noteData']['normalNotePreloadData']['noteData']
            except (KeyError, TypeError):
                pass

        if not note_data:
            return {"error": "无法从 __INITIAL_STATE__ 中解析笔记数据，路径可能已变更"}

        # 基本信息
        title = note_data.get('title', '')
        desc = note_data.get('desc', '')
        author = note_data.get('user', {}).get('nickname', '未知作者')

        # 图片 URL 列表
        image_list = note_data.get('imageList', [])
        image_urls = []
        for img in image_list:
            img_url = img.get('urlDefault') or img.get('url') or img.get('infoList', [{}])[0].get('url', '')
            if img_url:
                if img_url.startswith('//'):
                    img_url = 'https:' + img_url
                image_urls.append(img_url)

        if not image_urls:
            return {
                "title": title,
                "author": author,
                "desc": desc,
                "image_count": 0,
                "image_paths": [],
                "message": "该笔记没有图片（可能是纯文字或视频笔记）"
            }

        # 下载图片到本地临时目录（每次调用独立目录，避免多次调用互相覆盖）
        save_dir = tempfile.mkdtemp(prefix='xhs_images_')

        img_headers = {**headers, "Referer": "https://www.xiaohongshu.com/"}
        saved_paths = []

        for i, img_url in enumerate(image_urls):
            try:
                img_resp = requests.get(img_url, headers=img_headers, timeout=15)
                img_resp.raise_for_status()
                # 判断后缀
                content_type = img_resp.headers.get('Content-Type', 'image/jpeg')
                ext = 'jpg' if 'jpeg' in content_type else content_type.split('/')[-1].split(';')[0]
                fname = os.path.join(save_dir, f'xhs_{i+1}.{ext}')
                with open(fname, 'wb') as f:
                    f.write(img_resp.content)
                saved_paths.append(fname)
            except Exception as e:
                saved_paths.append(f"下载失败: {img_url} ({e})")

        return {
            "title": title,
            "author": author,
            "desc": desc,
            "image_count": len(image_urls),
            "image_paths": saved_paths,
            "message": f"共 {len(saved_paths)} 张图片已保存到本地，路径见 image_paths"
        }

    except requests.exceptions.RequestException as e:
        return {"error": f"网络请求失败: {e}"}
    except json.JSONDecodeError as e:
        return {"error": f"JSON 解析失败: {e}"}
    except Exception as e:
        return {"error": f"未知错误: {e}"}


@mcp.tool()
async def get_notifications(tab: str = "comments", limit: int = 20, only_unreplied: bool = False, verbose: bool = False) -> str:
    """获取小红书通知（评论@、赞和收藏、新增关注）
    评论通知会自动对照回复记录，回过的标"✅ 已回复"。想省 token 时用 only_unreplied=True 只看没回的。

    Args:
        tab: 通知类型，可选 "comments"（评论和@）、"likes"（赞和收藏）、"follows"（新增关注）。默认 "comments"。
        limit: 最多返回条数，默认 20。
        only_unreplied: 只返回还没回复过的评论通知（仅 comments 有效），默认 False。
        verbose: 评论通知默认用精简格式（按笔记分组、不带链接和 token，回复/点赞时 token 会自动补）；
            True 时输出带链接、token、被回复评论 ID 的完整格式。
    """
    login_status = await ensure_browser()
    if not login_status:
        return "请先登录小红书账号"
    if not browser_context:
        return "浏览器上下文未初始化"

    tab_map = {
        "comments": ("评论和@",     "/you/mentions"),
        "likes":    ("赞和收藏",    "/you/likes"),
        "follows":  ("新增关注",    "/you/connections"),
    }
    if tab not in tab_map:
        return f"不支持的通知类型：{tab}，请使用 comments/likes/follows"

    tab_label, api_path = tab_map[tab]
    captured = {}

    try:
        global notifications_page

        # 复用已有通知标签页；若已关闭或崩溃则重新开一个
        if notifications_page is not None:
            try:
                _ = notifications_page.url
            except Exception:
                notifications_page = None

        if notifications_page is None:
            notifications_page = await browser_context.new_page()
            notifications_page.set_default_timeout(30000)

        page = notifications_page

        async def on_response(resp):
            if api_path in resp.url:
                try:
                    captured['data'] = await resp.json()
                except Exception:
                    pass

        page.on('response', on_response)

        try:
            await page.goto("https://www.xiaohongshu.com/notification", timeout=30000)
            await _rand_sleep(2)

            # 点击对应 tab（如果不是默认的评论 tab）
            if tab != "comments":
                try:
                    btn = page.locator(f'text="{tab_label}"').first
                    if await btn.is_visible():
                        await btn.click()
                        await _rand_sleep(2)
                except Exception:
                    pass
            else:
                await _rand_sleep(2)

            # 模拟人在看通知，停留一会儿再处理数据
            await _rand_sleep(3)
        finally:
            # 无论成功失败都移除监听器，避免泄漏
            page.remove_listener('response', on_response)

        # 标签页保持打开，不关闭

        if not captured.get('data'):
            return f"暂无{tab_label}通知（或接口未响应）"

        msgs = captured['data'].get('data', {}).get('message_list', [])
        has_more = captured['data'].get('data', {}).get('has_more', False)

        if not msgs:
            return f"暂无{tab_label}通知"

        msgs = msgs[:limit]

        # 自动对照回复记录：comment_id -> 回复时间
        _replied = {}
        try:
            _c = sqlite3.connect(ACTIONS_DB)
            for _cid, _at in _c.execute("SELECT comment_id, created_at FROM comment_history WHERE action_type='replied' AND comment_id IS NOT NULL AND comment_id != ''"):
                _replied[_cid] = str(_at)[:16].replace('T', ' ')
            _c.close()
        except Exception as e:
            print(f"[notifications] 读回复记录失败: {e}", file=sys.stderr, flush=True)
        _cid_of = lambda m: (m.get('comment_info') or {}).get('id', '')
        _n_replied = sum(1 for m in msgs if _cid_of(m) in _replied) if tab == "comments" else 0
        if tab == "comments" and only_unreplied:
            msgs = [m for m in msgs if _cid_of(m) not in _replied]
            if not msgs:
                return f"📬 {tab_label}通知：最近 {_n_replied} 条都已回复过，没有待回的。"

        result = f"📬 {tab_label}通知（{len(msgs)} 条{'，还有更多' if has_more else ''}"
        if tab == "comments":
            result += f"，已回复 {_n_replied} 条" if not only_unreplied else f"，只显示未回复的，另有 {_n_replied} 条已回复"
        result += "）：\n\n"

        if tab == "comments" and not verbose:
            groups = {}  # note_id -> {'title':..., 'items': [...]}
            for msg in msgs:
                note = msg.get('item_info') or {}
                nid = note.get('id', '')
                _remember_note_token(nid, note.get('xsec_token', ''))
                g = groups.setdefault(nid, {'title': (note.get('content') or note.get('display_title') or note.get('displayTitle') or '')[:20], 'items': []})
                g['items'].append(msg)
            out = [result.rstrip('\n').rstrip('：') + "（回复/点赞时 xsec_token 会自动补，不用传）"]
            _my_nick = os.environ.get('XHS_MY_NICK', '')  # 被回复的评论若是自己的，显示"我"
            for nid, g in groups.items():
                out.append(f"\n📝 {g['title'] or '（无标题）'}  note_id: {nid or '?'}")
                for msg in g['items']:
                    nick = (msg.get('user_info') or {}).get('nickname', '?')
                    ci = msg.get('comment_info') or {}
                    text = ci.get('content') or ci.get('note_text') or ci.get('text') or ''
                    cid = ci.get('id', '')
                    act = msg.get('title', '')
                    ts = msg.get('time', 0)
                    try:
                        t = datetime.fromtimestamp(int(ts)).strftime('%m-%d %H:%M') if ts else ''
                    except Exception:
                        t = ''
                    if cid in _replied:
                        short = text[:20] + ('…' if len(text) > 20 else '')
                        out.append(f"  ✅ {nick}：{short}")
                    else:
                        kind = '@' if '@' in act or '提到' in act else ''
                        line = f"  💬 {nick}{kind} {t}：{text}"
                        _tgt = (ci.get('target_comment') or {})
                        _tgt_text = (_tgt.get('content') or '').replace('\n', ' ')
                        if _tgt_text:
                            _who = (_tgt.get('user_info') or {}).get('nickname', '')
                            _who = '我' if _who == (_my_nick or _who) else _who
                            line += f"\n     ↪ 回的是{_who}：{_tgt_text[:60]}{'…' if len(_tgt_text) > 60 else ''}"
                        if cid:
                            line += f"\n     comment_id: {cid}"
                        out.append(line)
            return "\n".join(out)

        for i, msg in enumerate(msgs, 1):
            user = msg.get('user_info') or msg.get('user') or {}
            nickname = user.get('nickname', '未知用户')
            indicator = user.get('indicator', '')  # "作者"/"粉丝" 等身份标注
            title = msg.get('title', '')           # 动作描述，如"回复了你的评论"
            ts = msg.get('time', 0)
            try:
                time_str = datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d %H:%M') if ts else ''
            except Exception:
                time_str = str(ts)
            comment_info = msg.get('comment_info') or {}
            comment = comment_info.get('content') or comment_info.get('note_text') or comment_info.get('text') or ''
            note = msg.get('item_info') or {}
            note_content = note.get('content') or note.get('display_title') or note.get('displayTitle') or ''
            note_id = note.get('id', '')
            note_token = note.get('xsec_token', '')
            _remember_note_token(note_id, note_token)
            note_url = f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else ''
            if note_url and note_token:
                note_url += f"?xsec_token={note_token}&xsec_source=pc_notice"
            this_cid = comment_info.get('id', '')
            target_cid = (comment_info.get('target_comment') or {}).get('id', '')

            line = f"{i}. **{nickname}**"
            if indicator:
                line += f"（{indicator}）"
            line += f" {title}"
            if time_str:
                line += f"  {time_str}"
            if comment:
                line += f"\n   💬 {comment}"
            if note_content:
                line += f"\n   📝 笔记：{note_content[:50]}{'…' if len(note_content) > 50 else ''}"
            if note_url:
                line += f"\n   🔗 {note_url}"
            if note_id:
                line += f"\n   🆔 note_id: {note_id}"
                if note_token:
                    line += f"  xsec_token: {note_token}"
            if this_cid and this_cid in _replied:
                line += f"\n   ✅ 已回复（{_replied[this_cid]}），comment_id: {this_cid}"
            elif this_cid:
                line += f"\n   💬 comment_id（可直接 reply_comment 回复这条）: {this_cid}"
            if target_cid:
                line += f"\n   ↩️ 对方回复的是你的评论 comment_id: {target_cid}"
            result += line + "\n\n"

        return result

    except Exception as e:
        return f"获取通知失败：{e}"


@mcp.tool()
async def search_user(keyword: str) -> str:
    """搜索小红书用户，返回匹配用户的昵称、hex ID 和 xsec_token。

    支持按小红书号（纯数字）或昵称搜索。小红书号搜索结果精确唯一；
    昵称搜索可能返回多个同名用户，需人工确认。

    Args:
        keyword: 小红书号（如 1103700607）或用户昵称
    """
    _rl = _RateLimit.check('search')
    if _rl:
        return _rl

    login_status = await ensure_browser()
    if not login_status:
        return "请先登录小红书账号"
    if not main_page:
        return "浏览器初始化失败，请重试"

    is_number = keyword.isdigit()
    try:
        search_url = f"https://www.xiaohongshu.com/search_result_ai?keyword={quote(keyword)}&source=web_explore_feed"
        await main_page.goto(search_url, timeout=30000)
        try:
            await main_page.wait_for_function("window.__INITIAL_STATE__ !== undefined", timeout=15000)
        except Exception:
            pass
        await _rand_sleep(1.5)

        # 点击"用户"tab，等结果切换
        clicked = await main_page.evaluate('''() => {
            const tabs = Array.from(document.querySelectorAll(
                '[class*="tab"], [class*="filter"] span, nav a, .search-tab, ul.tabs li'
            ));
            const userTab = tabs.find(el =>
                el.innerText?.trim() === '用户' || el.textContent?.trim() === '用户'
            );
            if (userTab) { userTab.click(); return true; }
            return false;
        }''')
        if clicked:
            await _rand_sleep(2)  # 等用户结果渲染

        # 只在用户搜索容器内找链接，优先找 __INITIAL_STATE__
        results = await main_page.evaluate('''() => {
            const found = [];
            const seen = new Set();

            // 尝试从 __INITIAL_STATE__ 拿干净的用户数据
            try {
                const state = window.__INITIAL_STATE__;
                const items = state?.search?.result?.items
                    || state?.searchResult?.items
                    || [];
                for (const item of items) {
                    const u = item?.user || item?.userInfo || item;
                    const hexId = u?.userId || u?.id || '';
                    if (!/^[0-9a-f]{20,}$/.test(hexId) || seen.has(hexId)) continue;
                    seen.add(hexId);
                    found.push({
                        hex_id: hexId,
                        token: u?.xsecToken || '',
                        nickname: u?.nickname || u?.name || ''
                    });
                }
                if (found.length) return found;
            } catch(e) {}

            // fallback: 只找"用户"tab容器内的卡片（class 含 user-item / user-card 等）
            // 跳过导航栏、自身账号区域
            const links = Array.from(document.querySelectorAll('a[href*="/user/profile/"]'));
            for (const a of links) {
                if (a.closest('header, nav, .nav, .sidebar, .side-bar, .login-btn, .reds-count, .user-info-wrapper')) continue;
                const href = a.getAttribute('href') || '';
                const m = href.match(/\/user\/profile\/([0-9a-f]{20,})/);
                if (!m || seen.has(m[1])) continue;
                seen.add(m[1]);
                const url = new URL(href, location.href);
                const card = a.closest('section, li, [class*="user-item"], [class*="user-card"]') || a.parentElement;
                // 只取直接子元素中第一个纯文字节点作昵称，避免混入日期
                const nameEl = card?.querySelector('[class*="name"]:not([class*="count"]):not([class*="date"]), [class*="nick"]');
                const nickname = nameEl?.childNodes[0]?.textContent?.trim() || nameEl?.innerText?.split('\\n')[0]?.trim() || '';
                found.push({
                    hex_id: m[1],
                    token: url.searchParams.get('xsec_token') || '',
                    nickname
                });
            }
            return found;
        }''')

        if not results:
            return f"未找到用户「{keyword}」，请确认小红书号或昵称是否正确"

        # 把所有搜到的用户 token 存入缓存，get_user_notes 自动使用
        for u in results:
            if u['hex_id'] and u['token']:
                _user_token_cache[u['hex_id']] = u['token']

        # 数字小红书号是唯一的，直接取第一个
        if is_number:
            u = results[0]
            lines = [
                f"找到用户：",
                f"昵称：{u['nickname'] or '(未获取)'}",
                f"hex ID：{u['hex_id']}",
                f"主页：https://www.xiaohongshu.com/user/profile/{u['hex_id']}",
                "",
                "直接用 hex ID 调用 get_user_notes，token 已自动缓存。"
            ]
            return "\n".join(lines)

        lines = [f"搜索「{keyword}」，找到 {len(results)} 个用户：\n"]
        for i, u in enumerate(results, 1):
            lines.append(f"{i}. 昵称：{u['nickname'] or '(未获取)'}")
            lines.append(f"   hex ID：{u['hex_id']}")
            lines.append(f"   主页：https://www.xiaohongshu.com/user/profile/{u['hex_id']}")
            lines.append("")
        lines.append("用 hex ID 调用 get_user_notes，token 已自动缓存。")
        _RateLimit.record('search')
        return "\n".join(lines)

    except Exception as e:
        return f"搜索用户失败：{str(e)}"


@mcp.tool()
async def get_user_notes(user_id: str, xsec_token: str = "", limit: int = 20) -> str:
    """获取指定用户主页的笔记列表。

    Args:
        user_id: 小红书号（纯数字，自动搜索）或用户 hex ID（如 5f1a2b3c0000000001000abc）。
        xsec_token: 一般不需要传，传小红书号时自动获取，传 hex ID 时从缓存取。
        limit: 最多返回笔记数，默认 20
    """
    login_status = await ensure_browser()
    if not login_status:
        return "请先登录小红书账号"
    if not main_page:
        return "浏览器初始化失败，请重试"

    hex_id = user_id
    token = xsec_token

    try:
        # 纯数字小红书号：自动搜索拿 hex ID 和 token
        if user_id.isdigit():
            search_url = f"https://www.xiaohongshu.com/search_result_ai?keyword={quote(user_id)}&source=web_explore_feed"
            await main_page.goto(search_url, timeout=30000)
            try:
                await main_page.wait_for_function("window.__INITIAL_STATE__ !== undefined", timeout=15000)
            except Exception:
                pass
            await _rand_sleep(1.5)
            await main_page.evaluate('''() => {
                const tabs = Array.from(document.querySelectorAll('[class*="tab"], [class*="filter"] span, nav a, ul.tabs li'));
                const t = tabs.find(el => el.innerText?.trim() === "用户" || el.textContent?.trim() === "用户");
                if (t) t.click();
            }''')
            await _rand_sleep(2)
            found = await main_page.evaluate('''() => {
                const links = Array.from(document.querySelectorAll('a[href*="/user/profile/"]'));
                for (const a of links) {
                    if (a.closest('header, nav, .nav, .sidebar, .side-bar, .login-btn, .reds-count, .user-info-wrapper')) continue;
                    const m = (a.getAttribute('href') || '').match(/\/user\/profile\/([0-9a-f]{20,})/);
                    if (!m) continue;
                    const url = new URL(a.getAttribute('href'), location.href);
                    return { hex_id: m[1], token: url.searchParams.get('xsec_token') || '' };
                }
                return null;
            }''')
            if not found:
                return f"无法通过小红书号 {user_id} 找到对应用户"
            hex_id = found['hex_id']
            token = found['token']
            if token:
                _user_token_cache[hex_id] = token
        else:
            # hex ID：从缓存取 token
            token = token or _user_token_cache.get(hex_id, "")

        # 访问 profile 页，必须带 xsec_token 否则会被拦截
        profile_url = f"https://www.xiaohongshu.com/user/profile/{hex_id}"
        if token:
            profile_url += f"?xsec_token={token}&xsec_source=pc_search"

        await main_page.goto(profile_url, timeout=30000)
        await asyncio.sleep(4)

        # 检查是否被拦截
        title = await main_page.title()
        if "安全限制" in title or "未连接" in title:
            return f"访问被拦截（{title}），请检查 xsec_token 是否有效"

        # 提取用户信息和笔记——直接读 DOM（比穿透 Vue Proxy 更可靠）
        data = await main_page.evaluate(f'''() => {{
            const result = {{ nickname: "", desc: "", fans: "", notes: [] }};

            // 用户昵称
            const nameEl = document.querySelector('.user-nickname') ||
                           document.querySelector('[class*="user-name"]') ||
                           document.querySelector('.info .name');
            if (nameEl) result.nickname = nameEl.innerText.trim();

            // 用户简介
            const descEl = document.querySelector('.user-desc') ||
                           document.querySelector('[class*="desc"]');
            if (descEl) result.desc = descEl.innerText.trim().slice(0, 100);

            // 粉丝数（从互动数字里找）
            const countEls = document.querySelectorAll('[class*="count"]');
            for (const el of countEls) {{
                const label = el.closest('[class*="item"]')?.querySelector('[class*="label"]')?.innerText || '';
                if (label.includes('粉丝')) {{ result.fans = el.innerText.trim(); break; }}
            }}

            // 笔记列表：用教程验证过的选择器
            const noteItems = document.querySelectorAll('[class*="note-item"]');
            const seen = new Set();
            noteItems.forEach(el => {{
                if (result.notes.length >= {limit}) return;
                const titleEl = el.querySelector('.footer a.title span') ||
                                el.querySelector('a.title span') ||
                                el.querySelector('[class*="title"] span') ||
                                el.querySelector('[class*="title"]');
                const coverEl = el.querySelector('a.cover') ||
                                el.querySelector('a[href*="/explore/"]');
                const likeEl  = el.querySelector('[class*="like-wrapper"] span') ||
                                el.querySelector('[class*="count"]');
                const topEl   = el.querySelector('.top-wrapper');

                const href = coverEl?.getAttribute('href') || '';
                if (!href || seen.has(href)) return;
                seen.add(href);

                const fullUrl = href.startsWith('http') ? href :
                                'https://www.xiaohongshu.com' + href;
                result.notes.push({{
                    title: titleEl?.innerText?.trim() || '无标题',
                    url:   fullUrl,
                    likes: likeEl?.innerText?.trim() || '',
                    top:   !!topEl,
                }});
            }});

            // 兜底：页面文字片段（诊断用）
            if (!result.nickname && result.notes.length === 0) {{
                result.raw_text = document.body.innerText.slice(0, 1000);
            }}
            return result;
        }}''')

        # 如果首屏没拿到，滚动一次再试
        if not data.get('notes'):
            await main_page.evaluate("window.scrollBy(0, 600)")
            await _rand_sleep(2)
            data = await main_page.evaluate(f'''() => {{
                const notes = [];
                const seen = new Set();
                document.querySelectorAll('[class*="note-item"]').forEach(el => {{
                    if (notes.length >= {limit}) return;
                    const titleEl = el.querySelector('.footer a.title span') ||
                                    el.querySelector('a.title span') ||
                                    el.querySelector('[class*="title"]');
                    const coverEl = el.querySelector('a.cover') ||
                                    el.querySelector('a[href*="/explore/"]');
                    const href = coverEl?.getAttribute('href') || '';
                    if (!href || seen.has(href)) return;
                    seen.add(href);
                    notes.push({{
                        title: titleEl?.innerText?.trim() || '无标题',
                        url: href.startsWith('http') ? href : 'https://www.xiaohongshu.com' + href,
                        likes: '',
                    }});
                }});
                return {{ notes, raw_text: notes.length === 0 ? document.body.innerText.slice(0, 1000) : '' }};
            }}''')

        # 格式化输出
        parts = []
        if data.get('nickname'):
            parts.append(f"👤 {data['nickname']}")
        if data.get('desc'):
            parts.append(f"📝 {data['desc']}")
        if data.get('fans'):
            parts.append(f"👥 粉丝 {data['fans']}")
        parts.append(f"🔗 {profile_url}\n")

        notes = data.get('notes', [])
        if notes:
            parts.append(f"📒 笔记列表（共 {len(notes)} 条）：\n")
            for i, note in enumerate(notes, 1):
                line = f"{i}. {'📌 ' if note.get('top') else ''}{note['title']}"
                if note.get('likes'):
                    line += f"  ❤️{note['likes']}"
                line += f"\n   {note['url']}"
                parts.append(line)
            return "\n".join(parts)
        elif data.get('raw_text'):
            parts.append(f"\n⚠️ 未找到笔记，页面内容：\n{data['raw_text']}")
            return "\n".join(parts)
        else:
            return f"未能获取用户笔记（hex_id={hex_id}），可能收藏不可见或页面结构变化"

    except Exception as e:
        return f"获取用户笔记失败：{str(e)}"


# ─── 首页 Feed / 我的笔记 ─────────────────────────────────────────────────────────

@mcp.tool()
async def list_feeds(limit: int = 20, verbose: bool = False) -> str:
    """获取小红书首页推荐 feed 流。

    Args:
        limit: 返回条数，默认 20，最多 100
        verbose: False（默认）只返回标题、作者、点赞、链接；True 额外返回 xsecToken
    """
    login_status = await ensure_browser()
    if not login_status:
        return "请先登录小红书账号"
    if not main_page:
        return "浏览器初始化失败，请重试"

    target = min(limit, 100)
    try:
        await main_page.goto("https://www.xiaohongshu.com/explore", timeout=30000)
        try:
            await main_page.wait_for_function("window.__INITIAL_STATE__ !== undefined", timeout=20000)
        except Exception:
            pass
        await _rand_sleep(2)

        raw = await main_page.evaluate("""() => {
            const state = window.__INITIAL_STATE__;
            if (!state?.feed?.feeds) return '[]';
            const feeds = state.feed.feeds;
            const data = feeds.value !== undefined ? feeds.value : feeds._value;
            return data ? JSON.stringify(data) : '[]';
        }""")
        feeds = json.loads(raw) if raw else []

        items = []
        for item in feeds[:target]:
            note_card = item.get('noteCard') or item.get('note_card') or {}
            user = note_card.get('user') or {}
            interact = note_card.get('interactInfo') or note_card.get('interact_info') or {}
            _remember_note_token(item.get('id', ''), item.get('xsec_token') or item.get('xsecToken') or '')
            items.append({
                'id': item.get('id', ''),
                'xsecToken': item.get('xsec_token') or item.get('xsecToken') or '',
                'title': note_card.get('displayTitle') or note_card.get('display_title') or note_card.get('title') or '',
                'type': note_card.get('type') or 'normal',
                'author': user.get('nickname') or '',
                'likes': interact.get('likedCount') or interact.get('liked_count') or '0',
            })

        if not items:
            return "首页 feed 为空，可能需要重新登录"

        result = f"首页推荐（{len(items)} 条）：\n\n"
        for i, item in enumerate(items, 1):
            if verbose:
                url = f"https://www.xiaohongshu.com/explore/{item['id']}?xsec_token={item['xsecToken']}&xsec_source=pc_homefeed"
                result += (
                    f"{i}. 【{item['title'] or '无标题'}】  {item['type']}\n"
                    f"   作者: {item['author']}  ❤️{item['likes']}\n"
                    f"   xsecToken: {item['xsecToken']}\n"
                    f"   {url}\n\n"
                )
            else:
                url = f"https://www.xiaohongshu.com/explore/{item['id']}"
                result += (
                    f"{i}. 【{item['title'] or '无标题'}】  {item['author']}  ❤️{item['likes']}\n"
                    f"   {url}\n\n"
                )
        return result

    except Exception as e:
        return f"获取首页 feed 失败：{e}"


@mcp.tool()
async def get_my_notes(limit: int = 50) -> str:
    """获取自己已发布的笔记列表。返回链接末段即为 note_id，可直接用于 delete_note。

    Args:
        limit: 最多返回条数，默认 50
    """
    login_status = await ensure_browser()
    if not login_status:
        return "请先登录小红书账号"
    if not browser_context:
        return "浏览器上下文未初始化"

    CREATOR_API = "creator.xiaohongshu.com/api/galaxy/v2/creator/note/user/posted"
    all_notes = []
    seen_ids = set()

    try:
        page = await browser_context.new_page()
        page.set_default_timeout(30000)

        # 监听 API 响应
        async def on_response(response):
            if CREATOR_API not in response.url:
                return
            try:
                data = await response.json()
                if data.get('success') and data.get('data', {}).get('notes'):
                    for n in data['data']['notes']:
                        nid = n.get('note_id') or n.get('noteId') or n.get('id') or ''
                        if nid and nid not in seen_ids:
                            seen_ids.add(nid)
                            info = n.get('interact_info') or n.get('interactInfo') or {}
                            likes = (
                                n.get('likes') or
                                info.get('liked_count') or info.get('likedCount') or
                                info.get('like_count') or info.get('likeCount') or
                                n.get('liked_count') or n.get('like_count') or '0'
                            )
                            xsec = (
                                n.get('xsec_token') or n.get('xsecToken') or
                                n.get('sec_token') or ''
                            )
                            _remember_note_token(nid, xsec)  # 之后只给 note_id 也能打开这条笔记
                            tab_status = n.get('tab_status')
                            status_label = {0: '草稿', 1: '已发布', 2: '审核中', 3: '违规下架'}.get(tab_status, str(tab_status) if tab_status is not None else '')
                            all_notes.append({
                                'id': nid,
                                'title': n.get('display_title') or n.get('displayTitle') or n.get('title') or '',
                                'type': n.get('type') or 'normal',
                                'likes': str(likes) if likes else '0',
                                'views': str(n.get('view_count') or '0'),
                                'comments': str(n.get('comments_count') or '0'),
                                'collects': str(n.get('collected_count') or '0'),
                                'xsec_token': xsec,
                                'status': status_label,
                                'time': n.get('last_update_time') or n.get('lastUpdateTime') or '',
                            })
            except Exception:
                pass

        page.on('response', on_response)

        await page.goto(
            "https://creator.xiaohongshu.com/new/note-manager?source=official",
            timeout=30000
        )

        title = await page.title()
        if "登录" in title or "login" in title.lower():
            await page.close()
            return "创作者中心需要登录，请先在浏览器中登录"

        # 等待首次 API 响应
        try:
            await page.wait_for_response(
                lambda r: CREATOR_API in r.url,
                timeout=15000
            )
        except Exception:
            pass
        await _rand_sleep(2)

        # 滚动加载更多
        no_new = 0
        while len(all_notes) < limit and no_new < 3:
            prev = len(all_notes)
            await page.evaluate("""() => {
                const container = document.querySelector('.note-list, .content-container, main');
                if (container) container.scrollBy(0, 500);
                else window.scrollBy(0, 500);
            }""")
            await _rand_sleep(1.5)
            if len(all_notes) == prev:
                no_new += 1
            else:
                no_new = 0

        await page.close()

        if not all_notes:
            return "未获取到已发布笔记（可能账号没有发布内容，或创作者中心登录状态不同）"

        notes = all_notes[:limit]
        result = f"已发布笔记（共 {len(notes)} 条）：\n\n"
        for i, n in enumerate(notes, 1):
            url = f"https://www.xiaohongshu.com/explore/{n['id']}"
            if n.get('xsec_token'):
                url += f"?xsec_token={n['xsec_token']}&xsec_source=pc_user"
            result += (
                f"{i}. 【{n['title'] or '无标题'}】  {n['type']}  {n['status']}\n"
                f"   ❤️{n['likes']}  💬{n['comments']}  ⭐{n['collects']}  👁{n['views']}\n"
                f"   {url}\n\n"
            )
        return result

    except Exception as e:
        return f"获取已发布笔记失败：{e}"


# ─── 点赞 / 取消点赞 ────────────────────────────────────────────────────────────

async def _navigate_note(note_id: str, xsec_token: str = "") -> tuple:
    """打开笔记页面，返回 (page, error_str)。page 用完后调用方负责关闭。"""
    login_status = await ensure_browser()
    if not login_status:
        return None, "请先登录小红书账号"
    if not browser_context:
        return None, "浏览器上下文未初始化"

    page = await browser_context.new_page()
    await asyncio.sleep(_random.uniform(0.5, 1.5))
    page.set_default_timeout(30000)

    url = f"https://www.xiaohongshu.com/explore/{note_id}"
    xsec_token = xsec_token or _note_token_cache.get(note_id, '')
    if xsec_token:
        url += f"?xsec_token={xsec_token}&xsec_source=pc_feed"
    await page.goto(url, timeout=30000, referer="https://www.xiaohongshu.com/explore")

    title = await page.title()
    if "安全限制" in title:
        await page.close()
        return None, f"访问被拦截：{title}"

    try:
        await page.wait_for_function("window.__INITIAL_STATE__ !== undefined", timeout=15000)
    except Exception:
        pass
    await _rand_sleep(1.5)
    return page, None


@mcp.tool()
async def like_note(note_id: str, xsec_token: str = "", unlike: bool = False) -> str:
    """点赞或取消点赞一篇笔记。note_id 和 xsec_token 从 search_notes(verbose=True) 或 list_feeds(verbose=True) 获取。

    Args:
        note_id: 笔记 ID（从搜索结果或 feed 获取）
        xsec_token: 笔记的 xsec_token（从搜索结果或 feed 获取，可留空）
        unlike: True 为取消点赞，默认 False（点赞）
    """
    _rl = _RateLimit.check('like')
    if _rl:
        return _rl

    page, err = await _navigate_note(note_id, xsec_token)
    if err:
        return err

    try:
        action = "取消点赞" if unlike else "点赞"

        # 读当前状态
        is_liked = await page.evaluate("""() => {
            const state = window.__INITIAL_STATE__;
            const map = state?.note?.noteDetailMap;
            if (map) {
                const key = Object.keys(map)[0];
                return map[key]?.note?.interactInfo?.liked || false;
            }
            return false;
        }""")

        if (unlike and not is_liked) or (not unlike and is_liked):
            return f"无需操作：笔记当前{'已点赞' if is_liked else '未点赞'}"

        # 点击按钮
        like_btn = await page.query_selector(
            ".interact-container .left .like-wrapper, .engage-bar .like-wrapper"
        )
        if not like_btn:
            return f"{action}失败：找不到点赞按钮"

        await _human_click(page, like_btn)
        await _rand_sleep(0.8)
        _RateLimit.record('like')
        return f"✅ {action}成功（笔记 {note_id}）"

    except Exception as e:
        return f"{action if 'action' in dir() else '操作'}失败：{e}"
    finally:
        await asyncio.sleep(_random.uniform(0.5, 1.2))
        await page.close()


@mcp.tool()
async def favorite_note(note_id: str, xsec_token: str = "", unfavorite: bool = False) -> str:
    """收藏或取消收藏一篇笔记。note_id 和 xsec_token 从 search_notes(verbose=True) 或 list_feeds(verbose=True) 获取。

    Args:
        note_id: 笔记 ID（从搜索结果或 feed 获取）
        xsec_token: 笔记的 xsec_token（从搜索结果或 feed 获取，可留空）
        unfavorite: True 为取消收藏，默认 False（收藏）
    """
    _rl = _RateLimit.check('like')
    if _rl:
        return _rl

    page, err = await _navigate_note(note_id, xsec_token)
    if err:
        return err

    try:
        action = "取消收藏" if unfavorite else "收藏"

        is_collected = await page.evaluate("""() => {
            const state = window.__INITIAL_STATE__;
            const map = state?.note?.noteDetailMap;
            if (map) {
                const key = Object.keys(map)[0];
                return map[key]?.note?.interactInfo?.collected || false;
            }
            return false;
        }""")

        if (unfavorite and not is_collected) or (not unfavorite and is_collected):
            return f"无需操作：笔记当前{'已收藏' if is_collected else '未收藏'}"

        collect_btn = await page.query_selector(
            ".interact-container .left .collect-wrapper, .engage-bar .collect-wrapper"
        )
        if not collect_btn:
            return f"{action}失败：找不到收藏按钮"

        await _human_click(page, collect_btn)
        await _rand_sleep(0.8)
        _RateLimit.record('like')
        return f"✅ {action}成功（笔记 {note_id}）"

    except Exception as e:
        return f"{action if 'action' in dir() else '操作'}失败：{e}"
    finally:
        await asyncio.sleep(_random.uniform(0.5, 1.2))
        await page.close()


async def _reply_via_notification(comment_id: str, content: str):
    """在通知页直接回复（真人最常见的回复路径）。
    返回 (handled, msg)：handled=False 表示通知里没找到这条/没能打开回复框，什么都没发，调用方可走原路径。"""
    global notifications_page
    if notifications_page is None or notifications_page.is_closed():
        notifications_page = await browser_context.new_page()
        notifications_page.set_default_timeout(30000)
    page = notifications_page
    cap = {}

    async def on_resp(r):
        if '/you/mentions' in r.url:
            try:
                cap['d'] = await r.json()
            except Exception:
                pass

    page.on('response', on_resp)
    try:
        await _enter_notifications(page)
        await _rand_sleep(3)
    finally:
        page.remove_listener('response', on_resp)

    msgs = ((cap.get('d') or {}).get('data') or {}).get('message_list') or []
    ids = [(m.get('comment_info') or {}).get('id', '') for m in msgs]
    if comment_id not in ids:
        return False, ''
    idx = ids.index(comment_id)
    items = page.locator('.tabs-content-container > .container')
    if await items.count() <= idx:
        return False, ''
    item = items.nth(idx)

    # 核对 DOM 这一条确实是目标评论（表情会渲染成图片，所以去掉 [xxR] 后比前 6 个字）
    def _norm(t):
        return re.sub(r'\[[^\]]{1,8}\]|\s', '', t or '')[:6]
    expected = _norm((msgs[idx].get('comment_info') or {}).get('content', ''))
    try:
        actual = _norm(await item.locator('.interaction-content').inner_text(timeout=3000))
    except Exception:
        actual = ''
    if expected and actual and expected != actual:
        print(f"[notif_reply] DOM 与接口对不上：{expected} vs {actual}，改走笔记页", file=sys.stderr, flush=True)
        return False, ''

    await item.scroll_into_view_if_needed()
    await asyncio.sleep(_random.uniform(2, 5))  # 先读一下这条通知
    btn = await item.locator('.action-reply').element_handle(timeout=5000)
    await _human_click(page, btn)
    await _rand_sleep(1.2)
    try:
        ta = await item.locator('textarea.comment-input').element_handle(timeout=5000)
    except Exception:
        return False, ''
    await _human_click(page, ta)
    await _rand_sleep(0.5)
    await _ensure_nl_route(page)
    await _human_type(page, content, soft_newline=True)
    await asyncio.sleep(_random.uniform(0.8, 2.0))

    # ── 以下点了发送，出任何问题都不能再走原路径，否则会重复发 ──
    try:
        sub = await item.locator('button.submit').element_handle(timeout=3000)
        await _human_click(page, sub)
        await _rand_sleep(2)
        risk = await _detect_risk(page)
        if risk:
            _RateLimit.trip(risk)
            return True, f"🛑 提交后检测到风控（{risk}），已熔断。回复可能没发出去，请到浏览器里确认。"
        left = ''
        try:
            if await item.locator('textarea.comment-input').count():
                left = await item.locator('textarea.comment-input').input_value(timeout=1000)
        except Exception:
            pass
        if left.strip():
            return True, "⚠️ 点了发送但输入框里还有内容，可能没发出去，请到浏览器确认（未自动重试，避免重复）。"
        return True, "✅ 回复成功（通知页直接回复）"
    except Exception as e:
        return True, f"⚠️ 发送过程中出错：{e}。可能已发出，请到浏览器确认（未自动重试，避免重复）。"


@mcp.tool()
async def reply_comment(note_id: str, comment_id: str, content: str, xsec_token: str = "", force: bool = False) -> str:
    """回复笔记下的某条评论。

    Args:
        note_id: 笔记 ID
        comment_id: 要回复的评论 ID（从 get_note_comments 获取）
        content: 回复内容
        xsec_token: 笔记的 xsec_token（可留空，会用缓存）
        force: 该评论已回复过时默认拦截并提醒；确实要再回一次时传 true
    """
    _rl = _RateLimit.check('reply')
    if _rl:
        return _rl

    # 查历史回复记录：已回复过则拦截，除非 force=True（在开页面之前查，省一次导航）
    _reply_history_prefix = ''
    try:
        _prev_replies = _get_comment_history(note_id, action_type='replied', comment_id=comment_id)
        if _prev_replies:
            _lines = [f"  · {_h['created_at']}  「{_h['content'][:60]}{'…' if len(_h['content']) > 60 else ''}」" for _h in _prev_replies]
            _msg = "你已回复过该评论：\n" + "\n".join(_lines)
            if not force:
                return f"⚠️ {_msg}\n未发送。确实要再回一次，请传 force=true。"
            _reply_history_prefix = f"⚠️ {_msg}\n（force=true，本次仍已发送）\n\n"
    except Exception as e:
        print(f"[comment_history] 查询回复历史失败: {e}", file=sys.stderr, flush=True)

    # 优先在通知页直接回复（别人回我的评论都在通知里），找不到再打开笔记页
    if not await ensure_browser():
        return "请先登录小红书账号"
    try:
        async with _notif_page_lock:
            try:
                _handled, _msg = await _reply_via_notification(comment_id, content)
            finally:
                if notifications_page is not None and not notifications_page.is_closed():
                    await _watch_leave_notifications(notifications_page)  # 回复完回首页，页面不一直停在通知页
    except _RiskBlocked:
        raise
    except Exception as e:
        print(f"[notif_reply] 通知页回复失败，改走笔记页：{e}", file=sys.stderr, flush=True)
        _handled, _msg = False, ''
    if _handled:
        if _msg.startswith('✅'):
            _record_comment('replied', note_id, comment_id=comment_id, content=content)
            _RateLimit.record('reply')
        return f"{_reply_history_prefix}{_msg}"

    page, err = await _navigate_note(note_id, xsec_token)
    if err:
        return err

    try:
        await _rand_sleep(_random.uniform(3, 6))  # 等评论区加载，顺便"读一会儿"

        # 找目标评论，支持滚动
        selector = f"#comment-{comment_id}"
        comment_el = None
        for _i in range(30):
            comment_el = await page.query_selector(selector)
            if comment_el:
                break
            if _i % 3 == 2:
                await _expand_replies(page, rounds=1)  # 楼中楼默认折叠，展开后才能定位
                comment_el = await page.query_selector(selector)
                if comment_el:
                    break
            await page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
            await _rand_sleep(0.8)

        if not comment_el:
            return f"找不到评论 {comment_id}"

        await comment_el.scroll_into_view_if_needed()
        await _rand_sleep(0.5)

        # 点回复按钮
        reply_btn = await comment_el.query_selector(".right .interactions .reply")
        if not reply_btn:
            return "找不到回复按钮"
        await _human_click(page, reply_btn)
        await _rand_sleep(0.8)

        # 输入内容（直接 evaluate 设置 + 触发 input 事件，绕过 Vue 响应式）
        comment_input = await page.query_selector(
            "div.input-box div.content-edit p.content-input"
        )
        if not comment_input:
            return "找不到回复输入框"

        await comment_input.evaluate("""(el, text) => {
            el.textContent = text;
            el.dispatchEvent(new Event('input', { bubbles: true }));
        }""", content)
        await _rand_sleep(0.5)

        # 提交
        submit_btn = await page.query_selector("div.bottom button.submit")
        if not submit_btn:
            return "找不到提交按钮"
        await _human_click(page, submit_btn)
        await _rand_sleep(1.5)
        _risk = await _detect_risk(page)
        if _risk:
            _RateLimit.trip(_risk)
            return f"🛑 提交后检测到风控（{_risk}），已熔断。回复可能没发出去，请到浏览器里确认。"
        _record_comment('replied', note_id, comment_id=comment_id, content=content)
        _RateLimit.record('reply')
        return f"{_reply_history_prefix}✅ 回复成功"

    except Exception as e:
        return f"回复失败：{e}"
    finally:
        await asyncio.sleep(_random.uniform(0.5, 1.2))
        await page.close()


@mcp.tool()
async def add_comment_history(note_id: str, content: str, action_type: str = "commented", comment_id: str = "") -> str:
    """手动补录一条评论/回复历史（漏记时用）。

    Args:
        note_id: 笔记 ID
        content: 评论/回复内容
        action_type: commented（一级评论）或 replied（回复某条评论，需填 comment_id 为被回复的评论 ID）
        comment_id: replied 时必填；commented 时可填自己那条评论的 ID
    """
    if action_type not in ("commented", "replied"):
        return "action_type 只能是 commented 或 replied"
    if action_type == "replied" and not comment_id:
        return "replied 需要填被回复的 comment_id"
    _record_comment(action_type, note_id, comment_id=comment_id, content=content)
    return f"已补录：[{action_type}] 笔记 {note_id} {('→ 评论 ' + comment_id) if comment_id else ''}"


@mcp.tool()
async def get_comment_history(note_id: str = "", limit: int = 20) -> str:
    """查询自己的评论/回复历史。可用于检查某篇笔记是否已评论过。

    Args:
        note_id: 筛选特定笔记（留空查全部最近记录）
        limit: 返回条数，默认 20
    """
    try:
        records = _get_comment_history(note_id=note_id)
        records = records[:limit]
        if not records:
            return "暂无评论/回复历史记录。"
        lines = [f"评论历史（共 {len(records)} 条）：\n"]
        for i, r in enumerate(records, 1):
            if r['action_type'] == 'replied' and r['comment_id']:
                target = f"笔记 {r['note_id']} → 评论 {r['comment_id']}"
            else:
                target = f"笔记 {r['note_id']}"
            lines.append(f"{i}. [{r['action_type']}] {r['created_at']}  {target}")
            lines.append(f"   内容：「{r['content']}」\n")
        return "\n".join(lines)
    except Exception as e:
        return f"查询历史失败：{e}"


@mcp.tool()
async def like_comment(note_id: str, comment_id: str, xsec_token: str = "", unlike: bool = False) -> str:
    """点赞或取消点赞笔记下的某条评论。

    Args:
        note_id: 笔记 ID
        comment_id: 评论 ID
        xsec_token: 笔记的 xsec_token
        unlike: True 为取消点赞
    """
    _rl = _RateLimit.check('like')
    if _rl:
        return _rl

    page, err = await _navigate_note(note_id, xsec_token)
    if err:
        return err

    try:
        action = "取消点赞评论" if unlike else "点赞评论"
        await _rand_sleep(2)

        selector = f"#comment-{comment_id}"
        comment_el = None
        for _i in range(30):
            comment_el = await page.query_selector(selector)
            if comment_el:
                break
            if _i % 3 == 2:
                await _expand_replies(page, rounds=1)  # 楼中楼默认折叠，展开后才能定位
                comment_el = await page.query_selector(selector)
                if comment_el:
                    break
            await page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
            await _rand_sleep(0.8)

        if not comment_el:
            return f"找不到评论 {comment_id}"

        await comment_el.scroll_into_view_if_needed()
        await _rand_sleep(0.5)

        like_btn = await comment_el.query_selector(".like .like-wrapper")
        if not like_btn:
            return f"{action}失败：找不到点赞按钮"

        # 通过 xlink:href 判断状态（#like=未赞，#liked=已赞）
        is_liked = await like_btn.evaluate("""el => {
            const use = el.querySelector('use');
            return use?.getAttribute('xlink:href') === '#liked';
        }""")

        if (unlike and not is_liked) or (not unlike and is_liked):
            return f"无需操作：评论当前{'已点赞' if is_liked else '未点赞'}"

        await like_btn.evaluate("""el => {
            el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
        }""")
        await _rand_sleep(0.8)
        _RateLimit.record('like')
        return f"✅ {action}成功"

    except Exception as e:
        return f"{action if 'action' in dir() else '操作'}失败：{e}"
    finally:
        await asyncio.sleep(_random.uniform(0.5, 1.2))
        await page.close()


def _make_text_image(title: str, content: str, output_path: str) -> None:
    """用 Pillow 生成小红书风格文字图片（1080x1440）。"""
    from PIL import Image, ImageDraw, ImageFont
    import random, textwrap

    FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
    FONT_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"

    PALETTES = [
        ("#FF6B9D", "#FFF0F6"),  # 粉红
        ("#6B5CE7", "#F3F0FF"),  # 紫
        ("#0AC18E", "#F0FFF9"),  # 青绿
        ("#FF9F43", "#FFF7EC"),  # 橙
        ("#3D5A80", "#EEF2FF"),  # 蓝
    ]
    accent, bg = random.choice(PALETTES)

    W, H = 1080, 1440
    img = Image.new("RGB", (W, H), color=bg)
    draw = ImageDraw.Draw(img)

    # 顶部色块装饰
    draw.rectangle([0, 0, W, 180], fill=accent)

    # 字体
    try:
        font_title = ImageFont.truetype(FONT_BOLD if os.path.exists(FONT_BOLD) else FONT_PATH, 80)
        font_body  = ImageFont.truetype(FONT_PATH, 52)
        font_deco  = ImageFont.truetype(FONT_PATH, 36)
    except Exception:
        font_title = font_body = font_deco = ImageFont.load_default()

    # 标题（色块上白字）
    draw.text((W // 2, 90), title[:18], font=font_title, fill="white", anchor="mm")

    # 正文（换行，最多 14 行）
    chars_per_line = 16
    lines = []
    for para in content.split("\n"):
        wrapped = textwrap.wrap(para, width=chars_per_line) or [""]
        lines.extend(wrapped)
    lines = lines[:14]

    y = 280
    line_h = 80
    for line in lines:
        draw.text((100, y), line, font=font_body, fill="#333333")
        y += line_h

    # 底部装饰线
    draw.rectangle([80, H - 120, W - 80, H - 116], fill=accent)
    draw.text((W // 2, H - 80), "小红书", font=font_deco, fill=accent, anchor="mm")

    img.save(output_path, "JPEG", quality=95)


async def _pub_click(page, selector: str, text: str = "", timeout: float = 10.0) -> bool:
    """创作者页点击：页面里常有隐藏的重复节点（在屏幕外 -9999 处）和叠在一起的重复节点，
    所以只挑真正落在视口内的那一份，用真实鼠标坐标点，鼠标天然点到最上层。"""
    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - t0 < timeout:
        pos = await page.evaluate("""([sel, txt]) => {
            for (const e of document.querySelectorAll(sel)) {
                if (txt && (e.innerText || '').trim() !== txt) continue;
                const r = e.getBoundingClientRect();
                if (r.width > 0 && r.height > 0 && r.left >= 0 && r.top >= 0 && r.top < innerHeight && r.left < innerWidth)
                    return [r.left + r.width / 2, r.top + r.height / 2];
            }
            return null;
        }""", [selector, text])
        if pos:
            await _human_click(page, x=pos[0], y=pos[1])
            return True
        await asyncio.sleep(0.5)
    return False


async def _fill_and_publish(page, title: str, content: str, tags: list,
                            text_card_mode: bool = False) -> str:
    """填写标题、正文、标签，然后点发布。供两种模式复用。"""
    await _rand_sleep(1.0)

    # ── 填标题 ───────────────────────────────────────────────
    title_el = (
        await page.query_selector("input[placeholder='填写标题会有更多赞哦']") or
        await page.query_selector("div.d-input input") or
        await page.query_selector("input.d-text")
    )
    if title_el:
        await title_el.click()
        await _rand_sleep(0.3)
        await _human_type(page, title[:20])
    await _rand_sleep(0.4)

    # ── 填正文 ───────────────────────────────────────────────
    if text_card_mode:
        # text_card 模式：正文框是 tiptap
        # 发布编辑页的 tiptap 可能残留卡片文字，先全选清空再写正文
        editor = await page.query_selector("div.tiptap.ProseMirror")
        if editor:
            await editor.click()
            await page.keyboard.press("Control+a")
            await _rand_sleep(0.2)
            await page.keyboard.press("Delete")
            await _rand_sleep(0.3)
            if content:
                await _human_type(page, content)
    else:
        # upload / pillow 模式：现在发布页正文也是 tiptap（ql-editor 已不存在，留作旧版兜底）
        editor = (await page.query_selector("div.tiptap.ProseMirror") or
                  await page.query_selector("div.ql-editor"))
        if editor and content:
            await editor.click()
            await _rand_sleep(0.3)
            await _human_type(page, content)
    await _rand_sleep(0.5)

    # ── 添加话题标签 ─────────────────────────────────────────
    if tags and editor:
        await editor.click()
        await page.keyboard.press("Control+End")
        await _rand_sleep(0.3)
        for tag in tags:
            await _human_type(page, f" #{tag}")
            await _rand_sleep(0.6)
            sug = await page.query_selector(
                f'#creator-editor-topic-container .item:has-text("{tag}")'
            )
            if sug:
                await sug.click()
            else:
                await page.keyboard.press("Space")
            await _rand_sleep(0.4)

    await _rand_sleep(1.0)

    # ── 点发布（xhs-publish-btn Web Component，closed shadow DOM）──
    # 通过页面加载时注入的 attachShadow 拦截器访问 closed shadow root
    # 按钮类名：button.ce-btn.bg-red（第二个 button，即"发布"）
    # 先取 shadow 里"发布"按钮的屏幕坐标，用真实鼠标点；取不到再退回 JS click
    _pos = await page.evaluate("""() => {
        const xb = document.querySelector('xhs-publish-btn');
        const sh = xb && (window.__shadowRoots?.get(xb) || xb.shadowRoot);
        const btn = sh && (sh.querySelector('button.ce-btn.bg-red') || sh.querySelector('button.bg-red'));
        if (!btn) return null;
        const r = btn.getBoundingClientRect();
        return (r.width > 0 && r.height > 0) ? [r.left + r.width / 2, r.top + r.height / 2] : null;
    }""")
    if _pos:
        await _human_click(page, x=_pos[0], y=_pos[1])
        clicked = "clicked: mouse"
    else:
      clicked = await page.evaluate("""() => {
        const xhsBtn = document.querySelector('xhs-publish-btn');
        if (!xhsBtn) return null;

        // 优先用注入的 __shadowRoots 访问 closed shadow root
        const shadow = window.__shadowRoots?.get(xhsBtn) || xhsBtn.shadowRoot;
        if (shadow) {
            // 精确找红色发布按钮
            const btn = shadow.querySelector('button.ce-btn.bg-red') ||
                        shadow.querySelector('button.bg-red') ||
                        shadow.querySelectorAll('button')[1];  // 第二个按钮是"发布"
            if (btn) {
                btn.click();
                return 'clicked: ' + (btn.className || btn.textContent?.trim());
            }
            return 'shadow found but no btn, HTML: ' + shadow.innerHTML.substring(0, 200);
        }
        return 'no shadow root';
    }""")

    if not clicked:
        return "找不到 xhs-publish-btn 元素"
    if 'no shadow' in clicked or 'shadow found but' in clicked:
        return f"发布按钮定位失败：{clicked}"

    # 轮询最多 20 秒：跳到成功页/管理页，或页面出现"发布成功"，才算成功
    for _ in range(20):
        await asyncio.sleep(1)
        result_url = page.url
        if any(kw in result_url for kw in ("success", "manage", "note-manager")):
            return "✅ 发布成功！"
        try:
            if await page.locator('text=/发布成功/').count():
                return "✅ 发布成功！"
        except Exception:
            pass
    return f"⚠️ 已点击发布，但 20 秒内没看到成功标志（当前 {page.url}），请到小红书确认"


@mcp.tool()
async def delete_note(note_id: str) -> str:
    """删除指定笔记。note_id 从 get_my_notes 返回的链接中获取（URL 最后一段）。"""
    login_status = await ensure_browser()
    if not login_status:
        return "请先登录小红书账号"
    if not browser_context:
        return "浏览器上下文未初始化"
    page = await browser_context.new_page()

    try:
        await page.goto(
            "https://creator.xiaohongshu.com/creator/notemanage",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        await _rand_sleep(2)

        # 卡片上已经没有带 note_id 的链接，id 只在卡片的 data-impression 属性里：
        # 找"最小的、既含该 id 又含删除按钮"的祖先，取它里面删除按钮的坐标
        target = None
        for _ in range(3):
            target = await page.evaluate("""(nid) => {
                for (const del of document.querySelectorAll('.note-card__action-btn--del')) {
                    let el = del;
                    for (let i = 0; i < 10 && el; i++, el = el.parentElement) {
                        // 爬到装着多张卡片的容器就停，否则容器必然含 id，会误选别的卡片的按钮
                        if (el.querySelectorAll('.note-card__action-btn--del').length > 1) break;
                        if (el.outerHTML.includes(nid)) {
                            el.scrollIntoView({block: 'center'});  // 先滚，再读坐标
                            const c = el.getBoundingClientRect();
                            const d = del.getBoundingClientRect();
                            return {card: [c.left + c.width / 2, c.top + c.height / 2],
                                    del: [d.left + d.width / 2, d.top + d.height / 2]};
                        }
                    }
                }
                return null;
            }""", note_id)
            if target:
                break
            await _rand_sleep(1.5)
        if not target:
            return f"找不到笔记 ID {note_id} 对应的卡片，请确认 ID 是否正确"

        # 鼠标移到卡片上（让操作按钮出现）→ 再移到删除按钮点击
        await page.mouse.move(target['card'][0], target['card'][1], steps=8)
        await _rand_sleep(0.8)
        await _human_click(page, x=target['del'][0], y=target['del'][1])
        await _rand_sleep(1)

        # 点确认弹窗（只点视口内可见的那个）
        confirmed = None
        for _txt in ('确定', '删除', '确认'):
            if await _pub_click(page, 'button', _txt, timeout=2):
                confirmed = _txt
                break

        if not confirmed:
            return "删除按钮已点击，但未找到确认弹窗，请手动确认"

        await _rand_sleep(2)
        return f"✅ 笔记 {note_id} 已删除"

    except Exception as e:
        return f"删除失败：{e}"
    finally:
        await page.close()


@mcp.tool()
async def publish_note(
    title: str,
    content: str,
    image_paths: list = [],
    tags: list = [],
    mode: str = "auto",
    card_text: str = "",
) -> str:
    """发布小红书图文笔记。

    Args:
        title: 笔记标题（最多20字）
        content: 正文内容（纯文字，不支持 Markdown）
        image_paths: 本地图片路径列表（jpg/png/webp）
        tags: 话题标签列表（不含#号，如 ["日常", "分享"]）
        mode: 配图模式：
              "text_card" - 用小红书内置文字配图功能（推荐，零依赖）
              "upload"    - 上传本地图片（需要提供 image_paths）
              "pillow"    - 本地生成纯色文字图片后上传
              "auto"      - 有 image_paths 用 upload，否则用 text_card
        card_text: text_card 模式下卡片显示的文字（留空则自动用标题+正文前100字）
    """
    login_status = await ensure_browser()
    if not login_status:
        return "请先登录小红书账号"
    if not browser_context:
        return "浏览器上下文未初始化"

    # 决定实际模式
    valid_paths = [p for p in image_paths if os.path.exists(p)]
    if mode == "auto":
        mode = "upload" if valid_paths else "text_card"

    page = await browser_context.new_page()
    temp_img = None

    try:
        await page.goto(
            "https://creator.xiaohongshu.com/publish/publish",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        await _rand_sleep(2)

        if "login" in page.url or "passport" in page.url:
            return "Session 已过期，需要重新登录"

        try:
            await page.wait_for_selector("div.upload-content", timeout=20000)
        except Exception:
            return f"创作者页面未正确加载（URL：{page.url}）"

        # ── 点"上传图文"标签（JS click，避免视口外问题）──────────
        if not await _pub_click(page, 'div.creator-tab', '上传图文'):
            return "找不到\"上传图文\"标签"
        await _rand_sleep(1.5)

        # ════════════════════════════════════════════════════════
        # 模式 A：小红书文字配图
        # ════════════════════════════════════════════════════════
        if mode == "text_card":
            # 点"文字配图"按钮
            if not await _pub_click(page, 'button.text2image-button'):
                return "找不到文字配图按钮"
            await _rand_sleep(2)

            # 等待 TipTap 编辑器出现
            try:
                await page.wait_for_selector("div.tiptap.ProseMirror", timeout=10000)
            except Exception:
                return "文字配图编辑器未出现"

            # 输入卡片文字（只放标题，正文另外写）
            text = card_text or title
            editor = await page.query_selector("div.tiptap.ProseMirror")
            await editor.click()
            await _rand_sleep(0.3)
            await _human_type(page, text)
            await _rand_sleep(0.8)

            # 点"生成图片"
            if not await _pub_click(page, 'div.edit-text-button'):
                return "找不到生成图片按钮"
            await _rand_sleep(2)

            # 循环点"下一步"，直到标题输入框出现（发布编辑页）
            # 流程：生成图片 → 预览图片页 → 下一步 → 发布编辑页
            for attempt in range(4):
                # 等"下一步"按钮出现（最多10秒）
                next_btn_found = False
                for _ in range(10):
                    has_next = await page.evaluate("""() => {
                        for (const btn of document.querySelectorAll('button'))
                            if (btn.innerText?.trim() === '下一步') return true;
                        return false;
                    }""")
                    if has_next:
                        next_btn_found = True
                        break
                    # 检查是否已经有标题输入框（说明已到发布页）
                    title_check = await page.query_selector(
                        "input[placeholder='填写标题会有更多赞哦']"
                    )
                    if title_check:
                        break
                    await _rand_sleep(1)

                # 有标题框就停
                title_check = await page.query_selector(
                    "input[placeholder='填写标题会有更多赞哦']"
                )
                if title_check:
                    break

                if not next_btn_found:
                    if attempt == 0:
                        return "生成图片超时，未出现下一步按钮"
                    break

                # 点下一步
                await _pub_click(page, 'button', '下一步', timeout=3)
                await _rand_sleep(2.5)

        # ════════════════════════════════════════════════════════
        # 模式 B：上传本地图片
        # ════════════════════════════════════════════════════════
        elif mode == "upload":
            if not valid_paths:
                return "upload 模式需要提供有效的 image_paths"

            upload_input = await page.query_selector(".upload-input")
            if not upload_input:
                return "找不到上传输入框（.upload-input）"

            await upload_input.set_input_files(valid_paths)

            # 等待上传完成（最多60秒）
            t0 = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - t0 < 60:
                previews = await page.query_selector_all(".img-preview-area .pr")
                if len(previews) >= len(valid_paths):
                    break
                await _rand_sleep(0.5)
            await _rand_sleep(2)

        # ════════════════════════════════════════════════════════
        # 模式 C：Pillow 本地生成后上传
        # ════════════════════════════════════════════════════════
        elif mode == "pillow":
            temp_img = f"/tmp/xhs_auto_{int(asyncio.get_event_loop().time())}.jpg"
            try:
                _make_text_image(title, content, temp_img)
            except Exception as e:
                return f"Pillow 生成图片失败：{e}"

            upload_input = await page.query_selector(".upload-input")
            if not upload_input:
                return "找不到上传输入框"
            await upload_input.set_input_files([temp_img])

            t0 = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - t0 < 30:
                previews = await page.query_selector_all(".img-preview-area .pr")
                if len(previews) >= 1:
                    break
                await _rand_sleep(0.5)
            await _rand_sleep(2)

        else:
            return f"未知 mode：{mode}，可选 text_card / upload / pillow / auto"

        # ── 填标题正文标签，点发布 ────────────────────────────────
        return await _fill_and_publish(page, title, content, tags,
                                       text_card_mode=(mode == "text_card"))

    except Exception as e:
        return f"发布失败：{e}"
    finally:
        await _rand_sleep(2)
        await page.close()
        if temp_img and os.path.exists(temp_img):
            try:
                os.remove(temp_img)
            except Exception:
                pass


if __name__ == "__main__":
    import sys
    transport = "stdio"
    host = "127.0.0.1"
    port = 8765
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--http":
            transport = "streamable-http"
        elif arg.startswith("--port="):
            port = int(arg.split("=")[1])
        elif arg == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])
    if transport == "streamable-http":
        print(f"启动 HTTP 模式，监听 {host}:{port}/mcp")
        mcp.run(transport="streamable-http", host=host, port=port)
    else:
        mcp.run(transport="stdio")