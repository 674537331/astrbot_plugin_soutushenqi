# -*- coding: utf-8 -*-
"""
搜图神器插件总线
实现自然语言搜图、VLM 淘汰比对机制，及基于上下文的实体解释附图功能。
包含健全的单图下载重试容错机制。
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
        
        umo = event.unified_msg_origin
        curr_id = await self.context.get_current_chat_provider_id(umo)
        return self.context.get_provider_by_id(curr_id)

    async def _process_image_search(self, event: AstrMessageEvent, keyword: str, use_vlm_selection: bool) -> tuple[bytes | None, str]:
        """统筹搜图与下载流程。针对单图模式实现下载失败自动重试下一个链接的功能。"""
        # 为保证容错率，统一抓取批量候选链接
        batch_size = self.config.get("batch_size", 9)
        logger.info(f"发起搜图: [{keyword}], VLM比对: {use_vlm_selection}, 候选池大小: {batch_size}")
        
        urls, error_msg = await fetch_image_urls(keyword, batch_size)
        if not urls:
            return None, f"抓取阶段失败: {error_msg}"
            
        logger.info(f"抓取完成，共提取 {len(urls)} 个候选链接。")

        # --- 模式 A: 启用 VLM 淘汰比对 ---
        if use_vlm_selection and len(urls) > 1:
            vlm_provider = await self._get_vlm_provider(event)
            if not vlm_provider:
                return None, "VLM比对失败：未获取到模型。"
                
            collage_bytes, successful_urls = await create_collage(urls)
            if not collage_bytes or not successful_urls:
                return None, "图片比对失败：所有候选图均无法下载(可能全部触发防盗链)。"
                
            best_idx = await select_best_image_index(vlm_provider, collage_bytes, keyword, len(successful_urls))
            final_url = successful_urls[best_idx]
            logger.info(f"VLM选中图片: {final_url}")
            
            img_bytes = await download_image(final_url)
            if img_bytes:
                return img_bytes, ""
            return None, f"VLM选中的图片由于网络原因下载失败: {final_url}"

        # --- 模式 B: 单图模式 (按序重试) ---
        else:
            logger.info("采用单图模式，开始顺序尝试下载...")
            for idx, url in enumerate(urls):
                img_bytes = await download_image(url)
                if img_bytes:
                    logger.info(f"顺序尝试成功：第 {idx + 1} 张图片下载完成。")
                    return img_bytes, ""
                else:
                    logger.warning(f"顺序尝试：第 {idx + 1} 张图片下载失败，尝试下一张...")
            
            return None, "抓取到的所有图片均下载失败（均被源站拦截或超时）。"

    @filter.command("搜图")
    async def cmd_search_image(self, event: AstrMessageEvent, keyword: str):
        """手动调用的搜图指令"""
        use_vlm = self.config.get("enable_vlm_selection", True)
        yield event.plain_result(f"正在处理搜图请求 [{keyword}]...")
        
        img_bytes, err_msg = await self._process_image_search(event, keyword, use_vlm)
        
        if img_bytes:
            yield event.chain_result([Comp.Image.fromBytes(img_bytes)])
        else:
            yield event.plain_result(f"搜图失败: {err_msg}")

    @filter.llm_tool(name="search_image_tool")
    async def tool_search_image(self, event: AstrMessageEvent, keyword: str):
        """
        当用户询问实体对象时调用的搜图工具。
        
        Args:
            keyword(string): 需搜索的实体关键词。
        """
        use_vlm = self.config.get("enable_explanation_vlm_selection", False)
        img_bytes, err_msg = await self._process_image_search(event, keyword, use_vlm)
        
        if img_bytes:
            await event.send(event.chain_result([Comp.Image.fromBytes(img_bytes)]).chain)
            return "图片已成功发送给用户。"
        else:
            return f"系统工具搜图失败: {err_msg}"

    @filter.on_llm_request()
    async def inject_explanation_instruction(self, event: AstrMessageEvent, req: ProviderRequest):
        """事件钩子：注入实体解释配图的强制指令"""
        if self.config.get("enable_explanation_image", True):
            instruction = (
                "\n【重要指令：实体解释配图】\n"
                "当用户询问具体的、可视化的实体对象（如装备、动物、人物、建筑等）是什么时，"
                "你必须调用 `search_image_tool` 工具搜索并发送该实体的图片。"
                "严禁使用 astrbot_execute_ipython 或自行编写代码执行搜索。"
                "若用户询问抽象概念或理论知识，请勿调用此工具。"
            )
            req.system_prompt += instruction
