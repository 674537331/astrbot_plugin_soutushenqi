# -*- coding: utf-8 -*-
import io
import math
import asyncio
import aiohttp
import ipaddress
from urllib.parse import urlparse
from typing import Optional
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError
from astrbot.api import logger

TILE_SIZE = 300
MAX_IMAGE_SIZE = 10 * 1024 * 1024  

_composer_session = None
_composer_session_lock = None
_global_dl_semaphore = None

async def _get_composer_lock():
    global _composer_session_lock
    if _composer_session_lock is None:
        _composer_session_lock = asyncio.Lock()
    return _composer_session_lock

async def _get_dl_semaphore():
    global _global_dl_semaphore
    lock = await _get_composer_lock()
    async with lock:
        if _global_dl_semaphore is None:
            _global_dl_semaphore = asyncio.Semaphore(10)
    return _global_dl_semaphore

async def get_composer_session() -> aiohttp.ClientSession:
    global _composer_session
    lock = await _get_composer_lock()
    async with lock:
        if _composer_session is None or _composer_session.closed:
            _composer_session = aiohttp.ClientSession()
    return _composer_session

async def close_composer_session():
    global _composer_session, _global_dl_semaphore
    lock = await _get_composer_lock()
    async with lock:
        if _composer_session and not _composer_session.closed:
            await _composer_session.close()
            _composer_session = None
        # 🚀 修复热重载场景下锁不释放的问题 🚀
        _global_dl_semaphore = None

def is_safe_url(url: str) -> bool:
    """🚀 防御 SSRF：拦截内网探测与元数据盗取 🚀"""
    try:
        host = urlparse(url).hostname
        if not host: return False
        if host.lower() in ('localhost', '127.0.0.1', '0.0.0.0', '::1'): return False
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                return False
        except ValueError:
            pass # 是域名而非直连 IP，放行
        return True
    except Exception:
        return False

async def download_image(session: aiohttp.ClientSession, semaphore: asyncio.Semaphore, url: str) -> Optional[bytes]:
    if not is_safe_url(url):
        logger.warning(f"触发 SSRF 安全防御，阻断恶意请求: {url}")
        return None
        
    async with semaphore:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"
        }
        req_timeout = aiohttp.ClientTimeout(total=15, connect=5)
        try:
            async with session.get(url, headers=headers, timeout=req_timeout) as resp:
                if resp.status != 200: return None
                content_type = resp.headers.get('Content-Type', '').lower()
                if 'text/html' in content_type: return None
                
                chunks = []
                downloaded_size = 0
                async for chunk in resp.content.iter_chunked(1024 * 1024):
                    downloaded_size += len(chunk)
                    if downloaded_size > MAX_IMAGE_SIZE:
                        logger.warning(f"触发 OOM 防御，截断下载: {url}")
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.debug(f"网络连接或超时 ({url}): {e}")
            return None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"下载图片时发生未预料异常 ({url}): {e}")
            return None

async def download_image_batch(urls: list[str]) -> list[tuple[str, bytes]]:
    semaphore = await _get_dl_semaphore()
    session = await get_composer_session()
    tasks = [download_image(session, semaphore, url) for url in urls]
    results = await asyncio.gather(*tasks)
    return [(u, r) for u, r in zip(urls, results) if r]

def _get_large_font():
    try:
        return ImageFont.truetype("arial.ttf", 36)
    except IOError:
        try:
            return ImageFont.truetype("DejaVuSans.ttf", 36)
        except IOError:
            return ImageFont.load_default()

def _create_collage_sync(items: list[tuple[str, bytes]]) -> tuple[Optional[bytes], list[tuple[str, bytes]]]:
    successful_images, valid_items = [], []
    for url, img_bytes in items:
        try:
            img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            img = img.resize((TILE_SIZE, TILE_SIZE), Image.Resampling.LANCZOS)
            successful_images.append(img)
            valid_items.append((url, img_bytes))
        except (IOError, UnidentifiedImageError) as e:
            logger.debug(f"丢弃无法识别或损坏的图像数据 ({url}): {e}")
            continue

    if not successful_images: return None, []

    columns = math.ceil(math.sqrt(len(successful_images)))
    rows = math.ceil(len(successful_images) / columns)
    
    collage = Image.new('RGB', (columns * TILE_SIZE, rows * TILE_SIZE), (255, 255, 255))
    draw = ImageDraw.Draw(collage)
    font = _get_large_font()
    
    # 检测是否降级到了默认字体 (默认字体没有 size 属性)
    is_default_font = getattr(font, 'size', None) is None

    for i, img in enumerate(successful_images):
        row, col = i // columns, i % columns
        x_offset, y_offset = col * TILE_SIZE, row * TILE_SIZE
        collage.paste(img, (x_offset, y_offset))
        
        bg_box = [x_offset + 5, y_offset + 5, x_offset + 60, y_offset + 50]
        draw.rectangle(bg_box, fill="black")
        
        # 🚀 绝杀 VLM 近视眼：若只能用微小的默认点阵字体，则暴力拉伸放大 🚀
        if is_default_font:
            txt_img = Image.new('RGBA', (40, 20), (0, 0, 0, 0))
            ImageDraw.Draw(txt_img).text((0, 0), str(i + 1), fill="white", font=font)
            # 使用 NEAREST 保持像素级锐利度
            txt_img = txt_img.resize((80, 40), Image.Resampling.NEAREST)
            collage.paste(txt_img, (x_offset + 10, y_offset + 10), txt_img)
        else:
            draw.text((x_offset + 15, y_offset + 10), str(i + 1), fill="white", font=font)

    with io.BytesIO() as buffer:
        collage.save(buffer, format="JPEG", quality=85)
        return buffer.getvalue(), valid_items

async def create_collage_from_items(items: list[tuple[str, bytes]]) -> tuple[Optional[bytes], list[tuple[str, bytes]]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _create_collage_sync, items)
