# -*- coding: utf-8 -*-
"""
搜图神器插件总线
包含了优雅的 Bing 混合补充机制。当主图库存活不足时，强力补充直至填满九宫格。
赋予大模型一票否决权，全都是无关图时直接拦截，杜绝辣眼睛图片。
修复：修正 LLM Tool 函数的规范注释以及图片发送的 Pydantic ValidationError。
"""
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest
import astrbot.api.message_components as Comp
from astrbot.api import logger

from .scraper import fetch_image_urls, fetch_bing_image_urls
from .composer import download_image_batch, create_collage_from_items
from .vlm import select_best_image_index

@register("astrbot_plugin_soutushenqi", "YourName", "智能搜图与比对插件", "v2.1.2")
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
        """包含 Bing 强力填满机制的顶层管线"""
        batch_size = self.config.get("batch_size", 9)
        logger.info(f"发起搜图: [{keyword}], VLM比对: {use_vlm_selection}, 期望数量: {batch_size}")
        
        # 1. 主源抓取与并发下载 (多要几个备用)
        urls, error_msg = await fetch_image_urls(keyword, batch_size + 5)
        items = await download_image_batch(urls)
        
        # 截断到允许的最大网格数
        items = items[:batch_size]
        logger.info(f"主来源下载完成，实际存活 {len(items)}/{batch_size} 张。")

        # 2. 只要不满 9 张，就强行去 Bing 抓满！
        if len(items) < batch_size:
            missing_count = batch_size - len(items)
            logger.warning(f"图片数量不足，还差 {missing_count} 张，启动 Bing 强力填补...")
            
            # 狮子大开口：为了填补几个空位，直接向 Bing 要 30 个链接作候选
            bing_urls = await fetch_bing_image_urls(keyword, 30)
            seen_urls = {u for u, _ in items}
            
            # 过滤掉主源已经有的
            new_bing_urls = [u for u in bing_urls if u not in seen_urls]
            
            # 批量下载 Bing 的图
            bing_items = await download_image_batch(new_bing_urls)
            
            # 逐个塞进候选池，塞满 9 个就停手
            for u, b in bing_items:
                items.append((u, b))
                seen_urls.add(u)
                if len(items) >= batch_size:
                    break
            
            logger.info(f"混合填补完毕，最终参与比对的总存活图数: {len(items)}")

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
                
                # 触发了一票否决权
                if best_idx == -1:
                    logger.warning(f"VLM 一票否决：检索到的图片均与 [{keyword}] 无关。")
                    return None, "找了一圈，但发现搜出来的图都是些无关的广告或占位图，为了不辣眼睛，我先拦截啦！建议换个准确的搜索词试试~"
                    
                final_url, final_bytes = valid_items[best_idx]
                logger.info(f"VLM优胜决定：{final_url}")
                return final_bytes, "" 
            else:
                final_url, final_bytes = valid_items[0]
                logger.warning(f"未获取到 VLM 模型，直接返回首张图。")
                return final_bytes, ""

        # 4. 模式 B: 单图模式 (跳过淘汰，直接发首图)
        else:
            final_url, final_bytes = items[0]
            logger.info(f"单图模式或候选不足，直接返回首张图: {final_url}")
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
        '''用于搜索网络上的高清图片、壁纸、照片并发送给用户。
        Args:
            keyword(string): 提取的搜索关键词，必须简练精准（例如用户说"搜一张拉克丝的图"，关键词就是"拉克丝"）。
            is_explanation(boolean): 默认为 false。若用户原话是疑问句要求科普（如"什么是XX"），需配合文字解释时，才将其设为 true。
        '''
