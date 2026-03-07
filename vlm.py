# -*- coding: utf-8 -*-
import base64
import json
import re
import textwrap
import asyncio
import random
from typing import List
from astrbot.api.provider import Provider
from astrbot.api import logger

def _extract_json_objects(text: str) -> List[str]:
    """
    改进的字符级堆栈解析器。
    通过约束在结构作用域内处理字符串标识，防御模型输出的前置孤立引号引发的提取截断问题。
    """
    results = []
    depth = 0
    start = -1
    in_string = False
    escape_next = False

    for i, char in enumerate(text):
        if depth > 0 and char == '\\':
            escape_next = True
            continue
        if depth > 0 and escape_next:
            escape_next = False
            continue

        if depth > 0 and char == '"':
            in_string = not in_string
            continue

        if not in_string:
            if char == '{':
                if depth == 0:
                    start = i
                depth += 1
            elif char == '}':
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start != -1:
                        results.append(text[start:i+1])
                        start = -1
    return results

async def select_best_image_index(vlm_provider: Provider, image_bytes: bytes, description: str, total_count: int) -> int:
    if total_count <= 0:
        return -1

    loop = asyncio.get_running_loop()
    base64_str = await loop.run_in_executor(None, lambda: base64.b64encode(image_bytes).decode('utf-8'))
    image_url = f"base64://{base64_str}"
    
    safe_desc = description[:300].replace('```', '')

    prompt = textwrap.dedent(f"""
        这是一张包含了 {total_count} 张图片的拼图网格，每张图片左上角都有一个数字编号。
        请仔细观察，并根据视觉需求描述：“{safe_desc}”，选出最符合要求的一张图片。
        
        【规则要求】
        1. 如果所有图片均不符合上述需求，请严格返回数值 0。
        2. 若存在符合需求的图片，请返回对应的数字编号。
        
        输出格式限定：仅返回一个JSON对象，包含 "best_index" 键。
        
        示例：
        {{
          "best_index": 0
        }}
    """).strip()
    
    retries = 3
    MAX_BACKOFF_TIME = 16.0 
    
    for attempt in range(retries):
        try:
            response = await vlm_provider.text_chat(prompt=prompt, image_urls=[image_url])
            
            if getattr(response, 'result_chain', None) is None:
                raise ValueError("提供方API返回数据结构无效，未包含消息链。")
                
            result_text = response.result_chain.get_plain_text().strip()
            json_blocks = _extract_json_objects(result_text)
            parsed_index = None
            
            for block in reversed(json_blocks): 
                try:
                    data = json.loads(block)
                    if "best_index" in data:
                        parsed_index = int(data["best_index"])
                        break 
                except json.JSONDecodeError:
                    continue
                    
            if parsed_index is not None:
                if parsed_index == 0: return -1 
                if 1 <= parsed_index <= total_count: return parsed_index - 1
                raise ValueError(f"序列化提取的索引值 {parsed_index} 不在合法区间 [0, {total_count}] 内。")
                
            fallback_matches = list(re.finditer(r'(?:"|\')?best_index(?:"|\')?\s*:\s*(\d+)', result_text, re.IGNORECASE))
            if fallback_matches:
                index = int(fallback_matches[-1].group(1))
                if index == 0: return -1
                if 1 <= index <= total_count: return index - 1
                raise ValueError(f"正则表达式回退提取的索引值 {index} 不在合法区间 [0, {total_count}] 内。")
            else:
                raise ValueError("输出响应未包含约定的特征键结构。")
                    
        except asyncio.CancelledError:
            raise
        except Exception as e:
            err_msg = str(e).lower()
            if any(k in err_msg for k in ["api key", "unauthorized", "blocked", "safety", "quota"]):
                logger.error(f"遭遇服务方拒绝服务响应 (Safety/Quota/Auth)，终止重试过程: {e}")
                break
                
            logger.warning(f"VLM评估执行异常 (执行次数 {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                base_sleep = min(2 ** attempt, MAX_BACKOFF_TIME)
                jitter = random.uniform(0, 1)
                await asyncio.sleep(base_sleep + jitter)
                
    logger.error("超出最大重试限制，状态降级返回基准索引(0)。")
    return 0
