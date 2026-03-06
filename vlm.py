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
        1. 如果你发现没有任何一张图片与“{description}”相关，请严格返回数字 0。
        2. 如果有符合的，请返回对应的数字编号。
        
        你必须且只能返回一个 JSON 对象，包含 "best_index" 键，其值为你选中的数字编号。
        不要返回任何其他说明文字。
        
        示例响应格式：
        {{
          "best_index": 0
        }}
    """).strip()
    
    retries = 3
    for attempt in range(retries):
        try:
            response = await vlm_provider.text_chat(prompt=prompt, image_urls=[image_url])
            result_text = response.result_chain.get_plain_text()
            
            # 1. 解析标准 JSON (改用 .*? 非贪婪模式防止跨越匹配)
            json_match = re.search(r'\{.*?\}', result_text, re.DOTALL)
            if json_match:
                try:
                    data = json.loads(json_match.group(0))
                    index = int(data.get("best_index", 1))
                    if 1 <= index <= total_count:
                        return index - 1
                    elif index == 0:
                        return -1  # 触发一票否决
                except json.JSONDecodeError as e:
                    logger.debug(f"VLM JSON 解析失败: {e}, 原始内容片段: {json_match.group(0)}")
                    
            # 2. 降级正则提取策略
            numbers = re.findall(r'\d+', result_text)
            if numbers:
                index = int(numbers[0])
                if index == 0:
                    return -1
                if 1 <= index <= total_count:
                    return index - 1
                    
        except Exception as e:
            logger.warning(f"VLM 选择过程发生异常 (尝试 {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                await asyncio.sleep(2)
                
    logger.error("VLM 重试均失败，降级返回第一张图。")
    return 0
