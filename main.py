# -*- coding: utf-8 -*-
import asyncio
import io
import aiohttp
from playwright.async_api import async_playwright

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp
from astrbot.api import logger

@register("astrbot_plugin_soutushenqi", "YourName", "搜图神器无头浏览器插件，支持自然语言调用", "v1.0.0")
class SouTuShenQiPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    async def _get_image_url_from_web(self, keyword: str) -> str:
        """
        核心逻辑：使用 Playwright 访问搜索结果，并直接从超链接参数中截获高清原图直链
        """
        async with async_playwright() as p:
            # 启动无头浏览器 (headless=True 确保在后台静默运行)
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            
            try:
                # 访问搜索结果页
                search_url = f"https://www.soutushenqi.com/image/search?searchWord={keyword}"
                await page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
                
                # 【绝杀策略】
                # 我们不再傻等 <img> 标签加载完毕，而是直接寻找带有 'largeUrl' 参数的超链接 <a>
                # 这 100% 是真实的搜索结果，且速度极快
                await page.wait_for_selector('a[href*="largeUrl="]', timeout=15000)
                
                # 利用 JS 直接从 a 标签的 href 中解析出 largeUrl 的值（也就是高清原图直链）
                hd_urls = await page.evaluate('''() => {
                    const links = Array.from(document.querySelectorAll('a[href*="largeUrl="]'));
                    return links.map(a => {
                        try {
                            // 使用 URL 对象自动解析并解码 URL 参数 (例如将 %3A%2F%2F 还原为 ://)
                            const urlObj = new URL(a.href, window.location.origin);
                            return urlObj.searchParams.get("largeUrl");
                        } catch (e) {
                            return null;
                        }
                    }).filter(url => url && url.startsWith("http"));
                }''')
                
                if not hd_urls:
                    logger.warning("页面加载成功，但未能从超链接中提取到 largeUrl 参数。")
                    return ""
                    
                # 拿到第一张原图
                hd_url = hd_urls[0]
                
                logger.info(f"【完美破局】直接从链接参数中截获高清原图: {hd_url}")
                return hd_url
                
            except Exception as e:
                logger.error(f"Playwright 抓取异常: {e}")
                return ""
            finally:
                await browser.close()

    async def _download_to_memory(self, url: str) -> bytes:
        """
        将图片下载到内存中，不占用服务器硬盘空间
        """
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.soutushenqi.com/"
        }
        try:
            # 增加超时时间以应对超大高清图的下载
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
        """
        用户手动调用的指令：/搜图 [关键词]
        """
        yield event.plain_result(f"🔍 正在前往搜图神器寻找【{keyword}】的高清图片，请稍等片刻...")
        
        # 1. 极速获取图片 URL
        hd_url = await self._get_image_url_from_web(keyword)
        if not hd_url:
            yield event.plain_result("😭 抱歉，没有找到相关图片或请求超时。")
            return
            
        # 2. 内存下载并发送
        img_bytes = await self._download_to_memory(hd_url)
        if img_bytes:
            yield event.chain_result([Comp.Image.fromBytes(img_bytes)])
        else:
            yield event.plain_result(f"图片下载失败，可能因原图体积过大或源站防盗链拦截。你可以直接点击链接查看原图：\n{hd_url}")

    @filter.llm_tool(name="search_image_tool")
    async def tool_search_image(self, event: AstrMessageEvent, keyword: str):
        """
        根据用户的视觉需求，在网络上搜索一张最匹配的高清图片并发送。

        Args:
            keyword(string): 必需参数。搜索关键词，例如“赛博朋克 城市”、“可爱 猫咪”、“BMPT坦克”等。
        """
        logger.info(f"大模型触发搜图工具，关键词: {keyword}")
        
        hd_url = await self._get_image_url_from_web(keyword)
        if not hd_url:
            yield event.plain_result(f"搜索图片失败，请告知用户没有找到关于“{keyword}”的图片。")
            return
            
        img_bytes = await self._download_to_memory(hd_url)
        if img_bytes:
            yield event.chain_result([Comp.Image.fromBytes(img_bytes)])
        else:
            yield event.plain_result(f"已获取到图片链接，但下载失败: {hd_url}")
