# -*- coding: utf-8 -*-
import re
import json
import urllib.parse
import asyncio
import aiohttp
import random
from playwright.async_api import async_playwright, Browser, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
from astrbot.api import logger

PLAYWRIGHT_TIMEOUT = 15000
SCROLL_TIMES = 4
SCROLL_WAIT = 1000

# 🚀 引入真实浏览器的 UA 池，大幅降低防爬拦截率
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0"
]

_playwright_mgr = None
_browser: Browser = None
_scraper_session = None
_scraper_lock = None

async def get_scraper_lock() -> asyncio.Lock:
    global _scraper_lock
    if _scraper_lock is None:
        _scraper_lock = asyncio.Lock()
    return _scraper_lock

async def get_browser() -> Browser:
    global _playwright_mgr, _browser
    lock = await get_scraper_lock()
    async with lock:
        if _browser is None or not _browser.is_connected():
            try:
                logger.info("初始化全局 Playwright 浏览器实例...")
                _playwright_mgr = await asyncio.wait_for(async_playwright().start(), timeout=10.0)
                _browser = await asyncio.wait_for(
                    _playwright_mgr.chromium.launch(
                        headless=True,
                        args=['--disable-blink-features=AutomationControlled', '--no-sandbox', '--disable-setuid-sandbox']
                    ), timeout=25.0
                )
            except Exception as e:
                logger.error(f"Playwright 浏览器初始化失败或超时: {e}")
                if _playwright_mgr:
                    try: await _playwright_mgr.stop()
                    except: pass
                    _playwright_mgr = None
                _browser = None
                raise e
    return _browser

async def get_scraper_session() -> aiohttp.ClientSession:
    global _scraper_session
    lock = await get_scraper_lock()
    async with lock:
        if _scraper_session is None or _scraper_session.closed:
            _scraper_session = aiohttp.ClientSession()
    return _scraper_session

async def close_browser():
    global _playwright_mgr, _browser
    lock = await get_scraper_lock()
    async with lock:
        if _browser:
            try: await asyncio.wait_for(_browser.close(), timeout=5.0)
            except: pass
            finally: _browser = None
                
        if _playwright_mgr:
            try: await asyncio.wait_for(_playwright_mgr.stop(), timeout=5.0)
            except: pass
            finally: _playwright_mgr = None

async def close_scraper_session():
    global _scraper_session
    lock = await get_scraper_lock()
    async with lock:
        if _scraper_session and not _scraper_session.closed:
            await _scraper_session.close()
            _scraper_session = None

def is_valid_image_url(u: str) -> bool:
    low_u = u.lower()
    if not low_u.startswith("http"): return False
    
    if '/assets/' in low_u or 'favicon' in low_u: return False
        
    invalid_exts = ['.js', '.css', '.html', '.php', '.json', '.xml', '.ts', '.woff', '.ttf']
    if any(ext in low_u for ext in invalid_exts): return False
        
    blacklisted_domains = ['baidu.com', 'bdimg.com', 'bdstatic.com', 'cnzz.com', 'google-analytics.com']
    if any(domain in low_u for domain in blacklisted_domains): return False
        
    if any(x in low_u for x in ['avatar', 'logo', 'icon', 'qrcode', 'profile', 'banner']): return False
    return True

def _extract_urls_from_html_sync(html_content: str, target_count: int) -> list[str]:
    raw_urls = re.findall(r'https?://[^\s"\'<>]+', html_content)
    raw_urls += re.findall(r'https?%3A%2F%2F[^\s"\'<>&]+', html_content)
    valid_urls = []
    for u in raw_urls:
        clean_url = u.split('@')[0].rstrip('.,;)')
        if '%3A%2F%2F' in clean_url:
            clean_url = urllib.parse.unquote(clean_url)
        if is_valid_image_url(clean_url):
            if clean_url not in valid_urls:
                valid_urls.append(clean_url)
            if len(valid_urls) >= target_count:
                break
    return valid_urls

