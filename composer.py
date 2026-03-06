# -*- coding: utf-8 -*-
"""
图像处理与下载模块
重构版：解耦了下载与拼贴逻辑，提供并发批量下载。
提取了魔法常量 TILE_SIZE。合规化了框架日志导入。
修复：引入上下文管理器关闭图片对象，杜绝批量下载处理引发的 OOM (内存溢出) 问题。
"""
import io
import math
import asyncio
import aiohttp
from typing import Optional
from PIL import Image, ImageDraw, ImageFont
from astrbot.api import logger

# 将魔法数字提取为常量，未来如果改 16 宫格或大图版，只需改这里
TILE_SIZE = 300

async def download_image(url: str) -> Optional[bytes]:
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
    except Exception as e:
        logger.debug(f"单图下载失败 ({url}): {e}")
        return None

async def download_image_batch(urls: list[str]) -> list[tuple[str, bytes]]:
    tasks = [download_image(url) for url in urls]
    results = await asyncio.gather(*tasks)
    
    successful_items = []
    for url, res in zip(urls, results):
        if res:
            successful_items.append((url, res))
    return successful_items

def _create_collage_sync(items: list[tuple[str, bytes]]) -> tuple[Optional[bytes], list[tuple[str, bytes]]]:
    successful_images = []
    valid_items = []
    
    for url, img_bytes in items:
        try:
            # 修复中等隐患：使用 with 语句确保底层文件和内存句柄被安全释放
            with Image.open(io.BytesIO(img_bytes)) as img:
                # convert 会返回新图，原有 img 会被 with 安全关闭
                rgb_img = img.convert("RGB")
                resized_img = rgb_img.resize((TILE_SIZE, TILE_SIZE), Image.Resampling.LANCZOS)
                successful_images.append(resized_img)
                valid_items.append((url, img_bytes))
        except OSError:  # 简化：UnidentifiedImageError 和 IOError 都是 OSError 的子类/别名
            continue

    if not successful_images:
        return None, []

    columns = math.ceil(math.sqrt(len(successful_images)))
    rows = math.ceil(len(successful_images) / columns)
    
    collage = Image.new('RGB', (columns * TILE_SIZE, rows * TILE_SIZE), (255, 255, 255))
    draw = ImageDraw.Draw(collage)
    font = ImageFont.load_default()

    for i, img in enumerate(successful_images):
        row, col = i // columns, i % columns
        x_offset, y_offset = col * TILE_SIZE, row * TILE_SIZE
        collage.paste(img, (x_offset, y_offset))
        
        label = str(i + 1)
        bg_box = [x_offset + 5, y_offset + 5, x_offset + 35, y_offset + 35]
        draw.rectangle(bg_box, fill="black")
        draw.text((x_offset + 15, y_offset + 15), label, fill="white", font=font, anchor="mm")

    buffer = io.BytesIO()
    collage.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue(), valid_items

async def create_collage_from_items(items: list[tuple[str, bytes]]) -> tuple[Optional[bytes], list[tuple[str, bytes]]]:
    loop = asyncio.get_running_loop()
    collage_bytes, valid_items = await loop.run_in_executor(
        None,
        _create_collage_sync,
        items
    )
    return collage_bytes, valid_items
