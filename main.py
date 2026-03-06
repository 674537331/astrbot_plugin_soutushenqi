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

@register("astrbot_plugin_soutushenqi", "YourName", "搜图神器插件：正则降维打击版", "v1.0.0")
class SouTuShenQiPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    async def _get_image_url_from_web(self, keyword: str) -> tuple[str, str]:
        """
        核心逻辑：使用正则暴力匹配网页源码中的所有外链图片
        """
        async with async_playwright() as p:
            # 启动无头浏览器，伪装特征
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-setuid-sandbox'
                ]
            )
            
            # 换回电脑端 UA，电脑端返回的高清数据更完整
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
                    
                    # 获取网页当前的全部源码（包括所有动态生成的 JS 变量和 DOM）
                    html_content = await page.content()
                    
                    # 【核武器：正则表达式】
                    # 匹配所有常规 HTTP 链接，以及被 URL 编码的链接 (https%3A%2F%2F...)
                    raw_urls = re.findall(r'https?://[^"\'\s\\<>]+|https?%3A%2F%2F[^"\'\s\\<>&]+', html_content)
                    
                    for u in raw_urls:
                        # 如果是被编码的链接 (如 largeUrl=...)，将其解码还原
                        if '%3A%2F%2F' in u:
                            u = urllib.parse.unquote(u)
                            
                        if not u.startswith("http"): continue
                        if 'soutushenqi.com' in u: continue # 排除官方域名
                        
                        low_u = u.lower()
                        # 排除没用的图标
                        if 'avatar' in low_u or 'logo' in low_u or 'icon' in low_u or 'qrcode' in low_u: 
                            continue
                            
                        # 最严格的白名单：必须带有主流图片格式的后缀
                        if not any(ext in low_u for ext in ['.jpg', '.jpeg', '.png', '.webp']):
                            continue
                            
                        valid_urls.append(u)
                        
                    # 只要抓到了有效的第三方壁纸，立刻跳出循环，不再死等！
                    if valid_urls:
                        break

                if not valid_urls:
                    error_msg = f"未找到壁纸。\n[尝试次数]: {attempt+1}\n[源码长度]: {len(html_content)}\n[提取到的正则链接(前5个)]: {raw_urls[:5] if 'raw_urls' in locals() else '无'}"
                    logger.warning(f"搜图页面异常: {error_msg}")
                    return "", error_msg

                # 拿到第一张图，并切掉 B站等图床可能带有的缩略图参数（@1192w.webp）
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
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.soutushenqi.com/"
        }
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=20) as resp:
                    if resp.status == 200:
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
            yield event.plain_result(f"图片提取成功，但下载超时或被拦截：\n{hd_url}")

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
            yield event.plain_result(f"获取到了图片链接，但下载失败: {hd_url}")
