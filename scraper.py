# -*- coding: utf-8 -*-
"""
数据抓取模块
负责通过无头浏览器加载目标网页，并使用正则表达式提取图片外链。
新增：Bing 必应图片搜索兜底机制。
"""
import re
import json
import urllib.parse
import aiohttp
from playwright.async_api import async_playwright
import logging

logger = logging.getLogger("astrbot")

async def fetch_bing_image_urls(keyword: str, target_count: int) -> list[str]:
    """
    使用 aiohttp 抓取 Bing 图片作为无头浏览器的兜底方案。
    Bing 的图片直链隐藏在源码的 m="{...}" JSON 字符串中。
    """
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
                    # 正则匹配 Bing 的图片数据块
                    matches = re.findall(r'm=\'({.*?})\'', html)
                    for match in matches:
                        try:
                            data = json.loads(match)
                            img_url = data.get("murl")
                            if img_url and img_url.startswith("http"):
                                # 基础过滤
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
    """
    主抓取逻辑：先尝试搜图神器（严禁百度），若无结果则自动降级到 Bing 搜图。
    """
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
            await page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
            
            for _ in range(4):
                await page.wait_for_timeout(1500)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                
                html_content = await page.content()
                raw_urls = re.findall(r'https?://[^"\'\s\\<>]+|https?%3A%2F%2F[^"\'\s\\<>&]+', html_content)
                
                for u in raw_urls:
                    if '%3A%2F%2F' in u:
                        u = urllib.parse.unquote(u)
                        
                    if not u.startswith("http"): continue
                    if 'soutushenqi.com' in u: continue
                    
                    # 【核心策略】：遇到百度系图床，直接判死刑，坚决不抓
                    if 'baidu.com' in u or 'bdimg.com' in u or 'bdstatic.com' in u:
                        continue
                    
                    low_u = u.lower()
                    if any(x in low_u for x in ['avatar', 'logo', 'icon', 'qrcode']):
                        continue
                        
                    # 严防死守奇怪的后缀
                    if not any(u.endswith(ext) or f"{ext}?" in u for ext in ['.jpg', '.jpeg', '.png', '.webp']):
                        continue
                        
                    clean_url = u.split('@')[0]
                    if clean_url not in valid_urls:
                        valid_urls.append(clean_url)
                        
                    if len(valid_urls) >= target_count:
                        break
                        
                if len(valid_urls) >= target_count:
                    break
                    
        except Exception as e:
            logger.warning(f"搜图神器 Playwright 抓取异常: {str(e)}")
        finally:
            await browser.close()
            
    # 【终极兜底策略】：如果搜图神器里抛去百度后，一张图都没剩下，立刻切换 Bing 搜图！
    if not valid_urls:
        logger.warning(f"搜图神器未能找到 [{keyword}] 的有效非百度图片，自动触发 Bing 搜索兜底...")
        valid_urls = await fetch_bing_image_urls(keyword, target_count)
        
        if not valid_urls:
            error_msg = "搜图神器与 Bing 兜底搜索均未能找到可用图片。"
        else:
            logger.info(f"Bing 兜底成功，抓取到 {len(valid_urls)} 张候选图片。")
            
    return valid_urls[:target_count], error_msg
