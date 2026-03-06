# -*- coding: utf-8 -*-
"""
图像处理与下载模块
负责将多张网络图片下载至内存，并合成带有数字标号的网格预览图供VLM比对使用。
"""
import io
import math
import asyncio
import aiohttp
from typing import Optional
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError
import logging

logger = logging.getLogger("astrbot")

async def download_image(url: str) -> Optional[bytes]:
    """
    下载单张图片到内存中，包含强力防盗链绕过策略。
    
    Args:
        url (str): 图片直链
        
    Returns:
        Optional[bytes]: 图片的二进制数据。若发生防盗链、无权限或非图片资源则返回 None。
    """
    # 针对不同图床，动态伪造对应的 Referer 以骗过防盗链
    referer = "https://www.google.com/"  # 默认伪装从谷歌搜索而来
    if 'baidu.com' in url:
        referer = "https://image.baidu.com/"
    elif 'duitang.com' in url:
        referer = "https://www.duitang.com/"
    elif 'bilibili.com' in url or 'hdslb.com' in url:
        referer = "https://www.bilibili.com/"
        
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": referer,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive"
    }
    
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=15) as resp:
                if resp.status == 200:
                    content_type = resp.headers.get('Content-Type', '').lower()
                    if 'text/html' in content_type:
                        logger.warning(f"下载失败：源站启动防盗链，返回了HTML网页 ({url})")
                        return None
                    return await resp.read()
                elif resp.status in [403, 401]:
                     logger.warning(f"下载失败：源站权限拒绝 HTTP {resp.status} ({url})")
                     return None
                else:
                    logger.warning(f"下载失败：异常 HTTP {resp.status} ({url})")
                    return None
    except Exception as e:
        logger.error(f"图片请求连接失败 ({url}): {str(e)}")
    return None

def _create_collage_sync(image_bytes_list: list[Optional[bytes]], valid_urls: list[str]) -> tuple[Optional[bytes], list[str]]:
    """
    同步拼接网格图。自动跳过下载失败或损坏的图片。
    此函数在独立线程中运行，防止阻塞异步事件循环。
    """
    successful_images = []
    successful_urls = []
    tile_size = 300
    
    for i, img_bytes in enumerate(image_bytes_list):
        if img_bytes:
            try:
                img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                img = img.resize((tile_size, tile_size), Image.Resampling.LANCZOS)
                successful_images.append(img)
                successful_urls.append(valid_urls[i])
            except (IOError, UnidentifiedImageError):
                continue

    if not successful_images:
        return None, []

    # 计算网格行列数
    columns = math.ceil(math.sqrt(len(successful_images)))
    rows = math.ceil(len(successful_images) / columns)
    
    # 创建纯白底色的画布
    collage = Image.new('RGB', (columns * tile_size, rows * tile_size), (255, 255, 255))
    draw = ImageDraw.Draw(collage)
    font = ImageFont.load_default()

    for i, img in enumerate(successful_images):
        row, col = i // columns, i % columns
        x_offset, y_offset = col * tile_size, row * tile_size
        collage.paste(img, (x_offset, y_offset))
        
        # 绘制黑底白字的序号标签 (1, 2, 3...)
        label = str(i + 1)
        bg_box = [x_offset + 5, y_offset + 5, x_offset + 35, y_offset + 35]
        draw.rectangle(bg_box, fill="black")
        draw.text((x_offset + 15, y_offset + 15), label, fill="white", font=font, anchor="mm")

    buffer = io.BytesIO()
    collage.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue(), successful_urls

async def create_collage(image_urls: list[str]) -> tuple[Optional[bytes], list[str]]:
    """
    异步调度图片下载与拼接任务。
    
    Args:
        image_urls (list[str]): 待下载的图片URL列表
        
    Returns:
        tuple: (拼接后的图片二进制数据, 成功参与拼接的URL列表)
    """
    tasks = [download_image(url) for url in image_urls]
    results = await asyncio.gather(*tasks)
    
    loop = asyncio.get_running_loop()
    collage_bytes, successful_urls = await loop.run_in_executor(
        None,
        _create_collage_sync,
        results,
        image_urls
    )
    return collage_bytes, successful_urls
