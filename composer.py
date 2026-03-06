# -*- coding: utf-8 -*-
import io
import math
import asyncio
import aiohttp
from typing import Optional
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError
from astrbot.api import logger

TILE_SIZE = 300
MAX_IMAGE_SIZE = 20 * 1024 * 1024  

# --- 全局 Session 懒加载管理 ---
_composer_session = None
_composer_session_lock = None

async def _get_composer_lock():
    """安全懒加载 asyncio 对象，防止跨循环错误"""
    global _composer_session_lock
    if _composer_session_lock is None:
        _composer_session_lock = asyncio.Lock()
    return _composer_session_lock

async def get_composer_session() -> aiohttp.ClientSession:
    global _composer_session
    lock = await _get_composer_lock()
    async with lock:
        if _composer_session is None or _composer_session.closed:
            _composer_session = aiohttp.ClientSession()
    return _composer_session

async def close_composer_session():
    global _composer_session
    lock = await _get_composer_lock()
    async with lock:
        if _composer_session and not _composer_session.closed:
            await _composer_session.close()
            _composer_session = None

async def download_image(session: aiohttp.ClientSession, semaphore: asyncio.Semaphore, url: str) -> Optional[bytes]:
    async with semaphore:
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
            "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"
        }
        
        req_timeout = aiohttp.ClientTimeout(total=15, connect=5)
        
        try:
            async with session.get(url, headers=headers, timeout=req_timeout) as resp:
                if resp.status == 200:
                    content_type = resp.headers.get('Content-Type', '').lower()
                    if 'text/html' in content_type:
                        return None
                    
                    # 修复：更容错的请求头解析
                    content_length_str = resp.headers.get('Content-Length', '0')
                    try:
                        content_length = int(content_length_str)
                    except ValueError:
                        content_length = 0
                        
                    if content_length > MAX_IMAGE_SIZE:
                        logger.warning(f"下载拒绝：目标文件超出安全大小: {url}")
                        return None
                        
                    return await resp.read()
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.debug(f"并发下载时网络连接或超时失败 ({url}): {e}")
            return None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"并发下载时发生未知异常 ({url}): {e}")
            return None

async def download_image_batch(urls: list[str]) -> list[tuple[str, bytes]]:
    semaphore = asyncio.Semaphore(10) 
    session = await get_composer_session()
    
    tasks = [download_image(session, semaphore, url) for url in urls]
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
        
        bg_box = [x_offset + 5, y_offset + 5, x_offset + 35, y_offset + 35]
        draw.rectangle(bg_box, fill="black")
        draw.text((x_offset + 15, y_offset + 15), str(i + 1), fill="white", font=font, anchor="mm")

    with io.BytesIO() as buffer:
        collage.save(buffer, format="JPEG", quality=85)
        return buffer.getvalue(), valid_items

async def create_collage_from_items(items: list[tuple[str, bytes]]) -> tuple[Optional[bytes], list[tuple[str, bytes]]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _create_collage_sync, items)
