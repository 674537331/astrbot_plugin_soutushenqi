# -*- coding: utf-8 -*-
"""
模型交互模块
使用 textwrap.dedent 完美消除前导空格。
修复：合规化日志，优化大模型正则降级提取的 Edge Case。
修复：将 JSON 正则匹配改为非贪婪模式，彻底解决带有前后缀杂乱文本导致解析崩溃的问题。
"""
import base64
import json
import re
import textwrap
from astrbot.api.provider import Provider
from astrbot.api import logger

async def select_best_image_index(vlm_provider: Provider, image_bytes: bytes, keyword: str, total_count: int) -> int:
    base64_str = base64.b64encode(image_bytes).decode('utf-8')
    image_url = f"base64://{base64_str}"

    prompt = textwrap.dedent(f"""
        这是一张包含了 {total_count} 张图片的拼图网格，每张图片左上角都有一个数字编号（1 到 {total_count}）。
        请仔细观察这些图片，并根据用户的需求描述：“{keyword}”，选出最符合要求的一张图片。
        
        你必须且只能返回一个 JSON 对象，包含 "best_index" 键，其值为你选中的数字编号。
        不要返回任何其他说明文字或标点符号！
        
        示例响应格式：
        {{
          "best_index": 2
        }}
    """).strip()
    
    try:
        response = await vlm_provider.text_chat(prompt=prompt, image_urls=[image_url])
        result_text = response.result_chain.get_plain_text()
        
        # 1. 首先尝试解析标准 JSON（修复严重漏洞：增加 '?' 改为非贪婪模式）
        json_match = re.search(r'\{.*?\}', result_text, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(0))
                index = int(data.get("best_index", 1))
                if 1 <= index <= total_count:
                    return index - 1
            except json.JSONDecodeError:
                pass
                
        # 2. 降级策略：正则提取
        numbers = re.findall(r'\d+', result_text)
        if numbers:
            # 过滤出符合范围内的合法序号
            valid_nums = [int(n) for n in numbers if 1 <= int(n) <= total_count]
            if valid_nums:
                # 若大模型废话连篇，通常“最后提及且在范围内”的数字才是它真正的选择
                index = valid_nums[-1]
                return index - 1
                
    except Exception as e:
        logger.error(f"VLM 选择过程发生异常: {e}")
        
    return 0
