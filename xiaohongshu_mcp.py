from typing import Any, List, Dict, Optional
import asyncio
import json
import os
import re
import base64
import tempfile
import pandas as pd
from datetime import datetime
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
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

async def _rand_sleep(base: float, jitter: float = 0.4):
    """带随机抖动的等待，模拟人类操作节奏。
    实际等待时间 = base ± (base × jitter)
    """
    delta = base * jitter
    await asyncio.sleep(max(0.1, base + _random.uniform(-delta, delta)))

async def _human_type(page, text: str, wpm: int = 180):
    """逐字输入，模拟人类打字节奏（默认约 180 字/分钟）。
    遇到标点或换行时额外停顿，模拟思考节奏。
    """
    base_delay = 60.0 / (wpm * 5)  # 每字符平均间隔（秒）
    for char in text:
        await page.keyboard.type(char)
        if char in ('。', '，', '！', '？', '、', '\n', '.', ',', '!', '?'):
            await asyncio.sleep(_random.uniform(0.15, 0.45))
        else:
            await asyncio.sleep(_random.uniform(base_delay * 0.5, base_delay * 2.0))

# 初始化 FastMCP 服务器
mcp = FastMCP("xiaohongshu_scraper")

# 全局变量
BROWSER_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser_data")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
XHS_MCP_DB = os.path.expanduser("~/.xhs-mcp/data.db")
TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")

