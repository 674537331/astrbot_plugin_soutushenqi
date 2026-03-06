# -*- coding: utf-8 -*-
import asyncio
import io
import aiohttp
from playwright.async_api import async_playwright

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp
from astrbot.api import logger

@register("astrbot_plugin_soutushenqi", "YourName", "搜图神器插件：支持详细报错输出", "v1.0.0")
class SouTuShenQiPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    async def _get_image_url_from_web(self, keyword: str) -> tuple[str, str]:
        """
        核心逻辑：使用带伪装的 Playwright 抓取。
        返回 tuple: (高清图URL, 错误信息详情)
        """
        async with async_playwright() as p:
            # 【反反爬升级】启动无头浏览器，但伪装得像个真人
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled', # 隐藏 webdriver 标记
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
                
                # 强制等 3 秒让数据加载
                await page.wait_for_timeout(3000)
                
                # 【诊断级排错】如果找不到 a 标签，我们把页面的真实 HTML 结构抓出来看看
                try:
                    await page.wait_for_selector('a[href*="largeUrl="]', timeout=8000)
                except Exception:
                    # 获取当前页面的纯文本和 <a> 标签的情况，用于 QQ 输出
                    page_text = await page.evaluate("document.body.innerText")
                    a_links = await page.evaluate("Array.from(document.querySelectorAll('a')).map(a => a.href).slice(0, 5)")
                    
                    error_msg = f"找不到 largeUrl 参数。\n[页面前100个字符]: {page_text[:100]}\n[抓到的前5个链接]: {a_links}"
                    logger.warning(f"搜图页面异常: {error_msg}")
                    return "", error_msg

                # 解析 URL
                hd_urls = await page.evaluate('''() => {
                    const links = Array.from(document.querySelectorAll('a[href*="largeUrl="]'));
                    return links.map(a => {
                        try {
                            const urlObj = new URL(a.href, window.location.origin);
                            return urlObj.searchParams.get("largeUrl");
                        } catch (e) {
                            return null;
                        }
                    }).filter(url => url && url.startsWith("http"));
                }''')
                
                if hd_urls:
                    hd_url = hd_urls[0]
                    
            except Exception as e:
                error_msg = f"Playwright 发生严重异常: {str(e)}"
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
        except Exception as e:
            logger.error(f"图片内存下载异常: {e}")
        return None

    @filter.command("搜图")
    async def cmd_search_image(self, event: AstrMessageEvent, keyword: str):
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
        hd_url, err_msg = await self._get_image_url_from_web(keyword)
        if not hd_url:
            yield event.plain_result(f"搜索失败，错误信息: {err_msg}")
            return
            
        img_bytes = await self._download_to_memory(hd_url)
        if img_bytes:
            yield event.chain_result([Comp.Image.fromBytes(img_bytes)])
        else:
            yield event.plain_result(f"获取到了图片链接: {hd_url}")
