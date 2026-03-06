# -*- coding: utf-8 -*-
"""
搜图神器插件总线
实现自然语言搜图、VLM 淘汰比对机制，及基于上下文的实体解释附图功能。
"""
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest
import astrbot.api.message_components as Comp
from astrbot.api import logger

from .scraper import fetch_image_urls
from .composer import download_image, create_collage
from .vlm import select_best_image_index

@register("astrbot_plugin_soutushenqi", "YourName", "智能搜图与比对插件", "v2.0.0")
class SouTuShenQiPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config

    async def _get_vlm_provider(self, event: AstrMessageEvent):
        """获取有效的 VLM Provider"""
        provider_id = self.config.get("vlm_provider_id", "")
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider:
                return provider
        
        # 默认回退至当前会话模型
        umo = event.unified_msg_origin
        curr_id = await self.context.get_current_chat_provider_id(umo)
        return self.context.get_provider_by_id(curr_id)

    async def _process_image_search(self, event: AstrMessageEvent, keyword: str, use_vlm_selection: bool) -> tuple[bytes | None, str]:
        """统筹搜图与比对的核心流程"""
        target_count = self.config.get("batch_size", 9) if use_vlm_selection else 1
        
        # 1. 抓取图片 URL 列表
        urls, error_msg = await fetch_image_urls(keyword, target_count)
        if not urls:
            return None, f"抓取环节失败: {error_msg}"
            
        final_url = urls[0]

        # 2. 淘汰比对流程 (VLM Selection)
        if use_vlm_selection and len(urls) > 1:
            vlm_provider = await self._get_vlm_provider(event)
            if not vlm_provider:
                return None, "VLM比对失败：未获取到有效的模型提供商。"
                
            collage_bytes, successful_urls = await create_collage(urls)
            if not collage_bytes:
                return None, "图片比对失败：无法生成网格预览图。"
                
            best_idx = await select_best_image_index(vlm_provider, collage_bytes, keyword, len(successful_urls))
            final_url = successful_urls[best_idx]

        # 3. 下载最终确定的图片
        img_bytes = await download_image(final_url)
        if not img_bytes:
            return None, f"图片提取成功，但下载被源站拦截，链接为: {final_url}"
            
        return img_bytes, ""

    @filter.command("搜图")
    async def cmd_search_image(self, event: AstrMessageEvent, keyword: str):
        """手动调用的搜图指令"""
        use_vlm = self.config.get("enable_vlm_selection", True)
        yield event.plain_result(f"执行搜图任务 [{keyword}]，请稍候...")
        
        img_bytes, err_msg = await self._process_image_search(event, keyword, use_vlm)
        
        if img_bytes:
            yield event.chain_result([Comp.Image.fromBytes(img_bytes)])
        else:
            yield event.plain_result(f"搜图任务中断。\n错误信息: {err_msg}")

    @filter.llm_tool(name="search_image_tool")
    async def tool_search_image(self, event: AstrMessageEvent, keyword: str):
        """
        供 LLM 调用的搜图工具。主要用于“解释附图”等场景。
        
        Args:
            keyword(string): 需搜索的实体或关键词。
        """
        use_vlm = self.config.get("enable_explanation_vlm_selection", False)
        
        img_bytes, err_msg = await self._process_image_search(event, keyword, use_vlm)
        
        if img_bytes:
            yield event.chain_result([Comp.Image.fromBytes(img_bytes)])
        else:
            yield event.plain_result(f"系统工具搜图失败: {err_msg}")

    @filter.on_llm_request()
    async def inject_explanation_instruction(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        事件钩子：拦截请求，注入“明确实体附带图片”的系统指令。
        """
        if self.config.get("enable_explanation_image", True):
            instruction = (
                "\n【功能指令】"
                "当用户询问关于某个具体的、可视化的实体对象（如特定的交通工具、动物、知名人物、地理建筑等）时，"
                "你必须调用 `search_image_tool` 工具搜索该实体的图片，以作为辅助说明发送给用户。"
                "严禁对抽象概念（如数学公式、编程语法、理论哲学等）调用此工具。"
            )
            req.system_prompt += instruction