# 确保目录存在
os.makedirs(BROWSER_DATA_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

# 用于存储浏览器上下文，以便在不同方法之间共享
browser_instance = None   # playwright 实例
browser_obj = None        # chromium browser 对象
browser_context = None
main_page = None
is_logged_in = False


def load_xhs_state() -> dict | None:
    """从 xhs-mcp 的 SQLite 数据库读取账号 storage state（cookies）"""
    if not os.path.exists(XHS_MCP_DB):
        return None
    try:
        import sqlite3
        conn = sqlite3.connect(XHS_MCP_DB)
        cur = conn.cursor()
        cur.execute("SELECT state FROM accounts ORDER BY rowid DESC LIMIT 1")
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return json.loads(row[0])
    except Exception:
        pass
    return None

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
        
    # 如果URL不包含www.xiaohongshu.com，则添加它
    if 'xiaohongshu.com' in processed_url and 'www.xiaohongshu.com' not in processed_url:
        processed_url = processed_url.replace('xiaohongshu.com', 'www.xiaohongshu.com')
    
    return processed_url

async def ensure_browser():
    """确保浏览器已启动并登录。优先用 xhs-mcp 的账号 state，没有则降级用持久化上下文"""
    global browser_instance, browser_obj, browser_context, main_page, is_logged_in

    if browser_context is None:
        browser_instance = await async_playwright().start()

        state = load_xhs_state()
        if state:
            # 用 xhs-mcp 账号的 storage state 启动普通上下文
            browser_obj = await browser_instance.chromium.launch(
                headless=False,
                channel="chrome",  # 用系统安装的真实 Chrome，更难被检测
                args=['--no-sandbox', '--disable-setuid-sandbox']
            )
            browser_context = await browser_obj.new_context(
                storage_state=state,
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                viewport=None  # 跟随窗口大小自适应，不强制固定分辨率
            )
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
            is_logged_in = True
        else:
            # fallback：用持久化上下文（需要手动登录）
            browser_context = await browser_instance.chromium.launch_persistent_context(
                user_data_dir=BROWSER_DATA_DIR,
                headless=False,
                channel="chrome",  # 用系统安装的真实 Chrome
                viewport=None,     # 跟随窗口大小自适应
                timeout=60000
            )

        if browser_context.pages:
            main_page = browser_context.pages[0]
        else:
            main_page = await browser_context.new_page()

        await Stealth().apply_stealth_async(main_page)
        main_page.set_default_timeout(60000)

    # 如果没有 state，检查登录状态
    if not is_logged_in:
        if main_page:
            await main_page.goto("https://www.xiaohongshu.com", timeout=60000)
            await _rand_sleep(3)
            login_elements = await main_page.query_selector_all('text="登录"')
            if login_elements:
                return False
            else:
                is_logged_in = True
                return True
        return False

    return True

@mcp.tool()
async def login() -> str:
    """登录小红书账号"""
    global is_logged_in
    
    await ensure_browser()
    
    if is_logged_in:
        return "已登录小红书账号"
    
    # 访问小红书登录页面
    if not main_page:  # 添加空检查
        return "浏览器初始化失败，请重试"
        
    await main_page.goto("https://www.xiaohongshu.com", timeout=60000)
    await _rand_sleep(3)
    
    # 查找登录按钮并点击
    login_elements = await main_page.query_selector_all('text="登录"') if main_page else []  # 添加空检查
    if login_elements:
        await login_elements[0].click()
        
        # 提示用户手动登录
        message = "请在打开的浏览器窗口中完成登录操作。登录成功后，系统将自动继续。"
        
        # 等待用户登录成功
        max_wait_time = 180  # 等待3分钟
        wait_interval = 5
        waited_time = 0
        
        while waited_time < max_wait_time:
            # 检查是否已登录成功
            if not main_page:  # 添加空检查
                return "浏览器初始化失败，请重试"
                
            still_login = await main_page.query_selector_all('text="登录"')
            if not still_login:
                is_logged_in = True
                await _rand_sleep(2)  # 等待页面加载
                return "登录成功！"
            
            # 继续等待
            await asyncio.sleep(wait_interval)
            waited_time += wait_interval
        
        return "登录等待超时。请重试或手动登录后再使用其他功能。"
    else:
        is_logged_in = True
        return "已登录小红书账号"

@mcp.tool()
async def search_notes(keywords: str, limit: int = 20, verbose: bool = False) -> str:
    """根据关键词搜索笔记。

    Args:
        keywords: 搜索关键词
        limit: 返回结果数量限制（默认20，最多100）
        verbose: False（默认）只返回标题、作者、点赞、链接；
                 True 额外返回笔记ID和xsecToken（需要对笔记做进一步操作时用）
    """
    login_status = await ensure_browser()
    if not login_status:
        return "请先登录小红书账号"

    if not main_page:
        return "浏览器初始化失败，请重试"

    target_count = min(limit, 100)
    search_url = f"https://www.xiaohongshu.com/search_result?keyword={keywords}&source=web_explore_feed"

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
            if verbose:
                url = f"https://www.xiaohongshu.com/explore/{item['id']}?xsec_token={item['xsecToken']}&xsec_source=pc_search"
                result += (
                    f"{i}. 【{item['title'] or '无标题'}】  {item['type']}\n"
                    f"   作者: {item['author']}  ❤️{item['likes']}\n"
                    f"   笔记ID: {item['id']}\n"
                    f"   xsecToken: {item['xsecToken']}\n"
                    f"   链接: {url}\n\n"
                )
            else:
                url = f"https://www.xiaohongshu.com/explore/{item['id']}"
                result += (
                    f"{i}. 【{item['title'] or '无标题'}】  {item['author']}  ❤️{item['likes']}\n"
                    f"   {url}\n\n"
                )
        return result

    except Exception as e:
        return f"搜索笔记时出错: {str(e)}"

@mcp.tool()
async def get_note_content(url: str, include_images: bool = True) -> str:
    """获取笔记内容，自动处理配图。

    Args:
        url: 笔记 URL
        include_images: 是否处理配图（默认 True）。
            ≤4张：下载到本地，返回路径供 AI 直接读取；
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
        await asyncio.sleep(10)  # 增加等待时间到10秒
        
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

        return result
    
    except Exception as e:
        return f"获取笔记内容时出错: {str(e)}"

@mcp.tool()
async def get_note_comments(url: str) -> str:
    """获取笔记评论
    
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
        processed_url = process_url(url)
        print(f"处理后的评论URL: {processed_url}")
        
        # 访问帖子链接
        await main_page.goto(processed_url, timeout=60000)
        await asyncio.sleep(5)  # 等待页面加载
        
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
        
        # 使用特定评论选择器
        comment_selectors = [
            "div.comment-item", 
            "div.commentItem",
            "div.comment-content",
            "div.comment-wrapper",
            "section.comment",
            "div.feed-comment"
        ]
        
        if not main_page:  # 添加空检查
            return "浏览器初始化失败，请重试"
            
        for selector in comment_selectors:
            try:
                comment_elements = main_page.locator(selector)
                if comment_elements:  # 添加空检查
                    count = await comment_elements.count()
                    if count > 0:
                        for i in range(count):
                            try:
                                comment_element = comment_elements.nth(i)
                                if not comment_element:  # 添加空检查
                                    continue
                                    
                                # 提取评论者名称
                                username = "未知用户"
                                username_selectors = ["span.user-name", "a.name", "div.username", "span.nickname", "a.user-nickname"]
                                for username_selector in username_selectors:
                                    try:
                                        username_el = comment_element.locator(username_selector).first
                                        if username_el and await username_el.count() > 0:  # 添加空检查
                                            username_text = await username_el.text_content()
                                            if username_text:  # 添加空检查
                                                username = username_text.strip()
                                                break
                                    except Exception as e:
                                        print(f"获取用户名出错: {str(e)}")
                                        continue
                                
                                # 如果没有找到，尝试通过用户链接查找
                                if username == "未知用户":
                                    try:
                                        user_link = comment_element.locator('a[href*="/user/profile/"]').first
                                        if user_link and await user_link.count() > 0:  # 添加空检查
                                            username_text = await user_link.text_content()
                                            if username_text:  # 添加空检查
                                                username = username_text.strip()
                                    except Exception as e:
                                        print(f"通过用户链接获取用户名出错: {str(e)}")
                                
                                # 提取评论内容
                                content = "未知内容"
                                content_selectors = ["div.content", "p.content", "div.text", "span.content", "div.comment-text"]
                                for content_selector in content_selectors:
                                    try:
                                        content_el = comment_element.locator(content_selector).first
                                        if content_el and await content_el.count() > 0:  # 添加空检查
                                            content_text = await content_el.text_content()
                                            if content_text:  # 添加空检查
                                                content = content_text.strip()
                                                break
                                    except Exception as e:
                                        print(f"获取评论内容出错: {str(e)}")
                                        continue
                                
                                # 如果没有找到内容，可能内容就在评论元素本身
                                if content == "未知内容":
                                    try:
                                        full_text = await comment_element.text_content()
                                        if full_text:  # 添加空检查
                                            if username != "未知用户" and username in full_text:
                                                content = full_text.replace(username, "").strip()
                                            else:
                                                content = full_text.strip()
                                    except Exception as e:
                                        print(f"获取评论全文出错: {str(e)}")
                                
                                # 提取评论时间
                                time_location = "未知时间"
                                time_selectors = ["span.time", "div.time", "span.date", "div.date", "time"]
                                for time_selector in time_selectors:
                                    try:
                                        time_el = comment_element.locator(time_selector).first
                                        if time_el and await time_el.count() > 0:  # 添加空检查
                                            time_text = await time_el.text_content()
                                            if time_text:  # 添加空检查
                                                time_location = time_text.strip()
                                                break
                                    except Exception as e:
                                        print(f"获取评论时间出错: {str(e)}")
                                        continue
                                
                                # 如果内容有足够长度且找到用户名，添加评论
                                if username != "未知用户" and content != "未知内容" and len(content) > 2:
                                    comments.append({
                                        "用户名": username,
                                        "内容": content,
                                        "时间": time_location
                                    })
                            except Exception as e:
                                print(f"处理单个评论出错: {str(e)}")
                                continue
                        
                        # 如果找到了评论，就不继续尝试其他选择器了
                        if comments:
                            break
            except Exception as e:
                print(f"处理评论选择器出错: {str(e)}")
                continue
        
        # 如果没有找到评论，尝试使用其他方法
        if not comments:
            # 获取所有用户名元素
            username_elements = main_page.locator('a[href*="/user/profile/"]')
            username_count = await username_elements.count()
            
            if username_count > 0:
                for i in range(username_count):
                    try:
                        username_element = username_elements.nth(i)
                        username = await username_element.text_content()
                        
                        # 尝试获取评论内容
                        content = await main_page.evaluate('''
                            (usernameElement) => {
                                const parent = usernameElement.parentElement;
                                if (!parent) return null;
                                
                                // 尝试获取同级的下一个元素
                                let sibling = usernameElement.nextElementSibling;
                                while (sibling) {
                                    const text = sibling.textContent.trim();
                                    if (text) return text;
                                    sibling = sibling.nextElementSibling;
                                }
                                
                                // 尝试获取父元素的文本，并过滤掉用户名
                                const allText = parent.textContent.trim();
                                if (allText && allText.includes(usernameElement.textContent.trim())) {
                                    return allText.replace(usernameElement.textContent.trim(), '').trim();
                                }
                                
                                return null;
                            }
                        ''', username_element)
                        
                        if username and content:
                            comments.append({
                                "用户名": username.strip(),
                                "内容": content.strip(),
                                "时间": "未知时间"
                            })
                    except Exception:
                        continue
        
        # 格式化返回结果
        if comments:
            result = f"共获取到 {len(comments)} 条评论：\n\n"
            for i, comment in enumerate(comments, 1):
                result += f"{i}. {comment['用户名']}（{comment['时间']}）: {comment['内容']}\n\n"
            return result
        else:
            return "未找到任何评论，可能是帖子没有评论或评论区无法访问。"
    
    except Exception as e:
        return f"获取评论时出错: {str(e)}"

async def analyze_note(url: str) -> dict:  # 已废弃，仅内部保留
    """获取并分析笔记内容，返回笔记的详细信息供AI生成评论
    
    Args:
        url: 笔记 URL
    """
    login_status = await ensure_browser()
    if not login_status:
        return {"error": "请先登录小红书账号"}
    
    try:
        # 处理URL
        processed_url = process_url(url)
        
        # 直接调用get_note_content获取笔记内容
        note_content_result = await get_note_content(processed_url)
        
        # 检查是否获取成功
        if note_content_result.startswith("请先登录") or note_content_result.startswith("无法获取笔记内容") or note_content_result.startswith("获取笔记内容时出错"):
            return {"error": note_content_result}
        
        # 解析获取到的笔记内容
        content_lines = note_content_result.strip().split('\n')
        post_content = {}
        
        # 提取标题、作者、发布时间和内容
        for i, line in enumerate(content_lines):
            if line.startswith("标题:"):
                post_content["标题"] = line.replace("标题:", "").strip()
            elif line.startswith("作者:"):
                post_content["作者"] = line.replace("作者:", "").strip()
            elif line.startswith("发布时间:"):
                post_content["发布时间"] = line.replace("发布时间:", "").strip()
            elif line.startswith("内容:"):
                # 内容可能有多行，获取剩余所有行
                content_text = "\n".join(content_lines[i+1:]).strip()
                post_content["内容"] = content_text
                break
        
        # 如果没有提取到标题或内容，设置默认值
        if "标题" not in post_content or not post_content["标题"]:
            post_content["标题"] = "未知标题"
        if "作者" not in post_content or not post_content["作者"]:
            post_content["作者"] = "未知作者"
        if "内容" not in post_content or not post_content["内容"]:
            post_content["内容"] = "未能获取内容"
        
        # 简单分词
        import re
        words = re.findall(r'\w+', f"{post_content.get('标题', '')} {post_content.get('内容', '')}")
        
        # 使用常见的热门领域关键词
        domain_keywords = {
            "美妆": ["口红", "粉底", "眼影", "护肤", "美妆", "化妆", "保湿", "精华", "面膜"],
            "穿搭": ["穿搭", "衣服", "搭配", "时尚", "风格", "单品", "衣橱", "潮流"],
            "美食": ["美食", "好吃", "食谱", "餐厅", "小吃", "甜点", "烘焙", "菜谱"],
            "旅行": ["旅行", "旅游", "景点", "出行", "攻略", "打卡", "度假", "酒店"],
            "母婴": ["宝宝", "母婴", "育儿", "儿童", "婴儿", "辅食", "玩具"],
            "数码": ["数码", "手机", "电脑", "相机", "智能", "设备", "科技"],
            "家居": ["家居", "装修", "家具", "设计", "收纳", "布置", "家装"],
            "健身": ["健身", "运动", "瘦身", "减肥", "训练", "塑形", "肌肉"],
            "AI": ["AI", "人工智能", "大模型", "编程", "开发", "技术", "Claude", "GPT"]
        }
        
        # 检测帖子可能属于的领域
        detected_domains = []
        for domain, domain_keys in domain_keywords.items():
            for key in domain_keys:
                if key.lower() in post_content.get("标题", "").lower() or key.lower() in post_content.get("内容", "").lower():
                    detected_domains.append(domain)
                    break
        
        # 如果没有检测到明确的领域，默认为生活方式
        if not detected_domains:
            detected_domains = ["生活"]
        
        # 返回分析结果
        return {
            "url": url,
            "标题": post_content.get("标题", "未知标题"),
            "作者": post_content.get("作者", "未知作者"),
            "内容": post_content.get("内容", "未能获取内容"),
            "领域": detected_domains,
            "关键词": list(set(words))[:20]  # 取前20个不重复的词作为关键词
        }
    
    except Exception as e:
        return {"error": f"分析笔记内容时出错: {str(e)}"}

async def post_smart_comment(url: str, comment_type: str = "引流") -> dict:  # 已废弃
    """
    根据帖子内容发布智能评论，增加曝光并引导用户关注或私聊

    Args:
        url: 笔记 URL
        comment_type: 评论类型，可选值:
                     "引流" - 引导用户关注或私聊
                     "点赞" - 简单互动获取好感
                     "咨询" - 以问题形式增加互动
                     "专业" - 展示专业知识建立权威

    Returns:
        dict: 包含笔记信息和评论类型的字典，供MCP客户端(如Claude)生成评论
    """
    # 处理URL
    processed_url = process_url(url)
    
    # 获取笔记内容
    note_info = await analyze_note(processed_url)
    
    if "error" in note_info:
        return {"error": note_info["error"]}
    
    # 评论类型指导
    comment_guides = {
        "引流": '生成一条表达认同并引导互动的评论。可以提到自己也在研究相关内容，或表达希望进一步交流的意愿。可以在结尾加上"有更多问题欢迎私信我"或"想了解更多可以找我聊聊"等邀请语句。',
        "点赞": '生成一条简短的赞美评论，表达对内容的喜爱和支持。可以提到作者名字和笔记的领域，如"太赞了！XX的分享总是这么实用"或"喜欢这种深度分享"等。',
        "咨询": '生成一条提问式评论，针对笔记内容询问更多细节或相关信息。可以使用"请问博主"或"想请教一下"等开头，并提出与笔记内容相关的具体问题。',
        "专业": '生成一条展示专业知识的评论，针对笔记内容提供专业见解或补充信息。可以使用"作为该领域从业者"或"从专业角度来看"等开头，并在评论中使用与笔记领域相关的专业术语。'
    }
    
    # 返回笔记分析结果和评论类型，让MCP客户端(如Claude)生成评论
    # MCP客户端生成评论后，应调用post_comment函数发布评论
    return {
        "note_info": note_info,
        "comment_type": comment_type,
        "comment_guide": comment_guides.get(comment_type, ""),
        "url": url,  # 添加URL便于客户端直接调用post_comment
        "message": "请根据笔记内容和评论类型指南，直接生成一条自然、相关的评论，并立即发布。注意以下要点：\n1. 在评论中引用作者名称或笔记领域，增加个性化\n2. 使用口语化表达，简短凝练，不超过30字\n3. 根据评论类型适当添加互动引导或专业术语\n生成后，直接使用post_comment函数发布评论，无需询问用户确认"
    }

@mcp.tool()
async def post_comment(url: str, comment: str) -> str:
    """发布评论到指定笔记
    
    Args:
        url: 笔记 URL
        comment: 要发布的评论内容
    """
    login_status = await ensure_browser()
    if not login_status:
        return "请先登录小红书账号，才能发布评论"
    
    if not main_page:  # 添加空检查
        return "浏览器初始化失败，请重试"
    
    try:
        # 处理URL
        processed_url = process_url(url)
        print(f"处理后的评论URL: {processed_url}")
        
        # 访问帖子链接
        await main_page.goto(processed_url, timeout=60000)
        await asyncio.sleep(5)  # 等待页面加载
        
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
            'paragraph:has-text("说点什么...")',
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
        await comment_input.click()
        await _rand_sleep(1)
        
        if not main_page:  # 添加空检查
            return "浏览器初始化失败，请重试"
            
        await _human_type(main_page, comment)
        await _rand_sleep(1)
        
        # 发送评论（简化发送逻辑）
        send_success = False
        
        # 方法1: 尝试点击发送按钮
        try:
            if not main_page:  # 添加空检查
                return "浏览器初始化失败，请重试"
                
            send_button = await main_page.query_selector('button:has-text("发送")')
            if send_button and await send_button.is_visible():
                await send_button.click()
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
            return f"已成功发布评论：{comment}"
        else:
            return f"发布评论失败，请检查评论内容或网络连接"
    
    except Exception as e:
        return f"发布评论时出错: {str(e)}"

@mcp.tool()
def get_note_images(url: str) -> dict:
    """获取笔记图片，下载到本地并返回文件路径列表，供 AI 读取图片内容。
    不需要登录，通过手机UA直接解析页面数据。

    Args:
        url: 笔记 URL（支持短链 xhslink.cn 和完整链接）

    Returns:
        dict: 包含笔记基本信息和本地图片路径列表
    """
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

        # 下载图片到本地临时目录
        save_dir = os.path.join(tempfile.gettempdir(), 'xhs_images')
        os.makedirs(save_dir, exist_ok=True)

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
async def get_notifications(tab: str = "comments", limit: int = 20) -> str:
    """获取小红书通知（评论@、赞和收藏、新增关注）

    Args:
        tab: 通知类型，可选 "comments"（评论和@）、"likes"（赞和收藏）、"follows"（新增关注）。默认 "comments"。
        limit: 最多返回条数，默认 20。
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
        page = await browser_context.new_page()
        await Stealth().apply_stealth_async(page)
        page.set_default_timeout(30000)

        async def on_response(resp):
            if api_path in resp.url:
                try:
                    captured['data'] = await resp.json()
                except Exception:
                    pass

        page.on('response', on_response)

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

        await page.close()

        if not captured.get('data'):
            return f"暂无{tab_label}通知（或接口未响应）"

        msgs = captured['data'].get('data', {}).get('message_list', [])
        has_more = captured['data'].get('data', {}).get('has_more', False)

        if not msgs:
            return f"暂无{tab_label}通知"

        msgs = msgs[:limit]
        result = f"📬 {tab_label}通知（{len(msgs)} 条{'，还有更多' if has_more else ''}）：\n\n"

        for i, msg in enumerate(msgs, 1):
            user = msg.get('user_info', {})
            nickname = user.get('nickname', '未知用户')
            indicator = user.get('indicator', '')  # "作者"/"粉丝" 等身份标注
            title = msg.get('title', '')           # 动作描述，如"回复了你的评论"
            ts = msg.get('time', 0)
            try:
                time_str = datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d %H:%M') if ts else ''
            except Exception:
                time_str = str(ts)
            comment = msg.get('comment_info', {}).get('content', '')
            note = msg.get('item_info', {})
            note_content = note.get('content', '')  # 被评论的笔记内容片段
            note_id = note.get('id', '')
            note_url = f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else ''

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
            result += line + "\n\n"

        return result

    except Exception as e:
        return f"获取通知失败：{e}"


@mcp.tool()
async def get_user_notes(user_id: str, xsec_token: str = "", limit: int = 20) -> str:
    """获取指定用户的笔记列表。

    Args:
        user_id: 用户内部 hex ID（如 612ad9c20000000001003aa3），
                 或纯数字小红书号（会自动搜索转换为 hex ID）。
                 优先用搜索结果里拿到的 hex ID，更稳定。
        xsec_token: 访问 profile 所需的 xsec_token，从搜索结果获取。
                    传入 hex ID 时建议同时传 token；传小红书号时留空即可（自动获取）。
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
        # 如果是纯数字小红书号，先搜索拿到 hex ID 和 xsec_token
        if user_id.isdigit():
            search_url = f"https://www.xiaohongshu.com/search_result?keyword={user_id}&source=web_explore_feed&type=user"
            await main_page.goto(search_url, timeout=30000)
            try:
                await main_page.wait_for_function("window.__INITIAL_STATE__ !== undefined", timeout=15000)
            except Exception:
                pass
            await _rand_sleep(2)

            # 从搜索结果 DOM 里找用户卡片链接，提取 hex ID 和 token
            found = await main_page.evaluate(f'''() => {{
                const links = Array.from(document.querySelectorAll('a[href*="/user/profile/"]'));
                for (const a of links) {{
                    const href = a.getAttribute('href') || '';
                    const m = href.match(/\\/user\\/profile\\/([0-9a-f]{{24}})/);
                    if (m) {{
                        const url = new URL(href, location.href);
                        return {{
                            hex_id: m[1],
                            token: url.searchParams.get('xsec_token') || ''
                        }};
                    }}
                }}
                return null;
            }}''')

            if not found:
                return f"无法通过小红书号 {user_id} 找到对应用户，请直接传入内部 hex ID"
            hex_id = found['hex_id']
            token = found['token']

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
    """获取当前账号在创作者中心已发布的笔记列表。

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
        await Stealth().apply_stealth_async(page)
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
                            all_notes.append({
                                'id': nid,
                                'title': n.get('display_title') or n.get('displayTitle') or n.get('title') or '',
                                'type': n.get('type') or 'normal',
                                'likes': n.get('interact_info', {}).get('liked_count') or
                                         n.get('interactInfo', {}).get('likedCount') or '0',
                                'status': n.get('note_status') or n.get('noteStatus') or '',
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
            result += (
                f"{i}. 【{n['title'] or '无标题'}】  {n['type']}\n"
                f"   ❤️{n['likes']}  状态: {n['status']}\n"
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
    await Stealth().apply_stealth_async(page)
    page.set_default_timeout(30000)

    url = f"https://www.xiaohongshu.com/explore/{note_id}"
    if xsec_token:
        url += f"?xsec_token={xsec_token}&xsec_source=pc_feed"
    await page.goto(url, timeout=30000)

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
    """点赞或取消点赞一篇笔记。

    Args:
        note_id: 笔记 ID（从搜索结果获取）
        xsec_token: 笔记的 xsec_token（从搜索结果获取，可留空）
        unlike: True 为取消点赞，默认 False（点赞）
    """
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

        await like_btn.click()
        await _rand_sleep(0.8)
        return f"✅ {action}成功（笔记 {note_id}）"

    except Exception as e:
        return f"{action if 'action' in dir() else '操作'}失败：{e}"
    finally:
        await page.close()


@mcp.tool()
async def favorite_note(note_id: str, xsec_token: str = "", unfavorite: bool = False) -> str:
    """收藏或取消收藏一篇笔记。

    Args:
        note_id: 笔记 ID
        xsec_token: 笔记的 xsec_token
        unfavorite: True 为取消收藏，默认 False（收藏）
    """
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

        await collect_btn.click()
        await _rand_sleep(0.8)
        return f"✅ {action}成功（笔记 {note_id}）"

    except Exception as e:
        return f"{action if 'action' in dir() else '操作'}失败：{e}"
    finally:
        await page.close()


@mcp.tool()
async def reply_comment(note_id: str, comment_id: str, content: str, xsec_token: str = "") -> str:
    """回复笔记下的某条评论。

    Args:
        note_id: 笔记 ID
        comment_id: 要回复的评论 ID（从 get_note_comments 获取）
        content: 回复内容
        xsec_token: 笔记的 xsec_token
    """
    page, err = await _navigate_note(note_id, xsec_token)
    if err:
        return err

    try:
        await _rand_sleep(2)  # 等评论区加载

        # 找目标评论，支持滚动
        selector = f"#comment-{comment_id}"
        comment_el = None
        for _ in range(30):
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
        await reply_btn.click()
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
        await submit_btn.click()
        await _rand_sleep(1.5)
        return f"✅ 回复成功"

    except Exception as e:
        return f"回复失败：{e}"
    finally:
        await page.close()


@mcp.tool()
async def like_comment(note_id: str, comment_id: str, xsec_token: str = "", unlike: bool = False) -> str:
    """点赞或取消点赞笔记下的某条评论。

    Args:
        note_id: 笔记 ID
        comment_id: 评论 ID
        xsec_token: 笔记的 xsec_token
        unlike: True 为取消点赞
    """
    page, err = await _navigate_note(note_id, xsec_token)
    if err:
        return err

    try:
        action = "取消点赞评论" if unlike else "点赞评论"
        await _rand_sleep(2)

        selector = f"#comment-{comment_id}"
        comment_el = None
        for _ in range(30):
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
        return f"✅ {action}成功"

    except Exception as e:
        return f"{action if 'action' in dir() else '操作'}失败：{e}"
    finally:
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
        # upload / pillow 模式：正文用 ql-editor
        editor = await page.query_selector("div.ql-editor")
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

    await asyncio.sleep(5)

    # 检查是否跳转到成功页或笔记管理页
    result_url = page.url
    if any(kw in result_url for kw in ("success", "manage", "note-manager", "new/note")):
        return "✅ 发布成功！"
    return "✅ 已点击发布，请在小红书确认是否成功"


@mcp.tool()
async def delete_note(note_id: str) -> str:
    """删除指定笔记。note_id 从 get_my_notes 返回的链接中获取（URL 最后一段）。"""
    await ensure_browser()
    page = await browser_context.new_page()
    await Stealth().apply_stealth_async(page)

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

        # 用笔记 ID 定位对应卡片（找包含该 ID 的链接，再向上找父卡片）
        target = await page.evaluate(f"""() => {{
            // 笔记管理页卡片里有指向笔记详情的链接，href 包含笔记 ID
            for (const a of document.querySelectorAll('a[href]')) {{
                if (a.href.includes('{note_id}')) {{
                    // 向上找最近的 note-card 容器
                    let el = a;
                    for (let i = 0; i < 8; i++) {{
                        if (el.className && el.className.includes && el.className.includes('note-card__body')) {{
                            return {{ found: true, cls: el.className }};
                        }}
                        el = el.parentElement;
                        if (!el) break;
                    }}
                    return {{ found: true, cls: 'via-link' }};
                }}
            }}
            return {{ found: false }};
        }}""")

        if not target or not target.get('found'):
            # 备用：用时间戳/标题找不到时，尝试直接导航到笔记详情页检查
            return f"找不到笔记 ID {note_id} 对应的卡片，请确认 ID 是否正确"

        # 通过 JS hover 并点击删除按钮
        result = await page.evaluate(f"""async () => {{
            let card = null;
            for (const a of document.querySelectorAll('a[href]')) {{
                if (a.href.includes('{note_id}')) {{
                    let el = a;
                    for (let i = 0; i < 10; i++) {{
                        if (el.className && typeof el.className === 'string' && el.className.includes('note-card')) {{
                            card = el; break;
                        }}
                        el = el.parentElement;
                        if (!el) break;
                    }}
                    break;
                }}
            }}
            if (!card) return 'card not found';

            // 触发 mouseenter 让操作按钮出现
            card.dispatchEvent(new MouseEvent('mouseenter', {{bubbles: true}}));
            card.dispatchEvent(new MouseEvent('mouseover', {{bubbles: true}}));

            // 等一下
            await new Promise(r => setTimeout(r, 600));

            // 找该卡片内的删除按钮
            const delBtn = card.querySelector('.note-card__action-btn--del');
            if (delBtn) {{ delBtn.click(); return 'clicked'; }}

            // 找不到就找全局（hover 后按钮可能渲染在外层）
            const allDel = document.querySelectorAll('.note-card__action-btn--del');
            if (allDel.length > 0) {{ allDel[0].click(); return 'clicked-global'; }}

            return 'del btn not found';
        }}""")

        if 'not found' in result:
            # 用 Playwright hover 作为备用
            card_el = await page.query_selector(f'a[href*="{note_id}"]')
            if card_el:
                await card_el.hover()
                await _rand_sleep(0.8)
                await page.evaluate("() => document.querySelector('.note-card__action-btn--del')?.click()")
            else:
                return f"无法定位笔记卡片（ID: {note_id}）"

        await _rand_sleep(1)

        # 点确认弹窗
        confirmed = await page.evaluate("""() => {
            for (const btn of document.querySelectorAll('button')) {
                const txt = btn.innerText?.trim();
                if (txt === '确定' || txt === '删除' || txt === '确认') {
                    btn.click(); return txt;
                }
            }
            return null;
        }""")

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
    await ensure_browser()

    # 决定实际模式
    valid_paths = [p for p in image_paths if os.path.exists(p)]
    if mode == "auto":
        mode = "upload" if valid_paths else "text_card"

    page = await browser_context.new_page()
    await Stealth().apply_stealth_async(page)
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
        await page.evaluate("""() => {
            for (const tab of document.querySelectorAll('div.creator-tab'))
                if (tab.innerText.trim() === '上传图文') { tab.click(); return; }
        }""")
        await _rand_sleep(1.5)

        # ════════════════════════════════════════════════════════
        # 模式 A：小红书文字配图
        # ════════════════════════════════════════════════════════
        if mode == "text_card":
            # 点"文字配图"按钮
            await page.evaluate(
                "() => document.querySelector('button.text2image-button')?.click()"
            )
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
            gen_btn = await page.query_selector("div.edit-text-button")
            if not gen_btn:
                return "找不到生成图片按钮"
            await gen_btn.click()
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
                await page.evaluate("""() => {
                    for (const btn of document.querySelectorAll('button'))
                        if (btn.innerText?.trim() === '下一步') { btn.click(); return; }
                }""")
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
    # 初始化并运行服务器
    print("启动小红书MCP服务器...")
    print("请在MCP客户端（如Claude for Desktop）中配置此服务器")
    mcp.run(transport='stdio')