# -*- coding: utf-8 -*-
import io
import math
import asyncio
import aiohttp
import ssl
from typing import Optional
from PIL import Image, ImageDraw, ImageFont
from astrbot.api import logger

TILE_SIZE = 300

async def download_image(url: str) -> Optional[bytes]:
    # 核心修改：创建不检查证书的 SSL 上下文
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"
    }
    
    try:
        # 增加 trust_env=True 并使用不校验的 connector
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        async with aiohttp.ClientSession(headers=headers, connector=connector, trust_env=True) as session:
            async with session.get(url, timeout=15) as resp:
                if resp.status == 200:
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
        if res: successful_items.append((url, res))
    return successful_items

def _create_collage_sync(items: list[tuple[str, bytes]]) -> tuple[Optional[bytes], list[tuple[str, bytes]]]:
    successful_images, valid_items = [], []
    for url, img_bytes in items:
        try:
            with Image.open(io.BytesIO(img_bytes)) as img:
                successful_images.append(img.convert("RGB").resize((TILE_SIZE, TILE_SIZE), Image.Resampling.LANCZOS))
                valid_items.append((url, img_bytes))
        except: continue
    if not successful_images: return None, []
    columns = math.ceil(math.sqrt(len(successful_images)))
    rows = math.ceil(len(successful_images) / columns)
    collage = Image.new('RGB', (columns * TILE_SIZE, rows * TILE_SIZE), (255, 255, 255))
    draw = ImageDraw.Draw(collage)
    for i, img in enumerate(successful_images):
        row, col = i // columns, i % columns
        x, y = col * TILE_SIZE, row * TILE_SIZE
        collage.paste(img, (x, y))
        draw.rectangle([x + 5, y + 5, x + 35, y + 35], fill="black")
        draw.text((x + 12, y + 10), str(i + 1), fill="white")
    buffer = io.BytesIO()
    collage.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue(), valid_items

async def create_collage_from_items(items: list[tuple[str, bytes]]) -> tuple[Optional[bytes], list[tuple[str, bytes]]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _create_collage_sync, items)
