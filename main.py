# -*- coding: utf-8 -*-
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest
import astrbot.api.message_components as Comp
from astrbot.api import logger

from .scraper import fetch_image_urls, fetch_bing_image_urls
from .composer import download_image_batch, create_collage_from_items
from .vlm import select_best_image_index

@register("astrbot_plugin_soutushenqi", "YourName", "智能搜图与比对插件(完全体)", "v3.0.0")
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
        threshold = batch_size * 0.3  # 计算 30% 的阈值
        logger.info(f"发起搜图: [{keyword}], VLM比对: {use_vlm_selection}, 期望数量: {batch_size}")
        
        # 1. 主源抓取与并发下载
        urls, _ = await fetch_image_urls(keyword, batch_size)
        items = await download_image_batch(urls)
        logger.info(f"主来源下载完成，实际存活 {len(items)} 张。")

        # 2. 存活率低于 30%，触发 Bing 混合补充
        if len(items) < threshold:
            logger.warning(f"主图床可用率过低 ({len(items)}/{batch_size})，启动 Bing 混合补充...")
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

        # 3. 启用 VLM 淘汰比对
        if use_vlm_selection and len(items) > 1:
            collage_bytes, valid_items = await create_collage_from_items(items)
            if not collage_bytes or not valid_items:
                return None, "图片拼合处理失败，可用图片的数据均已损坏。"
                
            vlm_provider = await self._get_vlm_provider(event)
            if vlm_provider:
                logger.info(f"开始大模型淘汰比对 ({len(valid_items)} 选 1)...")
                best_idx = await select_best_image_index(vlm_provider, collage_bytes, keyword, len(valid_items))
                
                # 恢复大模型的一票否决权
                if best_idx == -1:
                    logger.warning("VLM 判定所有候选图均不符合要求，已一票否决。")
                    return None, "检索到的图片均与关键词无关，为保证质量已自动拦截。"
                    
                final_url, final_bytes = valid_items[best_idx]
                logger.info(f"VLM优胜决定：{final_url}")
                return final_bytes, ""
            else:
                return valid_items[0][1], ""
        
        # 4. 单图或无 VLM 模式
        else:
            return items[0][1], ""

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
                "2. 若用户的原话是疑问句，在问你某个客观实体是什么（如：“歼20是什么？”、“介绍一下XX”），你为了辅助科普去搜图时，才将 `is_explanation` 设置为 true！\n"
                "3. 严禁对抽象概念搜图。严禁使用 astrbot_execute_ipython 编写代码搜图，必须且只能调用 `search_image_tool`！"
            )
            if instruction not in req.system_prompt:
                req.system_prompt += instruction
