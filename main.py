# -*- coding: utf-8 -*-
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest
import astrbot.api.message_components as Comp
from astrbot.api import logger
import io
from PIL import Image

from .scraper import fetch_image_urls, fetch_bing_image_urls
from .composer import download_image_batch, create_collage_from_items
from .vlm import select_best_image_index

@register("astrbot_plugin_soutushenqi", "YourName", "智能搜图与比对插件(完全体)", "v4.1.0")
class SouTuShenQiPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config

    async def _get_vlm_provider(self, event: AstrMessageEvent):
        provider_id = self.config.get("vlm_provider_id", "")
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider: return provider
        
        umo = event.unified_msg_origin
        curr_id = await self.context.get_current_chat_provider_id(umo)
        return self.context.get_provider_by_id(curr_id)

    async def _process_image_search(self, event: AstrMessageEvent, keyword: str, description: str, use_vlm_selection: bool) -> tuple[bytes | None, str]:
        batch_size = self.config.get("batch_size", 16)
        threshold = batch_size * 0.3  
        eval_desc = description if description else keyword
        logger.info(f"发起搜图: [{keyword}], 描述: [{eval_desc}], VLM比对: {use_vlm_selection}, 期望数量: {batch_size}")
        
        urls, _ = await fetch_image_urls(keyword, batch_size)
        items = await download_image_batch(urls)
        logger.info(f"主来源下载完成，实际存活 {len(items)} 张。")

        if len(items) < threshold:
            logger.warning(f"主图床可用率过低 ({len(items)}/{batch_size})，启动 Bing 强力混合补充...")
            bing_urls = await fetch_bing_image_urls(keyword, batch_size)
            bing_items = await download_image_batch(bing_urls)
            
            seen_urls = {u for u, _ in items}
            for u, b in bing_items:
                if u not in seen_urls:
                    items.append((u, b))
                    seen_urls.add(u)
            
            items = items[:batch_size]
            logger.info(f"混合补充完毕，最终参与比对的总存活图片数: {len(items)}")

        if not items:
            return None, "所有的图片渠道均触发强力防盗链或失效，无一可用。"

        # 核心筛选逻辑
        final_url, final_bytes = "", b""
        if use_vlm_selection and len(items) > 1:
            collage_bytes, valid_items = await create_collage_from_items(items)
            if not collage_bytes or not valid_items:
                return None, "图片拼合处理失败，可用图片的数据均已损坏。"
                
            vlm_provider = await self._get_vlm_provider(event)
            if vlm_provider:
                logger.info(f"开始大模型淘汰比对 ({len(valid_items)} 选 1)...")
                best_idx = await select_best_image_index(vlm_provider, collage_bytes, eval_desc, len(valid_items))
                
                if best_idx == -1:
                    logger.warning("VLM 判定所有候选图均不符合要求，已一票否决。")
                    return None, "检索到的图片均与要求无关，为保证质量已自动拦截。"
                    
                final_url, final_bytes = valid_items[best_idx]
                logger.info(f"VLM优胜决定：{final_url}")
            else:
                final_url, final_bytes = valid_items[0]
        else:
            final_url, final_bytes = items[0]
            logger.info(f"跳过VLM，直接返回首张图：{final_url}")

        # 🚀 绝杀黑洞：全局强制 JPEG 转码护航 🚀
        try:
            img = Image.open(io.BytesIO(final_bytes))
            # 过滤掉容易被平台静默吞噬的格式 (WebP, AVIF, HEIC 等)
            if img.format not in ['JPEG', 'PNG']:
                img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=95)
                final_bytes = buf.getvalue()
                logger.info(f"图片(原格式 {img.format})已强制转码为 JPEG，保障跨平台发送兼容性。")
        except Exception as e:
            logger.warning(f"图片转码检测时发生异常 (将尝试发送原始数据): {e}")

        return final_bytes, ""

    @filter.command("搜图")
    async def cmd_search_image(self, event: AstrMessageEvent, keyword: str, description: str = ""):
        use_vlm = self.config.get("enable_cmd_vlm_selection", True)
        yield event.plain_result(f"正在处理搜图请求 [{keyword}]...")
        
        img_bytes, err_msg = await self._process_image_search(event, keyword, description, use_vlm)
        if img_bytes:
            yield event.chain_result([Comp.Image.fromBytes(img_bytes)])
        else:
            yield event.plain_result(f"搜图失败: {err_msg}")

    @filter.llm_tool(name="search_image_tool")
    async def tool_search_image(self, event: AstrMessageEvent, keyword: str, description: str = "", is_explanation: bool = False):
        """
        用于搜索网络上的高清图片、壁纸、照片并发送给用户。
        
        Args:
            keyword (str): 具体的搜索关键词，简练精准（如“猫”、“星空”）。
            description (str): 对期望图片的详细视觉描述。用于大模型智能筛选最符合的图片。
            is_explanation (bool): 若用户要求科普或询问"什么是XX"时，才将其设为 true。
        """
        if is_explanation:
            use_vlm = self.config.get("enable_explanation_vlm_selection", False)
        else:
            use_vlm = self.config.get("enable_nl_search_vlm_selection", True)
            
        img_bytes, err_msg = await self._process_image_search(event, keyword, description, use_vlm)
        
        if img_bytes:
            # 🚀 规范化主动发送：严格遵循框架生命周期要求 🚀
            message_result = event.make_result()
            message_result.chain = [Comp.Image.fromBytes(img_bytes)]
            await event.send(message_result) 
            
            if is_explanation:
                return f"图片已成功发送！请立刻开始用文字向用户详细解释什么是 {keyword}。"
            else:
                return "图片已成功发送给用户！简单回复一句搜图完成的话语即可。"
        else:
            return f"系统搜图失败: {err_msg}。请向用户致歉并仅提供文字回复。"

    @filter.on_llm_request()
    async def inject_explanation_instruction(self, event: AstrMessageEvent, req: ProviderRequest):
        if self.config.get("enable_explanation_image", True):
            instruction = (
                "\n【🔴 核心红线规范：搜图必须调用原生工具 🔴】\n"
                "当用户要求搜图、找图或看图时，你【必须直接调用】 `search_image_tool` 这个 Function Tool。\n"
                "【绝不允许】使用 `astrbot_execute_ipython` 等代码工具去模拟或打印搜图结果！！\n"
                "参数填写指南：\n"
                "1. `keyword`: 核心简练的搜索词（必填）。\n"
                "2. `description`: 丰富的视觉描述，越详细越好（必填）。\n"
                "3. `is_explanation`: 仅在回答“XX是什么”并需要配图辅助科普时设为 true，普通的搜图指令必须设为 false。"
            )
            if instruction not in req.system_prompt:
                req.system_prompt += instruction
