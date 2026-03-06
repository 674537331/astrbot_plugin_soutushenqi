# -*- coding: utf-8 -*-
import base64
import json
import re
import logging
import textwrap
from astrbot.api.provider import Provider

logger = logging.getLogger("astrbot")

async def select_best_image_index(vlm_provider: Provider, image_bytes: bytes, keyword: str, total_count: int) -> int:
    base64_str = base64.b64encode(image_bytes).decode('utf-8')
    image_url = f"base64://{base64_str}"

    # 结合新版的清晰排版与旧版的“一票否决权”指令
    prompt = textwrap.dedent(f"""
        这是一张包含了 {total_count} 张图片的拼图网格，每张图片左上角都有一个数字编号（1 到 {total_count}）。
        请仔细观察这些图片，并根据用户的需求描述：“{keyword}”，选出最符合要求的一张图片。
        
        【重要规则】
        1. 如果你发现网格中没有任何一张图片与“{keyword}”相关（比如全是广告、毫不相干的人物、无关的素材图等），请严格返回数字 0。
        2. 如果有符合的，请返回对应的数字编号。
        
        你必须且只能返回一个 JSON 对象，包含 "best_index" 键，其值为你选中的数字编号（0 到 {total_count}）。
        不要返回任何其他说明文字。
        
        示例响应格式：
        {{
          "best_index": 0
        }}
    """).strip()
    
    try:
        response = await vlm_provider.text_chat(prompt=prompt, image_urls=[image_url])
        result_text = response.result_chain.get_plain_text()
        
        # 1. 解析标准 JSON
        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(0))
                index = int(data.get("best_index", 1))
                if 1 <= index <= total_count:
                    return index - 1
                elif index == 0:
                    return -1  # 触发一票否决
            except json.JSONDecodeError:
                pass
                
        # 2. 降级正则提取策略
        numbers = re.findall(r'\d+', result_text)
        if numbers:
            index = int(numbers[0]) # 优先取第一个识别到的数字
            if index == 0:
                return -1
            if 1 <= index <= total_count:
                return index - 1
                
    except Exception as e:
        logger.error(f"VLM 选择过程发生异常: {e}")
        
    return 0 # 兜底返回第一张图
