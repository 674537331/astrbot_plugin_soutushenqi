# -*- coding: utf-8 -*-
import re
import urllib.parse
import asyncio
import aiohttp
from playwright.async_api import async_playwright, Browser, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
from astrbot.api import logger

PLAYWRIGHT_TIMEOUT = 15000
SCROLL_TIMES = 4
SCROLL_WAIT = 1000

_playwright_mgr = None
_browser: Browser = None
_scraper_session = None
_scraper_lock = None

def get_scraper_lock() -> asyncio.Lock:
    global _scraper_lock
    if _scraper_lock is None:
        _scraper_lock = asyncio.Lock()
    return _scraper_lock

async def get_browser() -> Browser:
    global _playwright_mgr, _browser
    async with get_scraper_lock():
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
                    try:
                        await _playwright_mgr.stop()
                    except Exception as stop_e:
                        # 🚀 修复：严禁使用裸露的 except: pass 🚀
                        logger.debug(f"清理失败的 Playwright Mgr 时发生异常: {stop_e}")
                    _playwright_mgr = None
                _browser = None
                raise e
    return _browser

async def get_scraper_session() -> aiohttp.ClientSession:
    global _scraper_session
    async with get_scraper_lock():
        if _scraper_session is None or _scraper_session.closed:
            _scraper_session = aiohttp.ClientSession()
    return _scraper_session

async def close_browser():
    global _playwright_mgr, _browser
    async with get_scraper_lock():
        if _browser:
            try:
                await asyncio.wait_for(_browser.close(), timeout=5.0)
            except Exception as e:
                logger.error(f"强制关闭 Browser 实例时发生异常: {e}")
            finally:
                _browser = None
                
        if _playwright_mgr:
            try:
                await asyncio.wait_for(_playwright_mgr.stop(), timeout=5.0)
            except Exception as e:
                logger.error(f"强制关闭 Playwright Mgr 时发生异常: {e}")
            finally:
                _playwright_mgr = None

async def close_scraper_session():
    global _scraper_session
    async with get_scraper_lock():
        if _scraper_session and not _scraper_session.closed:
            await _scraper_session.close()
            _scraper_session = None

def is_valid_image_url(u: str) -> bool:
    if not u.startswith("http") or 'soutushenqi.com' in u: return False
    if 'baidu.com' in u or 'bdimg.com' in u or 'bdstatic.com' in u: return False
    low_u = u.lower()
    if any(x in low_u for x in ['avatar', 'logo', 'icon', 'qrcode']): return False
    
    parsed = urllib.parse.urlparse(low_u)
    path = parsed.path
    if not any(path.endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp']):
        return False
        
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

async def fetch_bing_image_urls(keyword: str, target_count: int) -> list[str]:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    valid_urls = []
    seen_urls = set()
    first = 0
    pages_fetched = 0
    max_pages = 10 
    consecutive_errors = 0 
    
    session = await get_scraper_session()
    
    while len(valid_urls) < target_count and pages_fetched < max_pages:
        pages_fetched += 1
        url = f"https://www.bing.com/images/search?q={urllib.parse.quote(keyword)}&first={first}"
        try:
            async with session.get(url, headers=headers, timeout=15) as resp:
                if resp.status != 200:
                    if resp.status in (403, 429):
                        logger.warning(f"Bing 触发 {resp.status} 反爬拦截，主动熔断。")
                        break
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        break
                    first += 35
                    continue
                    
                consecutive_errors = 0 
                html = await resp.text()
                matches = re.findall(r'(?:"|&quot;)murl(?:"|&quot;)\s*:\s*(?:"|&quot;)(https?://[^\s"\'<>]+)(?:"|&quot;)', html)
                new_found = 0
                
                for img_url in matches:
                    if img_url and img_url.startswith("http"):
                        low_u = img_url.lower()
                        if any(x in low_u for x in ['avatar', 'logo', 'icon', 'profile']):
                            continue
                        if img_url not in seen_urls:
                            valid_urls.append(img_url)
                            seen_urls.add(img_url)
                            new_found += 1
                            if len(valid_urls) >= target_count:
                                return valid_urls
                if new_found == 0:
                    break
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            # 🚀 修复未使用异常变量的坏味道 🚀
            logger.debug(f"Bing 网络请求错误或超时: {e}")
            consecutive_errors += 1
            if consecutive_errors >= 3:
                break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Bing 翻页异常: {e}")
            break
            
        first += 35 
        
    return valid_urls

async def fetch_image_urls(keyword: str, target_count: int) -> tuple[list[str], str]:
    valid_urls = []
    error_msg = ""
    context = None
    page = None
    
    try:
        browser = await get_browser()
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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
            try:
                await asyncio.wait_for(page.close(), timeout=2.0)
            except Exception as e:
                logger.debug(f"清理 Page 句柄时发生异常: {e}")
        if context:
            try:
                await asyncio.wait_for(context.close(), timeout=2.0)
            except Exception as e:
                logger.debug(f"清理 Context 句柄时发生异常: {e}")
            
    return valid_urls[:target_count], error_msg
