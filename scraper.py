# -*- coding: utf-8 -*-
import re
import urllib.parse
import asyncio
import aiohttp
import random
from typing import List, Tuple
from playwright.async_api import async_playwright, Browser, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
from astrbot.api import logger

PLAYWRIGHT_TIMEOUT = 15000
SCROLL_TIMES = 4
SCROLL_WAIT = 1000

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0"
]

def is_valid_image_url(u: str) -> bool:
    """验证URL有效性，过滤静态UI资源及已知黑名单域名"""
    low_u = u.lower()
    if not low_u.startswith("http"): return False
    
    if '/assets/' in low_u or 'favicon' in low_u: return False
        
    invalid_exts = ['.js', '.css', '.html', '.php', '.json', '.xml', '.ts', '.woff', '.ttf']
    if any(ext in low_u for ext in invalid_exts): return False
        
    blacklisted_domains = ['baidu.com', 'bdimg.com', 'bdstatic.com', 'cnzz.com', 'google-analytics.com']
    if any(domain in low_u for domain in blacklisted_domains): return False
        
    if any(x in low_u for x in ['avatar', 'logo', 'icon', 'qrcode', 'profile', 'banner']): return False
    return True

def _extract_urls_from_html_sync(html_content: str, target_count: int) -> List[str]:
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

class ScraperManager:
    """浏览器抓取与搜索引擎分析管理器，隔离并发与生命周期状态"""
    def __init__(self):
        self._playwright_mgr = None
        self._browser: Browser = None
        self._session = None
        self._lock = None

    async def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _get_browser(self) -> Browser:
        lock = await self._get_lock()
        async with lock:
            if self._browser is None or not self._browser.is_connected():
                try:
                    self._playwright_mgr = await asyncio.wait_for(async_playwright().start(), timeout=10.0)
                    self._browser = await asyncio.wait_for(
                        self._playwright_mgr.chromium.launch(
                            headless=True,
                            args=['--disable-blink-features=AutomationControlled', '--no-sandbox', '--disable-setuid-sandbox']
                        ), timeout=25.0
                    )
                except Exception as e:
                    logger.error(f"Playwright 实例初始化异常: {e}")
                    if self._playwright_mgr:
                        try: await self._playwright_mgr.stop()
                        except: pass
                        self._playwright_mgr = None
                    self._browser = None
                    raise e
        return self._browser

    async def _get_session(self) -> aiohttp.ClientSession:
        lock = await self._get_lock()
        async with lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
        return self._session

    async def close_all(self):
        lock = await self._get_lock()
        async with lock:
            if self._browser:
                try: await asyncio.wait_for(self._browser.close(), timeout=5.0)
                except: pass
                finally: self._browser = None
                    
            if self._playwright_mgr:
                try: await asyncio.wait_for(self._playwright_mgr.stop(), timeout=5.0)
                except: pass
                finally: self._playwright_mgr = None
                
            if self._session and not self._session.closed:
                await self._session.close()
                self._session = None

    async def fetch_bing_image_urls(self, keyword: str, target_count: int) -> List[str]:
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
        
        session = await self._get_session()
        
        while len(image_urls) < target_count and pages_fetched < max_pages:
            pages_fetched += 1
            params = {"q": keyword, "first": first}
            
            try:
                async with session.get(search_url, headers=headers, params=params, timeout=15) as resp:
                    if resp.status != 200:
                        break
                        
                    html = await resp.text()
                    html_clean = html.replace('&quot;', '"')
                    
                    matches = re.findall(r'"murl"\s*:\s*"([^"]+)"', html_clean)
                    if not matches:
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
                        break
                        
                    first += len(matches)
                    
                    if len(image_urls) < target_count:
                        await asyncio.sleep(random.uniform(0.5, 1.5))
                        
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(f"网络请求异常: {e}")
                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"处理数据包发生异常: {e}", exc_info=True)
                break

        return image_urls[:target_count]

    async def fetch_image_urls(self, keyword: str, target_count: int) -> Tuple[List[str], str]:
        valid_urls = []
        error_msg = ""
        context = None
        page = None
        
        try:
            browser = await self._get_browser()
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
                    error_msg = "目标页面未包含符合验证规则的数据项。"
                    
            except PlaywrightTimeoutError as e:
                logger.warning(f"页面渲染与交互超时: {str(e)}")
            except PlaywrightError as e:
                logger.warning(f"浏览器进程通讯异常: {str(e)}")
                
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"抓取管线发生非预期异常: {str(e)}")
        finally:
            if page:
                try: await asyncio.wait_for(page.close(), timeout=2.0)
                except: pass
            if context:
                try: await asyncio.wait_for(context.close(), timeout=2.0)
                except: pass
                
        return valid_urls[:target_count], error_msg
