# -*- coding: utf-8 -*-
import io
import math
import asyncio
import aiohttp
from typing import Optional
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError
import logging

logger = logging.getLogger("astrbot")

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
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": referer,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
    }
    
    try:
        # 加入连接超时，防止卡死
        timeout = aiohttp.ClientTimeout(total=15, connect=5)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    content_type = resp.headers.get('Content-Type', '').lower()
                    if 'text/html' in content_type: return None
                    return await resp.read()
                return None
    except Exception:
        return None

async def download_image_batch(urls: list[str]) -> list[tuple[str, bytes]]:
    tasks = [download_image(url) for url in urls]
    results = await asyncio.gather(*tasks)
    return [(u, r) for u, r in zip(urls, results) if r]

def _create_collage_sync(items: list[tuple[str, bytes]]) -> tuple[Optional[bytes], list[tuple[str, bytes]]]:
    successful_images, valid_items = [], []
    
    for url, img_bytes in items:
        try:
            img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            img = img.resize((TILE_SIZE, TILE_SIZE), Image.Resampling.LANCZOS)
            successful_images.append(img)
            valid_items.append((url, img_bytes))
        except (IOError, UnidentifiedImageError):
            continue

    if not successful_images: return None, []

    columns = math.ceil(math.sqrt(len(successful_images)))
    rows = math.ceil(len(successful_images) / columns)
    
    collage = Image.new('RGB', (columns * TILE_SIZE, rows * TILE_SIZE), (255, 255, 255))
    draw = ImageDraw.Draw(collage)
    font = ImageFont.load_default()

    for i, img in enumerate(successful_images):
        row, col = i // columns, i % columns
        x_offset, y_offset = col * TILE_SIZE, row * TILE_SIZE
        collage.paste(img, (x_offset, y_offset))
        
        bg_box = [x_offset + 5, y_offset + 5, x_offset + 35, y_offset + 35]
        draw.rectangle(bg_box, fill="black")
        draw.text((x_offset + 15, y_offset + 15), str(i + 1), fill="white", font=font, anchor="mm")

    buffer = io.BytesIO()
    collage.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue(), valid_items

async def create_collage_from_items(items: list[tuple[str, bytes]]) -> tuple[Optional[bytes], list[tuple[str, bytes]]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _create_collage_sync, items)
