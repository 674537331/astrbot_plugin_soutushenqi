# -*- coding: utf-8 -*-
import io
import json
import asyncio
import hashlib
from PIL import Image, UnidentifiedImageError
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest
import astrbot.api.message_components as Comp
from astrbot.api import logger

from .scraper import fetch_image_urls, fetch_bing_image_urls, close_browser, close_scraper_session
from .composer import download_image_batch, create_collage_from_items, close_composer_session
from .vlm import select_best_image_index

SUPPLEMENT_THRESHOLD_RATIO = 0.3
JPEG_QUALITY = 85
MAX_BATCH_SIZE = 36  

TOOL_INSTRUCTION = (
    "\n【🔴 致命红线警告：搜图行为规范 🔴】\n"
    "当用户要求搜图、找图、看图时，你【必须直接且仅使用】名为 `search_image_tool` 的 Function Tool。\n"
    "【绝对禁止以下违规行为】：\n"
    "1. 严禁使用 `astrbot_execute_ipython` 写代码搜图！\n"
    "2. 严禁使用 `astrbot_execute_shell` 搜图！\n"
    "3. 严禁你自己捏造或输出带有 [CQ:image,file=...] 或 Markdown 的虚假链接！\n"
    "你只需要在后台调用 `search_image_tool` 工具即可。"
)

