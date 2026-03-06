# -*- coding: utf-8 -*-
import asyncio
import io
import re
import urllib.parse
import aiohttp
from playwright.async_api import async_playwright

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp
from astrbot.api import logger

@register("astrbot_plugin_soutushenqi", "YourName", "搜图神器插件：正则降维打击 + 防盗链校验版", "v1.0.0")
class SouTuShenQiPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    async def _get_image_url_from_web(self, keyword: str) -> tuple[str, str]:
        """
        核心逻辑：使用正则暴力匹配网页源码中的所有外链图片
        """
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
            
            error_msg = ""
            hd_url = ""
            
            try:
                search_url = f"https://www.soutushenqi.com/image/search?searchWord={keyword}"
                await page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
                
                valid_urls = []
                html_content = ""
                
                # 循环最多 4 次，边滚动边用正则扫描网页源码
                for attempt in range(4):
                    await page.wait_for_timeout(2000)
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    
                    html_content = await page.content()
                    
                    raw_urls = re.findall(r'https?://[^"\'\s\\<>]+|https?%3A%2F%2F[^"\'\s\\<>&]+', html_content)
                    
                    for u in raw_urls:
                        if '%3A%2F%2F' in u:
                            u = urllib.parse.unquote(u)
                            
                        if not u.startswith("http"): continue
                        if 'soutushenqi.com' in u: continue
                        
                        low_u = u.lower()
                        if 'avatar' in low_u or 'logo' in low_u or 'icon' in low_u or 'qrcode' in low_u: 
                            continue
                            
                        if not any(ext in low_u for ext in ['.jpg', '.jpeg', '.png', '.webp']):
                            continue
                            
                        valid_urls.append(u)
                        
                    if valid_urls:
                        break

                if not valid_urls:
                    error_msg = f"未找到壁纸。\n[提取到的正则链接(前5个)]: {raw_urls[:5] if 'raw_urls' in locals() else '无'}"
                    logger.warning(f"搜图页面异常: {error_msg}")
                    return "", error_msg

                raw_url = valid_urls[0]
                hd_url = raw_url.split('@')[0]
                logger.info(f"【降维打击成功！】高清原图链接: {hd_url}")
                
            except Exception as e:
                error_msg = f"Playwright 发生异常: {str(e)}"
                logger.error(error_msg)
            finally:
                await browser.close()
                
            return hd_url, error_msg

    async def _download_to_memory(self, url: str) -> bytes:
        # 【极其关键】删除了 Referer，直接以浏览器身份裸请求，防止触发源站防盗链
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=20) as resp:
                    if resp.status == 200:
                        # 【安全校验】检查返回的内容到底是不是图片！如果是网页HTML，直接拒绝！
                        content_type = resp.headers.get('Content-Type', '').lower()
                        if 'text/html' in content_type:
                            logger.error(f"图片下载失败：源站启动了防盗链，返回了HTML网页！")
                            return None
                        return await resp.read()
                    else:
                        logger.error(f"图片下载失败，HTTP 状态码: {resp.status}")
        except Exception as e:
            logger.error(f"图片内存下载异常: {e}")
        return None

    @filter.command("搜图")
    async def cmd_search_image(self, event: AstrMessageEvent, keyword: str):
        yield event.plain_result(f"🔍 正在前往搜图神器寻找【{keyword}】的高清图片，请稍等片刻...")
        
        hd_url, err_msg = await self._get_image_url_from_web(keyword)
        
        if not hd_url:
            reply = f"😭 抱歉，抓取失败了！\n\n【诊断日志】\n{err_msg}"
            yield event.plain_result(reply)
            return
            
        img_bytes = await self._download_to_memory(hd_url)
        if img_bytes:
            yield event.chain_result([Comp.Image.fromBytes(img_bytes)])
        else:
            # 如果触发了极少数无法破解的防盗链，至少把链接发出来供用户点击
            yield event.plain_result(f"图片提取成功，但触发了源站防盗链无法直接发送。\n请直接点击链接查看原图：\n{hd_url}")

    @filter.llm_tool(name="search_image_tool")
    async def tool_search_image(self, event: AstrMessageEvent, keyword: str):
        """
        根据用户的视觉需求，在网络上搜索一张最匹配的高清图片并发送。

        Args:
            keyword(string): 必需参数。搜索关键词，例如“赛博朋克 城市”、“可爱 猫咪”等。
        """
        logger.info(f"大模型触发搜图工具，关键词: {keyword}")
        
        hd_url, err_msg = await self._get_image_url_from_web(keyword)
        if not hd_url:
            yield event.plain_result(f"搜索失败，错误信息: {err_msg}")
            return
            
        img_bytes = await self._download_to_memory(hd_url)
        if img_bytes:
            yield event.chain_result([Comp.Image.fromBytes(img_bytes)])
        else:
            yield event.plain_result(f"获取到了图片链接，但触发了防盗链无法下载: {hd_url}")
