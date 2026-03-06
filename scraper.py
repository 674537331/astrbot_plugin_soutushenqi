# -*- coding: utf-8 -*-
"""
数据抓取模块
新增：Bing 必应图片搜索兜底机制。清理了魔法数字，修复了正则标点问题，彻底拉黑百度图床。
"""
import re
import json
import urllib.parse
import aiohttp
from playwright.async_api import async_playwright
import logging

logger = logging.getLogger("astrbot")

# 抽离魔法常量，方便统一修改
PLAYWRIGHT_TIMEOUT = 15000
SCROLL_TIMES = 4
SCROLL_WAIT = 1500

async def fetch_bing_image_urls(keyword: str, target_count: int) -> list[str]:
    """使用 aiohttp 抓取 Bing 图片作为兜底。"""
    url = f"https://www.bing.com/images/search?q={urllib.parse.quote(keyword)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    valid_urls = []
    
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=15) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    matches = re.findall(r'm=\'({.*?})\'', html)
                    for match in matches:
                        try:
                            data = json.loads(match)
                            img_url = data.get("murl")
                            if img_url and img_url.startswith("http"):
                                low_u = img_url.lower()
                                if any(x in low_u for x in ['avatar', 'logo', 'icon']):
                                    continue
                                valid_urls.append(img_url)
                                if len(valid_urls) >= target_count:
                                    break
                        except json.JSONDecodeError:
                            continue
    except Exception as e:
        logger.error(f"Bing 兜底抓取发生异常: {e}")
        
    return valid_urls

async def fetch_image_urls(keyword: str, target_count: int) -> tuple[list[str], str]:
    """主抓取逻辑：优先尝试搜图神器（严禁百度）。"""
    valid_urls = []
    error_msg = ""
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-setuid-sandbox'
            ]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080}
        )
        page = await context.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        try:
            search_url = f"https://www.soutushenqi.com/image/search?searchWord={urllib.parse.quote(keyword)}"
            await page.goto(search_url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT)
            
            for _ in range(SCROLL_TIMES):
                await page.wait_for_timeout(SCROLL_WAIT)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                
                html_content = await page.content()
                raw_urls = re.findall(r'https?://[^"\'\s\\<>]+|https?%3A%2F%2F[^"\'\s\\<>&]+', html_content)
                
                for u in raw_urls:
                    if '%3A%2F%2F' in u:
                        u = urllib.parse.unquote(u)
                        
                    if not u.startswith("http"): continue
                    if 'soutushenqi.com' in u: continue
                    
                    # 彻底拉黑百度系毒瘤防盗链图床
                    if 'baidu.com' in u or 'bdimg.com' in u or 'bdstatic.com' in u:
                        continue
                    
                    low_u = u.lower()
                    if any(x in low_u for x in ['avatar', 'logo', 'icon', 'qrcode']):
                        continue
                        
                    if not any(u.endswith(ext) or f"{ext}?" in u for ext in ['.jpg', '.jpeg', '.png', '.webp']):
                        continue
                        
                    clean_url = u.split('@')[0]
                    # 清除可能误匹配的尾部标点符号，防止引发 404
                    clean_url = clean_url.rstrip('.,;)')
                    
                    if clean_url not in valid_urls:
                        valid_urls.append(clean_url)
                        
                    if len(valid_urls) >= target_count:
                        break
                        
                if len(valid_urls) >= target_count:
                    break

            if not valid_urls:
                error_msg = "未能匹配到符合白名单规则的高清第三方图片URL。"
                
        except Exception as e:
            logger.warning(f"搜图神器 Playwright 抓取异常: {str(e)}")
        finally:
            await browser.close()
            
    # 如果没抓到足够的图，也会在 main.py 的逻辑中自动由 Bing 补齐
    return valid_urls[:target_count], error_msg
