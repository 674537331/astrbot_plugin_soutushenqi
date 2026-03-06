# -*- coding: utf-8 -*-
"""
搜图神器插件总线
包含了优雅的 Bing 混合补充机制。当主图库下载存活率 < 30% 时，无缝混入 Bing 搜图进行同台淘汰比对。
极大地减少了冗余的重复下载，提升响应速度。修复了 Prompt 多轮对话无限叠加问题。
"""
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest
import astrbot.api.message_components as Comp
from astrbot.api import logger

from .scraper import fetch_image_urls, fetch_bing_image_urls
from .composer import download_image_batch, create_collage_from_items
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
        """包含 Bing 混合补充机制的顶层管线"""
        batch_size = self.config.get("batch_size", 9)
        threshold = batch_size * 0.3  # 计算 30% 的阈值
        logger.info(f"发起搜图: [{keyword}], VLM比对: {use_vlm_selection}, 期望数量: {batch_size}, 补充阈值: {threshold}")
        
        # 1. 主源抓取与并发下载
        urls, error_msg = await fetch_image_urls(keyword, batch_size)
        items = await download_image_batch(urls)
        logger.info(f"主来源下载完成，实际存活 {len(items)} 张。")

        # 2. 存活率低于 30%，触发 Bing 混合补充
        if len(items) < threshold:
            logger.warning(f"主图床可用率过低 ({len(items)}/{batch_size})，自动调用 Bing 图库进行混合补充...")
            bing_urls = await fetch_bing_image_urls(keyword, batch_size)
            bing_items = await download_image_batch(bing_urls)
            
            # 高效 O(1) 集合去重合并
            seen_urls = {u for u, _ in items}
            for u, b in bing_items:
                if u not in seen_urls:
                    items.append((u, b))
                    seen_urls.add(u)
            
            # 截断到允许的最大网格数
            items = items[:batch_size]
            logger.info(f"混合补充完毕，最终参与比对的总存活图片数: {len(items)}")

        if not items:
            return None, "所有的图片渠道均触发强力防盗链或失效，无一可用。"

        # 3. 模式 A: 启用 VLM 淘汰比对
        if use_vlm_selection and len(items) > 1:
            collage_bytes, valid_items = await create_collage_from_items(items)
            if not collage_bytes or not valid_items:
                return None, "图片拼合处理失败，可用图片的数据均已损坏。"
                
            vlm_provider = await self._get_vlm_provider(event)
            if vlm_provider:
                logger.info(f"开始大模型淘汰比对 ({len(valid_items)} 选 1)...")
                best_idx = await select_best_image_index(vlm_provider, collage_bytes, keyword, len(valid_items))
                final_url, final_bytes = valid_items[best_idx]
                logger.info(f"VLM优胜决定：{final_url}")
                return final_bytes, ""  # 直接提取内存里的 bytes，不再二次下载！
            else:
                final_url, final_bytes = valid_items[0]
                logger.warning(f"未获取到 VLM 模型，直接返回补充后的第一张可用图。")
                return final_bytes, ""

        # 4. 模式 B: 单图模式 (跳过淘汰，直接发首图)
        else:
            final_url, final_bytes = items[0]
            logger.info(f"单图模式或候选不足，直接返回首张存活图: {final_url}")
            return final_bytes, ""

    @filter.command("搜图")
    async def cmd_search_image(self, event: AstrMessageEvent, keyword: str):
        use_vlm = self.config.get("enable_cmd_vlm_selection", True)
        yield event.plain_result(f"正在处理搜图请求 [{keyword}]...")
        
        img_bytes, err_msg = await self._process_image_search(event, keyword, use_vlm)
        if img_bytes:
            yield event.chain_result([Comp.Image.fromBytes(img_bytes)])
        else:
            yield event.plain_result(f"搜图失败: {err_msg}")

    @filter.llm_tool(name="search_image_tool")
    async def tool_search_image(self, event: AstrMessageEvent, keyword: str, is_explanation: bool = False):
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
        if self.config.get("enable_explanation_image", True):
            instruction = (
                "\n【核心工具调用规范：search_image_tool】\n"
                "你必须极其严格地判断用户意图，正确设置 `is_explanation` 参数：\n"
                "1. 若用户的原话是明确的搜图祈使句（如：“帮我搜一张图”、“找张XX的图片”、“给我看XX”），你必须将 `is_explanation` 设置为 false！\n"
                "2. 若用户的原话是疑问句，在问你某个客观实体是什么（如：“xx是什么？”、“介绍一下XX”），你为了辅助科普去搜图时，才将 `is_explanation` 设置为 true！\n"
                "3. 严禁对抽象概念搜图。严禁使用 astrbot_execute_ipython 编写代码搜图，必须且只能调用 `search_image_tool`！"
            )
            # 防御性编程：防止在长程对话中无限追加相同 Prompt 导致 Token 爆炸
            if instruction not in req.system_prompt:
                req.system_prompt += instruction
