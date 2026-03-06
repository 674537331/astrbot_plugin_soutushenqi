# -*- coding: utf-8 -*-
"""
图像处理与下载模块
重构版：解耦了下载与拼贴逻辑。提供并发批量下载能力，并在内存中保留原图以避免重复请求。
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
    """下载单张图片到内存中，包含强力防盗链绕过策略。"""
    referer = "https://www.google.com/"
    if 'baidu.com' in url or 'bdimg.com' in url:
        referer = "https://image.baidu.com/"
    elif 'duitang.com' in url:
        referer = "https://www.duitang.com/"
    elif 'bilibili.com' in url or 'hdslb.com' in url:
        referer = "https://www.bilibili.com/"
        
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": referer,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Connection": "keep-alive"
    }
    
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=15) as resp:
                if resp.status == 200:
                    content_type = resp.headers.get('Content-Type', '').lower()
                    if 'text/html' in content_type:
                        return None
                    return await resp.read()
                return None
    except Exception:
        return None

async def download_image_batch(urls: list[str]) -> list[tuple[str, bytes]]:
    """
    并发下载多个图片。
    Returns:
        包含成功下载的 (URL, 图片二进制数据) 的列表。
    """
    tasks = [download_image(url) for url in urls]
    results = await asyncio.gather(*tasks)
    
    successful_items = []
    for url, res in zip(urls, results):
        if res:
            successful_items.append((url, res))
    return successful_items

def _create_collage_sync(items: list[tuple[str, bytes]]) -> tuple[Optional[bytes], list[tuple[str, bytes]]]:
    """
    同步拼接网格图。
    Returns:
        (拼接图的 bytes, 实际成功参与拼图的 items 列表)
    """
    successful_images = []
    valid_items = []
    tile_size = 300
    
    for url, img_bytes in items:
        try:
            img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            img = img.resize((tile_size, tile_size), Image.Resampling.LANCZOS)
            successful_images.append(img)
            valid_items.append((url, img_bytes))
        except (IOError, UnidentifiedImageError):
            continue

    if not successful_images:
        return None, []

    columns = math.ceil(math.sqrt(len(successful_images)))
    rows = math.ceil(len(successful_images) / columns)
    
    collage = Image.new('RGB', (columns * tile_size, rows * tile_size), (255, 255, 255))
    draw = ImageDraw.Draw(collage)
    font = ImageFont.load_default()

    for i, img in enumerate(successful_images):
        row, col = i // columns, i % columns
        x_offset, y_offset = col * tile_size, row * tile_size
        collage.paste(img, (x_offset, y_offset))
        
        label = str(i + 1)
        bg_box = [x_offset + 5, y_offset + 5, x_offset + 35, y_offset + 35]
        draw.rectangle(bg_box, fill="black")
        draw.text((x_offset + 15, y_offset + 15), label, fill="white", font=font, anchor="mm")

    buffer = io.BytesIO()
    collage.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue(), valid_items

async def create_collage_from_items(items: list[tuple[str, bytes]]) -> tuple[Optional[bytes], list[tuple[str, bytes]]]:
    """
    异步调度拼图任务。接收已下载的图源库进行拼贴。
    """
    loop = asyncio.get_running_loop()
    collage_bytes, valid_items = await loop.run_in_executor(
        None,
        _create_collage_sync,
        items
    )
    return collage_bytes, valid_items
