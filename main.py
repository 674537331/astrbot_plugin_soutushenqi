# -*- coding: utf-8 -*-
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest
import astrbot.api.message_components as Comp
from astrbot.api import logger
from .scraper import fetch_image_urls, fetch_bing_image_urls
from .composer import download_image_batch, create_collage_from_items
from .vlm import select_best_image_index

@register("astrbot_plugin_soutushenqi", "YourName", "智能搜图与比对插件", "v2.1.4")
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

    async def _process_image_search(self, event: AstrMessageEvent, keyword: str, use_vlm_selection: bool) -> tuple[bytes | None, str]:
        batch_size = self.config.get("batch_size", 9)
        logger.info(f"发起搜图: [{keyword}], VLM比对: {use_vlm_selection}, 期望数量: {batch_size}")
        
        # 1. 尝试主源
        urls, _ = await fetch_image_urls(keyword, batch_size)
        items = await download_image_batch(urls)
        
        # 2. 强力补满 9 张（改为向 Bing 请求大量候选以抵消坏链）
        if len(items) < batch_size:
            logger.warning(f"存活不足({len(items)}), 启动 Bing 强力补全...")
            bing_urls = await fetch_bing_image_urls(keyword, 30)
            seen = {u for u, _ in items}
            bing_items = await download_image_batch([u for u in bing_urls if u not in seen])
            for u, b in bing_items:
                items.append((u, b))
                if len(items) >= batch_size: break
        
        if not items: return None, "两路搜索均未找到可下载图片。"

        # 3. VLM 比对逻辑
        if use_vlm_selection and len(items) > 1:
            collage_bytes, valid_items = await create_collage_from_items(items)
            vlm_provider = await self._get_vlm_provider(event)
            if vlm_provider and collage_bytes:
                best_idx = await select_best_image_index(vlm_provider, collage_bytes, keyword, len(valid_items))
                if best_idx == -1: return None, "搜出的图均不相关。"
                return valid_items[best_idx][1], ""
            return items[0][1], ""
        return items[0][1], ""

    @filter.command("搜图")
    async def cmd_search_image(self, event: AstrMessageEvent, keyword: str):
        yield event.plain_result(f"正在处理搜图请求 [{keyword}]...")
        img, err = await self._process_image_search(event, keyword, True)
        if img: yield event.chain_result([Comp.Image.fromBytes(img)])
        else: yield event.plain_result(f"失败: {err}")

    @filter.llm_tool(name="search_image_tool")
    async def tool_search_image(self, event: AstrMessageEvent, keyword: str, is_explanation: bool = False) -> str:
        '''用于搜索网络图片并发送给用户。
        Args:
            keyword(string): 必须是具体的搜索词，如"拉克丝"。
            is_explanation(boolean): 疑问句科普时传 true。
        '''
        img, err = await self._process_image_search(event, keyword, True)
        if img:
            result_msg = event.make_result()
            result_msg.chain = [Comp.Image.fromBytes(img)]
            await event.send(result_msg)
            return f"图片已发送！关键词是{keyword}。请回复告知用户或继续解释。"
        return f"搜图失败: {err}。请仅文字回复。"

    @filter.on_llm_request()
    async def inject_explanation_instruction(self, event: AstrMessageEvent, req: ProviderRequest):
        if self.config.get("enable_explanation_image", True):
            req.system_prompt += "\n优先调用 search_image_tool 提供配图。"
