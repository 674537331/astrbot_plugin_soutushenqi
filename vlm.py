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
        【警告】绝对不允许输出任何多余的解释文本！
        
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
            
            # 1. 第一优先级：高鲁棒性 JSON 块扫描
            clean_text = re.sub(r'^```(json)?|```$', '', result_text, flags=re.IGNORECASE | re.MULTILINE).strip()
            
            # 🚀 修复贪婪灾难：使用非贪婪匹配找出所有的花括号结构，挨个尝试解析 🚀
            json_matches = re.findall(r'\{.*?\}', clean_text, re.DOTALL)
            parsed_index = None
            
            for match_str in json_matches:
                try:
                    data = json.loads(match_str)
                    if "best_index" in data:
                        parsed_index = int(data["best_index"])
                        break # 找到了第一个合法的 JSON 结构就立刻停止
                except json.JSONDecodeError:
                    continue
                    
            if parsed_index is not None:
                if parsed_index == 0: return -1 
                if 1 <= parsed_index <= total_count: return parsed_index - 1
                raise ValueError(f"JSON 提取的序号 {parsed_index} 越界")
                
            logger.debug("VLM 响应未能通过 JSON 解析，开启正则降级。")
                    
            # 2. 防御性极强的降级正则策略
            fallback_matches = list(re.finditer(r'(?:"|\')?best_index(?:"|\')?\s*:\s*(\d+)', result_text, re.IGNORECASE))
            if fallback_matches:
                extracted_numbers = {int(m.group(1)) for m in fallback_matches}
                
                if len(extracted_numbers) > 1:
                    raise ValueError(f"大模型输出了多个冲突的序号: {extracted_numbers}，拦截并打回重试。")
                    
                index = extracted_numbers.pop()
                if index == 0: return -1
                if 1 <= index <= total_count: return index - 1
                raise ValueError(f"降级提取的序号 {index} 越界")
            else:
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
