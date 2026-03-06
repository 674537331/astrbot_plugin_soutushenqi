# -*- coding: utf-8 -*-
"""
搜图神器插件总线
实现自然语言搜图、VLM 淘汰比对机制，及基于上下文的实体解释附图功能。
包含健全的单图下载重试容错机制及 Bing 兜底策略。
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
        use_vlm = self.config.get("enable_cmd_vlm_selection", True)
        yield event.plain_result(f"正在处理搜图请求 [{keyword}]...")
        
        img_bytes, err_msg = await self._process_image_search(event, keyword, use_vlm)
        
        if img_bytes:
            yield event.chain_result([Comp.Image.fromBytes(img_bytes)])
        else:
            yield event.plain_result(f"搜图失败: {err_msg}")

    @filter.llm_tool(name="search_image_tool")
    async def tool_search_image(self, event: AstrMessageEvent, keyword: str, is_explanation: bool = False):
        """
        当用户明确要求搜索图片，或者询问某个实体对象是什么时调用的工具。
        
        Args:
            keyword(string): 需搜索的实体关键词。
            is_explanation(boolean): 区分场景！若用户明确指令你“搜图/看图”，填 False；若用户在疑问“什么是XX/介绍XX”，需要你配图科普，填 True。
        """
        # 这里严格根据 LLM 解析出的意图，去读取你后台设置的对应开关！
        if is_explanation:
            use_vlm = self.config.get("enable_explanation_vlm_selection", False)
        else:
            use_vlm = self.config.get("enable_nl_search_vlm_selection", True)
            
        img_bytes, err_msg = await self._process_image_search(event, keyword, use_vlm)
        
        if img_bytes:
            result_obj = event.chain_result([Comp.Image.fromBytes(img_bytes)])
            await event.send(result_obj)
            
            if is_explanation:
                return f"图片已成功发送给用户！现在，请你立刻开始用文字向用户详细解释什么是 {keyword}。"
            else:
                return "图片已成功发送给用户！你可以简单回复一句搜图完成的话语。"
        else:
            return f"系统工具搜图失败: {err_msg}。请向用户致歉并仅提供文字回复。"

    @filter.on_llm_request()
    async def inject_explanation_instruction(self, event: AstrMessageEvent, req: ProviderRequest):
        """事件钩子：注入极其严格的实体配图指令"""
        if self.config.get("enable_explanation_image", True):
            instruction = (
                "\n【核心工具调用规范：search_image_tool】\n"
                "你必须极其严格地判断用户意图，正确设置 `is_explanation` 参数：\n"
                "1. 若用户的原话是明确的搜图祈使句（如：“帮我搜一张图”、“找张XX的图片”、“给我看XX”），你必须将 `is_explanation` 设置为 false！\n"
                "2. 若用户的原话是疑问句，在问你某个客观实体是什么（如：“歼20是什么？”、“介绍一下XX”），你为了辅助科普去搜图时，才将 `is_explanation` 设置为 true！\n"
                "3. 严禁对抽象概念搜图。严禁使用 astrbot_execute_ipython 编写代码搜图，必须且只能调用 `search_image_tool`！"
            )
            req.system_prompt += instruction
