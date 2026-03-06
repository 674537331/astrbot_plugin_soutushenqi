# -*- coding: utf-8 -*-
import re
import json
import urllib.parse
import asyncio
import requests
import random
import urllib3
from playwright.async_api import async_playwright
from astrbot.api import logger

# 禁用不安全请求警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PLAYWRIGHT_TIMEOUT = 15000
SCROLL_TIMES = 4
SCROLL_WAIT = 1500
BLACKLIST_WORDS = ['avatar', 'logo', 'icon', 'qrcode', 'notice', 'placeholder', 'default', 'thumb', 'profile']

def _scrape_bing_sync(keyword: str, target_count: int) -> list[str]:
    """同步阻塞的 Bing 抓取逻辑，放入线程池运行"""
    search_url = "https://www.bing.com/images/search"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"
    }
    valid_urls = []
    seen_urls = set()
    
    # 尝试翻页抓取，确保数量管够
    first = 0
    max_pages = 3 
    
    with requests.Session() as session:
        session.verify = False # 核心修改：禁用 SSL 验证
        session.headers.update(headers)
        
        for _ in range(max_pages):
            try:
                params = {"q": keyword, "first": first, "adlt": "off"} # 关闭安全搜索
                resp = session.get(search_url, params=params, timeout=15)
                resp.raise_for_status()
                
                # 使用正则提取 murl (高清图地址)
                matches = re.findall(r'm=\'({.*?})\'', resp.text)
                new_found = 0
                for match in matches:
                    try:
                        data = json.loads(match)
                        img_url = data.get("murl")
                        if img_url and img_url.startswith("http") and img_url not in seen_urls:
                            low_u = img_url.lower()
                            if any(x in low_u for x in BLACKLIST_WORDS):
                                continue
                            valid_urls.append(img_url)
                            seen_urls.add(img_url)
                            new_found += 1
                            if len(valid_urls) >= target_count:
                                return valid_urls
                    except:
                        continue
                
                if new_found == 0: break
                first += 35 # 翻页
                
            except Exception as e:
                logger.error(f"Bing 同步抓取异常: {e}")
                break
    return valid_urls

async def fetch_bing_image_urls(keyword: str, target_count: int) -> list[str]:
    """异步包装：利用线程池执行同步请求，解决网络连不上的玄学问题"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _scrape_bing_sync, keyword, target_count)

async def fetch_image_urls(keyword: str, target_count: int) -> tuple[list[str], str]:
    """主抓取逻辑（Playwright 版保持不变，负责攻坚）"""
    valid_urls = []
    error_msg = ""
    browser = None 
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
            context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
            page = await context.new_page()
            search_url = f"https://www.soutushenqi.com/image/search?searchWord={urllib.parse.quote(keyword)}"
            await page.goto(search_url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT)
            
            for _ in range(SCROLL_TIMES):
                await page.wait_for_timeout(SCROLL_WAIT)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                html_content = await page.content()
                raw_urls = re.findall(r'https?://[^"\'\s\\<>]+|https?%3A%2F%2F[^"\'\s\\<>&]+', html_content)
                for u in raw_urls:
                    if '%3A%2F%2F' in u: u = urllib.parse.unquote(u)
                    if not u.startswith("http") or 'soutushenqi.com' in u: continue
                    if 'baidu.com' in u or 'bdimg.com' in u: continue
                    low_u = u.lower()
                    if any(x in low_u for x in BLACKLIST_WORDS): continue
                    if not any(u.endswith(ext) or f"{ext}?" in u for ext in ['.jpg', '.jpeg', '.png', '.webp']): continue
                    clean_url = u.split('@')[0].rstrip('.,;)')
                    if clean_url not in valid_urls:
                        valid_urls.append(clean_url)
                    if len(valid_urls) >= target_count: break
                if len(valid_urls) >= target_count: break
        except Exception as e:
            logger.warning(f"主源 Playwright 异常: {str(e)}")
        finally:
            if browser: await browser.close()
    return valid_urls[:target_count], error_msg
