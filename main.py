# -*- coding: utf-8 -*-
import asyncio
import io
import aiohttp
from playwright.async_api import async_playwright

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp
from astrbot.api import logger

@register("astrbot_plugin_soutushenqi", "YourName", "搜图神器插件：支持详细报错输出与手机端伪装", "v1.0.0")
class SouTuShenQiPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    async def _get_image_url_from_web(self, keyword: str) -> tuple[str, str]:
        """
        核心逻辑：使用带伪装的 Playwright 抓取。
        返回 tuple: (高清图URL, 错误信息详情)
        """
        async with async_playwright() as p:
            # 【反反爬升级】启动无头浏览器，伪装特征
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-setuid-sandbox'
                ]
            )
            
            # 使用手机设备的 User-Agent 往往能绕过很多风控，且页面更简单
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
                viewport={'width': 390, 'height': 844}
            )
            
            page = await context.new_page()
            
            # 注入脚本，进一步隐藏自动控制特征
            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            
            error_msg = ""
            hd_url = ""
            
            try:
                search_url = f"https://www.soutushenqi.com/image/search?searchWord={keyword}"
                await page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
                
                # 暴力下拉到底部，连续拉3次，彻底触发懒加载
                for _ in range(3):
                    await page.wait_for_timeout(1000)
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                
                await page.wait_for_timeout(2000)

                # 暴力提取页面所有图片
                img_urls = await page.evaluate('''() => {
                    const imgs = Array.from(document.querySelectorAll("img"));
                    return imgs.map(img => {
                        return img.getAttribute("data-src") || img.getAttribute("src") || img.src;
                    }).filter(url => url && url.startsWith("http"));
                }''')
                
                # 最严格的白名单：只要外链壁纸
                valid_urls = [
                    src for src in img_urls 
                    if 'soutushenqi.com' not in src 
                    and 'avatar' not in src 
                    and 'logo' not in src.lower()
                    and 'icon' not in src.lower()
                    and '.png' not in src.lower()
                    and '.gif' not in src.lower()
                ]

                if not valid_urls:
                    # 获取当前页面的纯文本，用于 QQ 输出诊断
                    page_text = await page.evaluate("document.body.innerText")
                    error_msg = f"未找到壁纸。\n[页面前100个字符]: {page_text[:100]}\n[提取到的原始链接(前5个)]: {img_urls[:5]}"
                    logger.warning(f"搜图页面异常: {error_msg}")
                    return "", error_msg

                # 拿到第一张，并去掉压缩后缀还原大图 (例如 @1192w.webp)
                raw_url = valid_urls[0]
                hd_url = raw_url.split('@')[0]
                logger.info(f"【成功提取！】高清原图链接: {hd_url}")
                
            except Exception as e:
                error_msg = f"Playwright 发生异常: {str(e)}"
                logger.error(error_msg)
            finally:
                await browser.close()
                
            return hd_url, error_msg

    async def _download_to_memory(self, url: str) -> bytes:
        """
        将图片下载到内存中，不占用服务器硬盘空间
        """
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
        """
        用户手动调用的指令：/搜图 [关键词]
        """
        yield event.plain_result(f"🔍 正在前往搜图神器寻找【{keyword}】的高清图片，请稍等片刻...")
        
        # 获取图片 URL 和详细报错
        hd_url, err_msg = await self._get_image_url_from_web(keyword)
        
        if not hd_url:
            # 直接在 QQ 输出诊断信息
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