# =========================================================================
# 🚀 吸收你提供的方案：使用标准 aiohttp 请求，精准解析 murl JSON 属性 🚀
# =========================================================================
async def fetch_bing_image_urls(keyword: str, target_count: int) -> list[str]:
    search_url = "https://www.bing.com/images/search"
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
    }
    
    image_urls = []
    seen_urls = set()
    first = 0
    max_pages = 10 
    pages_fetched = 0
    
    session = await get_scraper_session()
    
    while len(image_urls) < target_count and pages_fetched < max_pages:
        pages_fetched += 1
        params = {"q": keyword, "first": first}
        
        try:
            logger.info(f"Bing 兜底引擎: 正在请求下标 {first} ...")
            async with session.get(search_url, headers=headers, params=params, timeout=15) as resp:
                if resp.status != 200:
                    logger.warning(f"Bing 返回状态码 {resp.status}，提前终止。")
                    break
                    
                html = await resp.text()
                
                # 预处理：将 HTML 中的 &quot; 转回双引号，极大降低解析难度
                html_clean = html.replace('&quot;', '"')
                
                # 核心提取逻辑：直接提取所有 "murl":"https://..." 的内容 (等同于解析 m="{...}")
                matches = re.findall(r'"murl"\s*:\s*"([^"]+)"', html_clean)
                
                if not matches:
                    logger.info("Bing 兜底引擎: 页面结构改变或已无更多图片，终止翻页。")
                    break
                
                new_found = 0
                for url in matches:
                    if url not in seen_urls and is_valid_image_url(url):
                        image_urls.append(url)
                        seen_urls.add(url)
                        new_found += 1
                        if len(image_urls) >= target_count:
                            break
                            
                if new_found == 0 and first > 0:
                    logger.info("Bing 兜底引擎: 本页未发现新的有效图片，终止。")
                    break
                    
                # 步进值使用匹配到的图片数量（通常一页约 35 张）
                first += len(matches)
                
                if len(image_urls) < target_count:
                    # 礼貌性延迟，防封 IP
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                    
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error(f"Bing 网络请求出错: {e}")
            break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Bing 抓取发生异常: {e}", exc_info=True)
            break

    logger.info(f"Bing 兜底引擎: 共获取到 {len(image_urls)} 个链接。")
    return image_urls[:target_count]

async def fetch_image_urls(keyword: str, target_count: int) -> tuple[list[str], str]:
    valid_urls = []
    error_msg = ""
    context = None
    page = None
    
    try:
        browser = await get_browser()
        context = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={'width': 1920, 'height': 1080},
            ignore_https_errors=True 
        )
        page = await context.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        try:
            search_url = f"https://www.soutushenqi.com/image/search?searchWord={urllib.parse.quote(keyword)}"
            await page.goto(search_url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT)
            
            for _ in range(SCROLL_TIMES):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(SCROLL_WAIT)
                
                html_content = await page.content()
                loop = asyncio.get_running_loop()
                valid_urls = await loop.run_in_executor(None, _extract_urls_from_html_sync, html_content, target_count)
                if len(valid_urls) >= target_count:
                    break

            if not valid_urls:
                error_msg = "未能匹配到符合白名单规则的高清第三方图片URL。"
                
        except PlaywrightTimeoutError as e:
            logger.warning(f"搜图主源节点交互超时: {str(e)}")
        except PlaywrightError as e:
            logger.warning(f"搜图主源底层通讯异常: {str(e)}")
            
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"Playwright 抓取管线发生全局崩溃: {str(e)}")
    finally:
        if page:
            try: await asyncio.wait_for(page.close(), timeout=2.0)
            except: pass
        if context:
            try: await asyncio.wait_for(context.close(), timeout=2.0)
            except: pass
            
    return valid_urls[:target_count], error_msg
