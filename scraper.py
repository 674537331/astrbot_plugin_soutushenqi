# -*- coding: utf-8 -*-
import re
import urllib.parse
import aiohttp
from playwright.async_api import async_playwright
import logging

logger = logging.getLogger("astrbot")

PLAYWRIGHT_TIMEOUT = 15000
SCROLL_TIMES = 4
SCROLL_WAIT = 1500

async def fetch_bing_image_urls(keyword: str, target_count: int) -> list[str]:
    """融合 PicSearch 优点：支持异步翻页的 Bing 强力抓取"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    valid_urls = []
    seen_urls = set()
    first = 0
    
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            # 加入翻页循环，保障能抓满 target_count
            while len(valid_urls) < target_count:
                url = f"https://www.bing.com/images/search?q={urllib.parse.quote(keyword)}&first={first}"
                async with session.get(url, timeout=15) as resp:
                    if resp.status != 200:
                        break
                        
                    html = await resp.text()
                    matches = re.findall(r'(?:"|&quot;)murl(?:"|&quot;)\s*:\s*(?:"|&quot;)(https?://.*?)(?:"|&quot;)', html)
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
                        break # 如果这一页没新图了，跳出循环防死循环
                    first += 35 # Bing 默认每页约 35 张
                    
    except Exception as e:
        logger.error(f"Bing 翻页兜底抓取发生异常: {e}")
        
    return valid_urls

async def fetch_image_urls(keyword: str, target_count: int) -> tuple[list[str], str]:
    """保留强力 Playwright 主源防反爬逻辑"""
    valid_urls = []
    error_msg = ""
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox', '--disable-setuid-sandbox']
        )
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
                await page.wait_for_timeout(SCROLL_WAIT)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                
                html_content = await page.content()
                raw_urls = re.findall(r'https?://[^"\'\s\\<>]+|https?%3A%2F%2F[^"\'\s\\<>&]+', html_content)
                
                for u in raw_urls:
                    if '%3A%2F%2F' in u: u = urllib.parse.unquote(u)
                    if not u.startswith("http") or 'soutushenqi.com' in u: continue
                    if 'baidu.com' in u or 'bdimg.com' in u or 'bdstatic.com' in u: continue
                    
                    low_u = u.lower()
                    if any(x in low_u for x in ['avatar', 'logo', 'icon', 'qrcode']): continue
                    if not any(u.endswith(ext) or f"{ext}?" in u for ext in ['.jpg', '.jpeg', '.png', '.webp']): continue
                        
                    clean_url = u.split('@')[0].rstrip('.,;)')
                    if clean_url not in valid_urls:
                        valid_urls.append(clean_url)
                    if len(valid_urls) >= target_count: break
                if len(valid_urls) >= target_count: break

            if not valid_urls:
                error_msg = "未能匹配到符合白名单规则的高清第三方图片URL。"
                
        except Exception as e:
            logger.warning(f"搜图主源 Playwright 抓取异常: {str(e)}")
        finally:
            await browser.close()
            
    return valid_urls[:target_count], error_msg
