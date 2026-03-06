# -*- coding: utf-8 -*-
"""
数据抓取模块
负责通过无头浏览器加载目标网页，并使用正则表达式提取图片外链。
"""
import re
import urllib.parse
from playwright.async_api import async_playwright
import logging

logger = logging.getLogger("astrbot")

async def fetch_image_urls(keyword: str, target_count: int) -> tuple[list[str], str]:
    """
    抓取指定数量的图片URL。
    
    Args:
        keyword (str): 搜索关键词
        target_count (int): 期望获取的最大图片数量
        
    Returns:
        tuple[list[str], str]: (有效图片URL列表, 错误信息详情)
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
            
            # 执行滚动操作以触发懒加载
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
                    
                    low_u = u.lower()
                    if any(x in low_u for x in ['avatar', 'logo', 'icon', 'qrcode']):
                        continue
                    if not any(ext in low_u for ext in ['.jpg', '.jpeg', '.png', '.webp']):
                        continue
                        
                    # 去除第三方图床可能带有的压缩参数
                    clean_url = u.split('@')[0]
                    if clean_url not in valid_urls:
                        valid_urls.append(clean_url)
                        
                    if len(valid_urls) >= target_count:
                        break
                        
                if len(valid_urls) >= target_count:
                    break

            if not valid_urls:
                error_msg = "正则扫描未能匹配到符合白名单规则的第三方图片URL。"
                
        except Exception as e:
            error_msg = f"Playwright 抓取异常: {str(e)}"
        finally:
            await browser.close()
            
    return valid_urls[:target_count], error_msg
