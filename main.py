# -*- coding: utf-8 -*-
import io
import asyncio
from typing import Optional, List, Tuple, Callable

from PIL import Image, UnidentifiedImageError
from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest, Provider
import astrbot.api.message_components as Comp
from astrbot.api import logger

from .scraper import ScraperManager
from .composer import ComposerManager
from .vlm import select_best_image_index

JPEG_QUALITY = 85

@dataclass
class SearchImageFunctionTool(FunctionTool[AstrAgentContext]):
    name: str = "search_image_tool"
    description: str = (
        "当用户要求搜图、找图、看图，或者在解释明确实体时需要配图时，"
        "使用此工具搜索网络上的高清图片并发送给用户。"
        "必须提供 keyword 和 description。"
    )
    plugin_callback: Optional[Callable] = Field(default=None, exclude=True) 
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "初步搜索的精准关键词，用于搜索引擎查询。",
                },
                "description": {
                    "type": "string",
                    "description": "对期望图片的视觉描述。注意：只描述核心主体、角色特征或整体画风，绝对禁止凭空捏造具体的特定动作、罕见场景或细枝末节（如“在电车上”等），以免导致视觉筛选过于苛刻而失败。",
                },
                "is_explanation": {
                    "type": "boolean",
                    "description": "如果是因为解答明确实体（如介绍景点、动物等）而主动触发的配图，请设为 true。如果是用户明确要求搜图，设为 false。"
                }
            },
            "required": ["keyword", "description"]
        }
    )

    async def call(self, ctx: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        keyword = kwargs.get("keyword", "")
        description = kwargs.get("description", keyword)
        is_explanation = kwargs.get("is_explanation", False)
        event = ctx.context.event
        
        if not keyword:
            return "工具调用失败：缺少必需参数 keyword。"
            
        try:
            if is_explanation:
                await event.send(event.make_result().message(f"✨ 正在为您检索【{keyword}】的相关配图，请稍候..."))
            else:
                await event.send(event.make_result().message(f"⏳ 正在全网为您搜寻【{keyword}】的高清原图并进行 AI 视觉筛选，预计需要 20~40 秒，请稍候..."))
        except Exception as e:
            logger.debug(f"发送状态提示异常: {e}")
            
        if self.plugin_callback:
            return await self.plugin_callback(event, keyword, description, is_explanation)
        return "工具调用失败：插件实例回调未绑定。"

@register("astrbot_plugin_soutushenqi", "RyanVaderAN", "智能搜图与比对插件", "v2.0.0")
class SouTuShenQiPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        
        self.scraper_mgr = ScraperManager()
        self.composer_mgr = ComposerManager()
        self._vlm_semaphore = asyncio.Semaphore(2)
        
        tool = SearchImageFunctionTool()
        tool.plugin_callback = self._execute_tool 
        
        try:
            self.context.add_llm_tools(tool)
        except AttributeError:
            tool_mgr = self.context.provider_manager.llm_tools
            tool_mgr.func_list.append(tool)

    async def terminate(self):
        await self.scraper_mgr.close_all()
        await self.composer_mgr.close_all()
        logger.info("SouTuShenQi 插件资源回收完成。")

    async def _get_vlm_provider(self, event: AstrMessageEvent) -> Optional[Provider]:
        provider_id = self.config.get("vlm_provider_id", "")
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider: return provider
        
        umo = getattr(event, "unified_msg_origin", None)
        if umo:
            curr_id = await self.context.get_current_chat_provider_id(umo)
            if curr_id:
                provider = self.context.get_provider_by_id(curr_id)
                if provider: return provider
                
        return None

    def _validate_and_hash_sync(self, img_bytes: bytes, min_res: int) -> Tuple[bool, str]:
        try:
            with Image.open(io.BytesIO(img_bytes)) as img:
                if img.width < min_res or img.height < min_res:
                    return False, ""
                img = img.convert('L').resize((8, 8), Image.Resampling.LANCZOS)
                pixels = list(img.getdata())
                avg = sum(pixels) / len(pixels)
                bits = "".join(['1' if p > avg else '0' for p in pixels])
                return True, hex(int(bits, 2))[2:].zfill(16)
        except Exception:
            return False, ""

    async def _ensure_minimum_images(self, keyword: str) -> List[Tuple[str, bytes]]:
        try:
            raw_count = self.config.get("batch_size", 9)
            target_count = max(1, min(int(raw_count), 16))
        except (TypeError, ValueError):
            target_count = 9
            
        try:
            raw_res = self.config.get("min_resolution", 500)
            min_resolution = max(100, min(int(raw_res), 4000))
        except (TypeError, ValueError):
            min_resolution = 500
            
        valid_items = []
        seen_hashes = set()
        loop = asyncio.get_running_loop()

        urls, _ = await self.scraper_mgr.fetch_image_urls(keyword, target_count * 4)
        url_pool = urls.copy() if urls else []
        
        while url_pool and len(valid_items) < target_count:
            needed = target_count - len(valid_items)
            batch_size = min(len(url_pool), max(needed, needed * 2)) 
            
            batch_urls = url_pool[:batch_size]
            url_pool = url_pool[batch_size:] 
            
            downloaded = await self.composer_mgr.download_image_batch(batch_urls, target_count=len(batch_urls))
            
            for url, img_bytes in downloaded:
                if len(valid_items) >= target_count: break 
                is_valid, b_hash = await loop.run_in_executor(None, self._validate_and_hash_sync, img_bytes, min_resolution)
                if is_valid and b_hash not in seen_hashes:
                    valid_items.append((url, img_bytes))
                    seen_hashes.add(b_hash)
                    
        logger.info(f"主图源处理完毕，当前高清去重有效图片数: {len(valid_items)}")

        if len(valid_items) < target_count:
            bing_urls = await self.scraper_mgr.fetch_bing_image_urls(keyword, target_count * 3)
            bing_pool = bing_urls.copy() if bing_urls else []
            
            while bing_pool and len(valid_items) < target_count:
                needed = target_count - len(valid_items)
                batch_size = min(len(bing_pool), max(needed, needed * 2))
                
                batch_urls = bing_pool[:batch_size]
                bing_pool = bing_pool[batch_size:]
                
                bing_dl = await self.composer_mgr.download_image_batch(batch_urls, target_count=len(batch_urls))
                for url, img_bytes in bing_dl:
                    if len(valid_items) >= target_count: break
                    is_valid, b_hash = await loop.run_in_executor(None, self._validate_and_hash_sync, img_bytes, min_resolution)
                    if is_valid and b_hash not in seen_hashes:
                        valid_items.append((url, img_bytes))
                        seen_hashes.add(b_hash)
                        
            logger.info(f"Bing 补充处理完毕，当前高清有效图片数: {len(valid_items)}")

        return valid_items[:target_count]

    async def _vlm_selection(self, event: AstrMessageEvent, items: List[Tuple[str, bytes]], eval_desc: str) -> Tuple[str, bytes, str, bool]:
        collage_bytes, valid_items = await self.composer_mgr.create_collage_from_items(items)
        if not collage_bytes or not valid_items:
            return "", b"", "图像组合处理失败，候选数据损坏。", False
            
        vlm_provider = await self._get_vlm_provider(event)
        if vlm_provider:
            async with self._vlm_semaphore:
                best_idx = await select_best_image_index(vlm_provider, collage_bytes, eval_desc, len(valid_items))
            
            # 🌟 修复魔法数字重载：明确区分拒绝(-1)和异常崩溃(-2)
            if best_idx in (-1, -2):
                if best_idx == -1:
                    logger.info("VLM 判定候选图均未完美匹配描述，触发软回退，默认下发首张有效候选图。")
                else:
                    logger.warning("VLM 调用异常或超出重试限制，触发软回退，默认下发首张有效候选图。")
                final_url, final_bytes = valid_items[0]
                return final_url, final_bytes, "", True
                
            final_url, final_bytes = valid_items[best_idx]
            return final_url, final_bytes, "", False
        else:
            logger.info("VLM 模型未配置或获取失败，自动降级为首张有效候选图。")
            return valid_items[0][0], valid_items[0][1], "", True

    def _format_image_sync(self, img_bytes: bytes) -> bytes:
        try:
            with io.BytesIO(img_bytes) as img_io:
                with Image.open(img_io) as img:
                    if img.format not in ['JPEG', 'PNG']:
                        if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                            try:
                                img = img.convert('RGBA')
                                bg = Image.new("RGB", img.size, (255, 255, 255))
                                bg.paste(img, mask=img.split()[-1])
                                img = bg
                            except Exception:
                                img = img.convert("RGB")
                        else:
                            img = img.convert("RGB")
                            
                        with io.BytesIO() as buf:
                            img.save(buf, format="JPEG", quality=JPEG_QUALITY)
                            return buf.getvalue()
                    return img_bytes
        except UnidentifiedImageError:
            return img_bytes
        except Exception:
            return img_bytes

    async def _format_image(self, img_bytes: bytes) -> bytes:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._format_image_sync, img_bytes)

    async def _process_image_search(self, event: AstrMessageEvent, keyword: str, description: str, use_vlm_selection: bool) -> Tuple[Optional[bytes], str, bool]:
        eval_desc = description if description else keyword
        items = await self._ensure_minimum_images(keyword)
        
        if not items:
            return None, "未找到符合分辨率要求且可访问的图像资源。", False

        final_bytes = b""
        is_fallback = False
        
        if use_vlm_selection and len(items) > 1:
            _, final_bytes, err_msg, is_fallback = await self._vlm_selection(event, items, eval_desc)
            if not final_bytes:
                return None, err_msg, False
        else:
            _, final_bytes = items[0]

        final_bytes = await self._format_image(final_bytes)
        return final_bytes, "", is_fallback

    @filter.command("搜图")
    async def cmd_search_image(self, event: AstrMessageEvent, keyword: str, description: str = ""):
        try:
            use_vlm = self.config.get("enable_cmd_vlm_selection", True)
            yield event.plain_result(f"正在处理搜图请求 [{keyword}]...")
            
            img_bytes, err_msg, is_fallback = await self._process_image_search(event, keyword, description, use_vlm)
            if img_bytes:
                yield event.chain_result([Comp.Image.fromBytes(img_bytes)])
                if is_fallback and use_vlm:
                    yield event.plain_result("（注：本次未找到完美匹配细节的图片，已下发默认首图。如果不满意，您可以尝试描述得更具体些再搜一次~）")
            else:
                yield event.plain_result(f"搜图失败: {err_msg}")
        except Exception as e:
            logger.error(f"指令层搜图管线异常: {e}", exc_info=True)
            yield event.plain_result(f"处理过程中发生系统级错误: {str(e)}")

    async def _execute_tool(self, event: AstrMessageEvent, keyword: str, description: str, is_explanation: bool = False) -> str:
        try:
            if is_explanation:
                use_vlm = self.config.get("enable_explanation_vlm_selection", False)
            else:
                use_vlm = self.config.get("enable_nl_search_vlm_selection", True)
                
            img_bytes, err_msg, is_fallback = await self._process_image_search(event, keyword, description, use_vlm)
            
            if img_bytes:
                message_result = event.make_result()
                message_result.chain = [Comp.Image.fromBytes(img_bytes)]
                await event.send(message_result) 
                
                if is_fallback and use_vlm:
                    return (
                        "图像检索并下发成功。但 VLM 视觉模型未找到完美匹配描述的图片，已兜底下发默认首图。"
                        "请在回复中告知用户：由于目前的描述不够具体或场景较为罕见，本次发送的是搜索引擎默认首图。如果这不是您想要的，请提供更详细的外观特征后重试。"
                    )
                return "图像检索并下发成功。"
            else:
                return f"图像检索失败，错误原因：{err_msg}"
        except Exception as e:
            logger.error(f"工具层搜图管线异常: {e}", exc_info=True)
            return f"执行中断，系统抛出异常：{str(e)}"

    @filter.on_llm_request()
    async def inject_explanation_instruction(self, event: AstrMessageEvent, req: ProviderRequest):
        req.system_prompt = req.system_prompt or ""
        
        base_rule = (
            "\n【搜图工具使用规范】\n"
            "1. 当用户发出搜图、找图、看图等请求时，必须直接且仅使用 `search_image_tool` 工具。\n"
            "2. 禁止使用其他非搜图专用工具或虚构Markdown图片链接。\n"
            "3. 扩写 description 参数时，必须只描述核心主体和基础画风，绝不能虚构具体的特定动作、罕见姿势或生僻背景细节。\n"
        )
        
        if self.config.get("enable_explanation_image", True):
            base_rule += "4. 【自动配图规则】当你为用户解答或介绍明确实体（如人物、景点、动植物、物品等）时，若附图能明显提升用户的理解体验，可以主动调用 `search_image_tool` 搜索并附带一张该实体的图片，同时将 `is_explanation` 参数设为 true。"
            
        if "【搜图工具使用规范】" not in req.system_prompt:
            req.system_prompt += base_rule
