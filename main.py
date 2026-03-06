# -*- coding: utf-8 -*-
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest
import astrbot.api.message_components as Comp
from astrbot.api import logger
from .scraper import fetch_image_urls, fetch_bing_image_urls
from .composer import download_image_batch, create_collage_from_items
from .vlm import select_best_image_index

@register("astrbot_plugin_soutushenqi", "YourName", "智能搜图与比对插件", "v2.1.5")
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
        
        # 1. 尝试主源抓取
        urls, _ = await fetch_image_urls(keyword, batch_size)
        items = await download_image_batch(urls)
        logger.info(f"主来源下载完成，实际存活 {len(items)}/{batch_size} 张。")

        # 2. 强力补满 9 张（改为向 Bing 请求大量候选以抵消坏链）
        if len(items) < batch_size:
            logger.warning(f"图片数量不足，还差 {batch_size - len(items)} 张，启动 Bing 强力填补...")
            bing_urls = await fetch_bing_image_urls(keyword, 30)
            seen_urls = {u for u, _ in items}
            # 并发下载 Bing 的图
            bing_items = await download_image_batch([u for u in bing_urls if u not in seen_urls])
            for u, b in bing_items:
                items.append((u, b))
                if len(items) >= batch_size: break
            logger.info(f"混合填补完毕，最终参与比对的总图数: {len(items)}")

        if not items: return None, "所有的图片渠道均失效或被拦截，无一可用。"

        # 3. 模式 A: 启用 VLM 淘汰比对
        if use_vlm_selection and len(items) > 1:
            collage_bytes, valid_items = await create_collage_from_items(items)
            vlm_provider = await self._get_vlm_provider(event)
            if vlm_provider and collage_bytes:
                logger.info(f"开始大模型淘汰比对 ({len(valid_items)} 选 1)...")
                best_idx = await select_best_image_index(vlm_provider, collage_bytes, keyword, len(valid_items))
                if best_idx == -1:
                    return None, "检索到的图片均与关键词无关，已自动拦截。"
                return valid_items[best_idx][1], ""
            return items[0][1], ""
        else:
            return items[0][1], ""

    @filter.command("搜图")
    async def cmd_search_image(self, event: AstrMessageEvent, keyword: str):
        yield event.plain_result(f"正在处理搜图请求 [{keyword}]...")
        img, err = await self._process_image_search(event, keyword, True)
        if img: yield event.chain_result([Comp.Image.fromBytes(img)])
        else: yield event.plain_result(f"搜图失败: {err}")

    @filter.llm_tool(name="search_image_tool")
    async def tool_search_image(self, event: AstrMessageEvent, keyword: str, is_explanation: bool = False) -> str:
        '''用于搜索网络上的高清图片、壁纸、照片并发送给用户。
        Args:
            keyword(string): 具体的搜索关键词，必须简练精准。
            is_explanation(boolean): 若用户要求科普或询问"什么是XX"时，才将其设为 true。
        '''
        img, err = await self._process_image_search(event, keyword, True)
        if img:
            result_msg = event.make_result()
            result_msg.chain = [Comp.Image.fromBytes(img)]
            await event.send(result_msg)
            return f"图片已成功发送给用户！关键词是{keyword}。你可以简单回复一句搜图完成的话语。"
        return f"系统工具搜图失败: {err}。请向用户致歉并仅提供文字回复。"

    @filter.on_llm_request()
    async def inject_explanation_instruction(self, event: AstrMessageEvent, req: ProviderRequest):
        if self.config.get("enable_explanation_image", True):
            instruction = (
                "\n【核心工具调用绝对规范】\n"
                "你当前拥有多个图片相关工具，必须严格遵守以下红线原则，绝不允许越界混用：\n"
                "1. 【严格锁定搜图】：只要用户的原话中包含“搜图”、“找图”、“搜一张”、“给我看”等明确的搜索指令，你【必须且只能】调用 `search_image_tool`，绝不允许调用任何画图/生成图像的工具！此时 is_explanation=false。\n"
                "2. 【百科科普配图】：若用户是疑问句询问“XX是什么/介绍XX”，你为了辅助科普，必须调用 `search_image_tool`，此时 is_explanation=true。\n"
                "3. 【画图生成指令】：只有当用户明确说出“画一张”、“生成一张”、“做一张”时，你才能使用你的生图工具。"
            )
            if instruction not in req.system_prompt:
                req.system_prompt += instruction