@register("astrbot_plugin_soutushenqi", "RyanVaderAn", "智能搜图与比对插件(究极版)", "v6.2.0")
class SouTuShenQiPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config

    async def terminate(self):
        await close_browser()
        await close_scraper_session()
        await close_composer_session()
        logger.info("SouTuShenQi 插件资源回收完毕。")

    async def _get_vlm_provider(self, event: AstrMessageEvent):
        provider_id = self.config.get("vlm_provider_id", "")
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider:
                return provider
        
        umo = getattr(event, "unified_msg_origin", None)
        if umo:
            curr_id = await self.context.get_current_chat_provider_id(umo)
            if curr_id:
                provider = self.context.get_provider_by_id(curr_id)
                if provider:
                    return provider
                
        return getattr(self.context, 'llm', None)

    def _compute_image_hash(self, img_bytes: bytes) -> str:
        try:
            with Image.open(io.BytesIO(img_bytes)) as img:
                img = img.convert('L').resize((8, 8), Image.Resampling.LANCZOS)
                pixels = list(img.getdata())
                avg = sum(pixels) / len(pixels)
                bits = "".join(['1' if p > avg else '0' for p in pixels])
                return hex(int(bits, 2))[2:].zfill(16)
        except Exception:
            return hashlib.md5(img_bytes).hexdigest()

    def _calculate_and_dedup_sync(
        self, items: list[tuple[str, bytes]], bing_items: list[tuple[str, bytes]]
    ) -> list[tuple[str, bytes]]:
        seen_urls = {u for u, _ in items}
        seen_hashes = {self._compute_image_hash(b) for _, b in items}
        
        new_bing_items = []
        for u, b in bing_items:
            if u not in seen_urls:
                b_hash = self._compute_image_hash(b)
                if b_hash not in seen_hashes:
                    new_bing_items.append((u, b))
                    seen_hashes.add(b_hash)
        return new_bing_items

    async def _ensure_minimum_images(self, keyword: str, batch_size: int) -> list[tuple[str, bytes]]:
        threshold = batch_size * SUPPLEMENT_THRESHOLD_RATIO  
        
        urls, _ = await fetch_image_urls(keyword, batch_size)
        items = await download_image_batch(urls)
        logger.info(f"主来源下载完成，存活 {len(items)} 张。")

        if len(items) < threshold:
            logger.warning(f"主图源可用率低，启动 Bing 混合补充...")
            bing_urls = await fetch_bing_image_urls(keyword, batch_size)
            bing_items = await download_image_batch(bing_urls)
            
            loop = asyncio.get_running_loop()
            new_bing_items = await loop.run_in_executor(
                None, self._calculate_and_dedup_sync, items, bing_items
            )
            
            items = (items + new_bing_items)[:batch_size]
            logger.info(f"混合补充完毕，最终参与比对数: {len(items)}")
            
        return items

    async def _vlm_selection(
        self, event: AstrMessageEvent, items: list[tuple[str, bytes]], eval_desc: str
    ) -> tuple[str, bytes, str]:
        collage_bytes, valid_items = await create_collage_from_items(items)
        if not collage_bytes or not valid_items:
            return "", b"", "图片拼合处理失败，可用图片的数据均已损坏。"
            
        vlm_provider = await self._get_vlm_provider(event)
        if vlm_provider:
            logger.info(f"开始大模型淘汰比对 ({len(valid_items)} 选 1)...")
            best_idx = await select_best_image_index(vlm_provider, collage_bytes, eval_desc, len(valid_items))
            
            if best_idx == -1:
                return "", b"", "检索到的图片均与要求无关，为保证质量已拦截。"
                
            final_url, final_bytes = valid_items[best_idx]
            logger.info(f"VLM优胜决定：{final_url}")
            return final_url, final_bytes, ""
        else:
            return valid_items[0][0], valid_items[0][1], ""

    def _format_image_sync(self, img_bytes: bytes) -> bytes:
        try:
            with io.BytesIO(img_bytes) as img_io:
                img = Image.open(img_io)
                if img.format not in ['JPEG', 'PNG']:
                    if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                        try:
                            img = img.convert('RGBA')
                            bg = Image.new("RGB", img.size, (255, 255, 255))
                            bg.paste(img, mask=img.split()[-1])
                            img = bg
                        except Exception as alpha_e:
                            logger.debug(f"Alpha 通道复合失败，降级转换: {alpha_e}")
                            img = img.convert("RGB")
                    else:
                        img = img.convert("RGB")
                        
                    with io.BytesIO() as buf:
                        img.save(buf, format="JPEG", quality=JPEG_QUALITY)
                        final_bytes = buf.getvalue()
                    return final_bytes
                return img_bytes
        except UnidentifiedImageError:
            logger.warning("捕获到 UnidentifiedImageError，图片文件损坏。")
            return img_bytes
        except OSError as e:
            logger.warning(f"图片转码发生IO格式错误: {e}")
            return img_bytes
        except Exception as e:
            logger.error(f"图片转码发生严重错误: {e}", exc_info=True)
            return img_bytes

    async def _format_image(self, img_bytes: bytes) -> bytes:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._format_image_sync, img_bytes)

    async def _process_image_search(
        self, event: AstrMessageEvent, keyword: str, description: str, use_vlm_selection: bool
    ) -> tuple[bytes | None, str]:
        batch_size = min(self.config.get("batch_size", 16), MAX_BATCH_SIZE)
        eval_desc = description if description else keyword
        logger.info(f"发起搜图: [{keyword}], VLM比对: {use_vlm_selection}")
        
        items = await self._ensure_minimum_images(keyword, batch_size)
        if not items:
            return None, "所有的图片渠道均触发强力防盗链或失效，无一可用。"

        final_bytes = b""
        if use_vlm_selection and len(items) > 1:
            _, final_bytes, err_msg = await self._vlm_selection(event, items, eval_desc)
            if not final_bytes:
                return None, err_msg
        else:
            final_url, final_bytes = items[0]
            logger.info(f"跳过VLM，直接返回首张图：{final_url}")

        final_bytes = await self._format_image(final_bytes)
        return final_bytes, ""

    @filter.command("搜图")
    async def cmd_search_image(self, event: AstrMessageEvent, keyword: str, description: str = ""):
        try:
            use_vlm = self.config.get("enable_cmd_vlm_selection", True)
            yield event.plain_result(f"正在处理搜图请求 [{keyword}]...")
            
            img_bytes, err_msg = await self._process_image_search(event, keyword, description, use_vlm)
            if img_bytes:
                yield event.chain_result([Comp.Image.fromBytes(img_bytes)])
            else:
                yield event.plain_result(f"搜图失败: {err_msg}")
        except Exception as e:
            logger.error(f"指令搜图管线崩溃: {e}", exc_info=True)
            yield event.plain_result(f"抱歉，搜图执行期间发生系统错误: {str(e)}")

    # 🚀 终极修复：使用 AstrBot 框架最标准、解析通过率 100% 的注释格式 🚀
    @filter.llm_tool(name="search_image_tool")
    async def tool_search_image(
        self, event: AstrMessageEvent, keyword: str, description: str = "", is_explanation: bool = False
    ):
        """搜索网络上的高清图片、壁纸、照片并发送给用户。
        
        :param keyword: 具体的搜索关键词，简练精准。
        :param description: 对期望图片的详细视觉描述。用于大模型智能筛选最符合的图片。
        :param is_explanation: 若用户要求科普或询问时，设为true。
        """
        try:
            if is_explanation:
                use_vlm = self.config.get("enable_explanation_vlm_selection", False)
            else:
                use_vlm = self.config.get("enable_nl_search_vlm_selection", True)
                
            img_bytes, err_msg = await self._process_image_search(event, keyword, description, use_vlm)
            
            if img_bytes:
                message_result = event.make_result()
                message_result.chain = [Comp.Image.fromBytes(img_bytes)]
                await event.send(message_result) 
                
                if is_explanation:
                    return f"图片已成功发送！请立刻开始向用户详细解释什么是 {keyword}。"
                else:
                    return "图片已发送！简单回复一句搜图完成的话语即可。"
            else:
                return f"系统搜图失败，原因：{err_msg}。请向用户说明情况。"
        except Exception as e:
            logger.error(f"工具搜图管线崩溃: {e}", exc_info=True)
            return f"发生系统错误导致搜图中断：{str(e)}。请向用户致歉。"

    @filter.on_llm_request()
    async def inject_explanation_instruction(self, event: AstrMessageEvent, req: ProviderRequest):
        if self.config.get("enable_explanation_image", True):
            if TOOL_INSTRUCTION not in req.system_prompt:
                req.system_prompt += TOOL_INSTRUCTION
