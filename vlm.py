# -*- coding: utf-8 -*-
import base64
import json
import re
import textwrap
import asyncio
from astrbot.api.provider import Provider
from astrbot.api import logger

async def select_best_image_index(vlm_provider: Provider, image_bytes: bytes, description: str, total_count: int) -> int:
    base64_str = base64.b64encode(image_bytes).decode('utf-8')
    image_url = f"base64://{base64_str}"

    prompt = textwrap.dedent(f"""
        这是一张包含了 {total_count} 张图片的拼图网格，每张图片左上角都有一个数字编号。
        请仔细观察，并根据视觉需求描述：“{description}”，选出最符合要求的一张图片。
        
        【重要规则】
        1. 如果没有任何图片与“{description}”相关，请严格返回 0。
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
            result_text = response.result_chain.get_plain_text().strip()
            
            # 1. 尝试直接加载纯净 JSON
            try:
                # 剔除可能的非标准控制字符或多余反引号
                clean_text = re.sub(r'^```json|```$', '', result_text, flags=re.MULTILINE).strip()
                data = json.loads(clean_text)
                index = int(data.get("best_index", 1))
                if 1 <= index <= total_count: return index - 1
                elif index == 0: return -1 
            except json.JSONDecodeError as e:
                logger.debug(f"VLM JSON 解析失败: {e}, 原始内容片段: {result_text}")
                    
            # 2. 防御性极强的降级正则策略
            # 方案 A: 尝试提取形如 "best_index": 5 的残缺结构
            fallback_match = re.search(r'(?:"best_index"\s*:\s*)(\d+)', result_text)
            if fallback_match:
                index = int(fallback_match.group(1))
            else:
                # 方案 B: 提取孤立的数字 (限定在 0 到 16 之间，前后不能连着其他数字)
                numbers = re.findall(r'(?<!\d)(?:1[0-6]|[0-9])(?!\d)', result_text)
                if numbers:
                    index = int(numbers[-1]) # 如果模型话多，通常最后一句是结论
                else:
                    raise ValueError(f"无法从大模型回复中提取合法序号。原始内容: {result_text}")
                    
            if index == 0: return -1
            if 1 <= index <= total_count: return index - 1
                    
        except Exception as e:
            logger.warning(f"VLM 选择过程发生异常 (尝试 {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                await asyncio.sleep(2)
                
    logger.error("VLM 重试均失败，降级返回第一张图。")
    return 0
