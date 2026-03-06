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

# --- 全局资源管理器 ---
_playwright_mgr = None
_browser: Browser = None
_browser_lock = asyncio.Lock()

_scraper_session = None
_scraper_session_lock = asyncio.Lock()

async def get_browser() -> Browser:
    global _playwright_mgr, _browser
    async with _browser_lock:
        if _browser is None:
            try:
                logger.info("初始化全局 Playwright 浏览器实例...")
                _playwright_mgr = await async_playwright().start()
                _browser = await _playwright_mgr.chromium.launch(
                    headless=True,
                    args=['--disable-blink-features=AutomationControlled', '--no-sandbox', '--disable-setuid-sandbox']
                )
            except Exception as e:
                logger.error(f"Playwright 浏览器初始化失败: {e}")
                if _playwright_mgr:
                    await _playwright_mgr.stop()
                    _playwright_mgr = None
                _browser = None
                raise e
    return _browser

async def get_scraper_session() -> aiohttp.ClientSession:
    global _scraper_session
    async with _scraper_session_lock:
        if _scraper_session is None or _scraper_session.closed:
            _scraper_session = aiohttp.ClientSession()
    return _scraper_session

async def close_browser():
    global _playwright_mgr, _browser
    async with _browser_lock:
        if _browser:
            await _browser.close()
            _browser = None
        if _playwright_mgr:
            await _playwright_mgr.stop()
            _playwright_mgr = None

async def close_scraper_session():
    global _scraper_session
    async with _scraper_session_lock:
        if _scraper_session and not _scraper_session.closed:
            await _scraper_session.close()
            _scraper_session = None

async def fetch_bing_image_urls(keyword: str, target_count: int) -> list[str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    valid_urls = []
    seen_urls = set()
    first = 0
    
    session = await get_scraper_session()
    
    try:
        while len(valid_urls) < target_count:
            url = f"https://www.bing.com/images/search?q={urllib.parse.quote(keyword)}&first={first}"
            async with session.get(url, headers=headers, timeout=15) as resp:
                if resp.status != 200:
                    break
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
                first += 35 
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.error(f"Bing 翻页抓取发生网络或超时异常: {e}")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"Bing 翻页抓取发生未知异常: {e}")
        
    return valid_urls

async def fetch_image_urls(keyword: str, target_count: int) -> tuple[list[str], str]:
    valid_urls = []
    error_msg = ""
    context = None
    
    try:
        browser = await get_browser()
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            viewport={'width': 1920, 'height': 1080}
        )
        page = await context.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        try:
            search_url = f"https://www.soutushenqi.com/image/search?searchWord={urllib.parse.quote(keyword)}"
            await page.goto(search_url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT)
            
            for _ in range(SCROLL_TIMES):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                
                try:
                    await page.wait_for_load_state("networkidle", timeout=SCROLL_WAIT)
                except PlaywrightTimeoutError:
                    pass
                
                html_content = await page.content()
                raw_urls = re.findall(r'https?://[^"\'\s\\<>]+|https?%3A%2F%2F[^"\'\s\\<>&]+', html_content)
                
                for u in raw_urls:
                    if '%3A%2F%2F' in u:
                        u = urllib.parse.unquote(u)
                    if not u.startswith("http") or 'soutushenqi.com' in u:
                        continue
                    if 'baidu.com' in u or 'bdimg.com' in u or 'bdstatic.com' in u:
                        continue
                    
                    low_u = u.lower()
                    if any(x in low_u for x in ['avatar', 'logo', 'icon', 'qrcode']):
                        continue
                    if not any(u.endswith(ext) or f"{ext}?" in u for ext in ['.jpg', '.jpeg', '.png', '.webp']):
                        continue
                        
                    clean_url = u.split('@')[0].rstrip('.,;)')
                    if clean_url not in valid_urls:
                        valid_urls.append(clean_url)
                    if len(valid_urls) >= target_count:
                        break
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
        if context:
            await context.close()
            
    return valid_urls[:target_count], error_msg
