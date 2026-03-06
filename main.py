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
        核心逻辑：使用 Playwright 后台打开网页，搜索并抓取第一张高清图链接
        """
        async with async_playwright() as p:
            # 启动无头浏览器 (不显示界面)
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            
            try:
                # 访问搜索结果页
                search_url = f"https://www.soutushenqi.com/image/search?searchWord={keyword}"
                
                # 【重要修复】将 wait_until 从 "networkidle" 改为 "domcontentloaded" 避免死等超时
                await page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
                
                # 【重要修复】增加图片加载的等待超时时间到 15000 毫秒
                await page.wait_for_selector('img', timeout=15000)
                
                # 为了确保 Vue/React 等前端框架把图片渲染出来，稍微硬等待 1.5 秒
                await page.wait_for_timeout(1500)
                
                # 执行 JS 获取所有图片的 src
                img_srcs = await page.eval_on_selector_all(
                    'img', 
                    'imgs => imgs.map(img => img.src).filter(src => src.startsWith("http"))'
                )
                
                # 过滤掉头像、logo、占位图等无用小图，提取真正的壁纸图
                valid_urls = [
                    src for src in img_srcs 
                    if 'soutushenqi.com' not in src 
                    and 'avatar' not in src 
                    and 'logo' not in src.lower()
                ]
                
                if not valid_urls:
                    logger.warning(f"页面加载成功，但未提取到有效的图片URL。获取到的全部 img src: {img_srcs[:5]}")
                    return ""
                    
                # 拿到第一张有效图，并去除缩略图后缀 (例如 @1192w.webp) 还原高清大图
                raw_url = valid_urls[0]
                hd_url = raw_url.split('@')[0]
                
                logger.info(f"成功获取高清原图链接: {hd_url}")
                return hd_url
                
            except Exception as e:
                logger.error(f"Playwright 抓取失败: {e}")
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
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=10) as resp:
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
        
        # 1. 获取图片 URL
        hd_url = await self._get_image_url_from_web(keyword)
        if not hd_url:
            yield event.plain_result("😭 抱歉，没有找到相关图片或请求超时。")
            return
            
        # 2. 内存下载并发送
        img_bytes = await self._download_to_memory(hd_url)
        if img_bytes:
            yield event.chain_result([Comp.Image.fromBytes(img_bytes)])
        else:
            yield event.plain_result(f"图片下载失败，但你可以直接点击链接查看原图：\n{hd_url}")

    @filter.llm_tool(name="search_image_tool")
    async def tool_search_image(self, event: AstrMessageEvent, keyword: str):
        """
        根据用户的视觉需求，搜索一张最匹配的高清图片。

        Args:
            keyword(string): 必需参数。搜索关键词，例如“赛博朋克 城市”、“可爱 猫咪”、“BMPT坦克”等。
        """
        logger.info(f"大模型触发搜图工具，关键词: {keyword}")
        
        hd_url = await self._get_image_url_from_web(keyword)
        if not hd_url:
            yield event.plain_result("搜索图片失败，请告知用户没有找到图片。")
            return
            
        img_bytes = await self._download_to_memory(hd_url)
        if img_bytes:
            yield event.chain_result([Comp.Image.fromBytes(img_bytes)])
        else:
            yield event.plain_result(f"获取到了图片链接: {hd_url}")
