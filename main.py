# -*- coding: utf-8 -*-
import io
from PIL import Image, UnidentifiedImageError
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest
import astrbot.api.message_components as Comp
from astrbot.api import logger

# --- 常量定义区 ---
SUPPLEMENT_THRESHOLD_RATIO = 0.3
JPEG_QUALITY = 95

@register("astrbot_plugin_soutushenqi", "YourName", "智能搜图与比对插件(完全体)", "v4.5.0")
class SouTuShenQiPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config

    async def terminate(self):
        """当插件被禁用、更新或卸载时，自动清理常驻的无头浏览器资源"""
        from .scraper import close_browser
        await close_browser()
        logger.info("SouTuShenQi 插件资源回收完毕，安全卸载。")

    async def _get_vlm_provider(self, event: AstrMessageEvent):
        provider_id = self.config.get("vlm_provider_id", "")
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider: return provider
        
        # 修复：空指针防御与兜底
        umo = getattr(event, "unified_msg_origin", None)
        if umo:
            curr_id = await self.context.get_current_chat_provider_id(umo)
            if curr_id:
                provider = self.context.get_provider_by_id(curr_id)
                if provider: return provider
                
        # 终极兜底：如果没有获取到特定会话的提供商，回退到全局 llm
        return getattr(self.context, 'llm', None)

    async def _ensure_minimum_images(self, keyword: str, batch_size: int) -> list[tuple[str, bytes]]:
        """子模块：负责基础图像获取与防盗链兜底"""
        threshold = batch_size * SUPPLEMENT_THRESHOLD_RATIO  
        urls, _ = await fetch_image_urls(keyword, batch_size)
        items = await download_image_batch(urls)
        logger.info(f"主来源下载完成，实际存活 {len(items)} 张。")

        if len(items) < threshold:
            logger.warning(f"主图床可用率过低 ({len(items)}/{batch_size})，启动 Bing 强力混合补充...")
            bing_urls = await fetch_bing_image_urls(keyword, batch_size)
            bing_items = await download_image_batch(bing_urls)
            
            seen_urls = {u for u, _ in items}
            new_bing_items = [(u, b) for u, b in bing_items if u not in seen_urls]
            
            items = (items + new_bing_items)[:batch_size]
            logger.info(f"混合补充完毕，最终参与比对的总存活图片数: {len(items)}")
            
        return items

    async def _vlm_selection(self, event: AstrMessageEvent, items: list[tuple[str, bytes]], eval_desc: str) -> tuple[str, bytes, str]:
        """子模块：负责构建拼图与大模型淘汰比对"""
        collage_bytes, valid_items = await create_collage_from_items(items)
        if not collage_bytes or not valid_items:
            return "", b"", "图片拼合处理失败，可用图片的数据均已损坏。"
            
        vlm_provider = await self._get_vlm_provider(event)
        if vlm_provider:
            logger.info(f"开始大模型淘汰比对 ({len(valid_items)} 选 1)...")
            best_idx = await select_best_image_index(vlm_provider, collage_bytes, eval_desc, len(valid_items))
            
            if best_idx == -1:
                logger.warning("VLM 判定所有候选图均不符合要求，已一票否决。")
                return "", b"", "检索到的图片均与要求无关，为保证质量已自动拦截。"
                
            final_url, final_bytes = valid_items[best_idx]
            logger.info(f"VLM优胜决定：{final_url}")
            return final_url, final_bytes, ""
        else:
            return valid_items[0][0], valid_items[0][1], ""

    def _format_image(self, img_bytes: bytes) -> bytes:
        """子模块：负责图片格式校验与安全转码"""
        try:
            # 修复：使用 with 上下文管理器确保内存被主动释放
            with io.BytesIO(img_bytes) as img_io:
                img = Image.open(img_io)
                if img.format not in ['JPEG', 'PNG']:
                    img = img.convert("RGB")
                    with io.BytesIO() as buf:
                        img.save(buf, format="JPEG", quality=JPEG_QUALITY)
                        final_bytes = buf.getvalue()
                    logger.info(f"图片(原格式 {img.format})已强制转码为 JPEG，保障跨平台发送兼容性。")
                    return final_bytes
                return img_bytes
        except UnidentifiedImageError:
            logger.warning("捕获到 UnidentifiedImageError，图片文件可能已损坏或非合法图像格式。")
            return img_bytes
        except OSError as e:
            logger.warning(f"图片转码检测时发生IO格式错误 (将尝试发送原始数据): {e}")
            return img_bytes
        except Exception as e:
            logger.warning(f"图片转码检测时发生未知错误: {e}")
            return img_bytes

    async def _process_image_search(self, event: AstrMessageEvent, keyword: str, description: str, use_vlm_selection: bool) -> tuple[bytes | None, str]:
        """总调度管线"""
        batch_size = self.config.get("batch_size", 16)
        eval_desc = description if description else keyword
        logger.info(f"发起搜图: [{keyword}], 描述: [{eval_desc}], VLM比对: {use_vlm_selection}, 期望数量: {batch_size}")
        
        items = await self._ensure_minimum_images(keyword, batch_size)
        if not items:
            return None, "所有的图片渠道均触发强力防盗链或失效，无一可用。"

        final_bytes = b""
        if use_vlm_selection and len(items) > 1:
            _, final_bytes, err_msg = await self._vlm_selection(event, items, eval_desc)
            if not final_bytes:
                return None, err_msg
        else:
            final_url, final_bytes = items[0]
            logger.info(f"跳过VLM，直接返回首张图：{final_url}")

        final_bytes = self._format_image(final_bytes)
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
                "\n【🔴 致命红线警告：搜图行为规范 🔴】\n"
                "当用户要求搜图、找图、看图时，你【必须直接且仅使用】名为 `search_image_tool` 的 Function Tool。\n"
                "【绝对禁止以下违规行为】：\n"
                "1. 严禁使用 `astrbot_execute_ipython` 写代码搜图！\n"
                "2. 严禁使用 `astrbot_execute_shell` 通过 curl 或其他命令请求接口搜图！\n"
                "3. 严禁你自己捏造或输出带有 [CQ:image,file=...] 或 Markdown 格式的虚假图片链接！\n"
                "你只需要在后台调用 `search_image_tool` 工具，填写 keyword 和 description 即可，系统会自动把图发给用户。"
            )
            if instruction not in req.system_prompt:
                req.system_prompt += instruction
