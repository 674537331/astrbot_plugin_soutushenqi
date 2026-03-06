# -*- coding: utf-8 -*-
import base64
import json
import re
import textwrap
import asyncio
import random
from astrbot.api.provider import Provider
from astrbot.api import logger

async def select_best_image_index(vlm_provider: Provider, image_bytes: bytes, description: str, total_count: int) -> int:
    # 🚀 前置极值防御 🚀
    if total_count <= 0:
        return -1

    base64_str = base64.b64encode(image_bytes).decode('utf-8')
    image_url = f"base64://{base64_str}"

    safe_desc = description[:300].replace('```', '')

    prompt = textwrap.dedent(f"""
        这是一张包含了 {total_count} 张图片的拼图网格，每张图片左上角都有一个数字编号。
        请仔细观察，并根据视觉需求描述：“{safe_desc}”，选出最符合要求的一张图片。
        
        【重要规则】
        1. 如果没有任何图片与需求相关，请严格返回 0。
        2. 如果有符合的，请返回对应的数字编号。
        
        你必须且只能返回一个纯净的 JSON 对象，包含 "best_index" 键。
        【警告】绝对不允许输出任何多余的解释文本！绝对不允许使用 Markdown 代码块（如 ```json）包裹！
        
        示例响应：
        {{
          "best_index": 0
        }}
    """).strip()
    
    retries = 3
    for attempt in range(retries):
        try:
            response = await vlm_provider.text_chat(prompt=prompt, image_urls=[image_url])
            
            if getattr(response, 'result_chain', None) is None:
                raise ValueError("VLM Provider 发生故障，返回了无效的响应。")
                
            result_text = response.result_chain.get_plain_text().strip()
            
            # 1. 第一优先级：标准 JSON 解析
            try:
                clean_text = re.sub(r'^```json|```$', '', result_text, flags=re.MULTILINE).strip()
                data = json.loads(clean_text)
                index = int(data.get("best_index", 1))
                
                if index == 0: return -1 
                if 1 <= index <= total_count: return index - 1
                raise ValueError(f"JSON 提取的序号 {index} 越界")
                
            except json.JSONDecodeError as e:
                logger.debug(f"VLM JSON 解析失败: {e}")
                    
            # 2. 🚀 剔除“饥不择食”的盲猜提取，启用严格格式正则 🚀
            fallback_match = re.search(r'(?:"best_index"\s*:\s*)(\d+)', result_text)
            if fallback_match:
                index = int(fallback_match.group(1))
                if index == 0: return -1
                if 1 <= index <= total_count: return index - 1
                raise ValueError(f"严格降级提取的序号 {index} 越界")
            else:
                # 严厉的反馈：如果不符合上述两项格式，直接判定失败并打回重试！
                raise ValueError("未在输出中找到合法的 'best_index: [数字]' 结构。")
                    
        except asyncio.CancelledError:
            raise
        except Exception as e:
            err_msg = str(e).lower()
            if any(k in err_msg for k in ["api key", "unauthorized", "blocked", "safety", "quota"]):
                logger.error(f"遭遇不可逆的模型 API 拒绝服务，放弃重试: {e}")
                break
                
            logger.warning(f"VLM 选择异常 (尝试 {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                base_sleep = 2 ** attempt
                jitter = random.uniform(0, 1)
                await asyncio.sleep(base_sleep + jitter)
                
    logger.error("VLM 重试均失败，降级返回第一张图。")
    return 0
