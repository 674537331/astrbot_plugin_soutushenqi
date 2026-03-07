# -*- coding: utf-8 -*-
import io
import math
import asyncio
import aiohttp
import socket
import ipaddress
from urllib.parse import urlparse
from typing import Optional
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError
from astrbot.api import logger

TILE_SIZE = 300
MAX_IMAGE_SIZE = 10 * 1024 * 1024  

_composer_session = None
_global_dl_semaphore = None
_composer_lock = None

# 🚀 修复隐患：强制在协程环境内获取事件循环并实例化 Lock 🚀
async def get_composer_lock() -> asyncio.Lock:
    global _composer_lock
    if _composer_lock is None:
        _composer_lock = asyncio.Lock()
    return _composer_lock

class SSRFInterceptError(Exception):
    pass

class SafeResolver(aiohttp.DefaultResolver):
    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        resolved = await super().resolve(host, port, family)
        for info in resolved:
            ip_str = info['host']
            try:
                ip = ipaddress.ip_address(ip_str)
                if (ip.is_private or ip.is_loopback or ip.is_link_local or 
                    ip.is_multicast or getattr(ip, 'is_reserved', False) or ip.is_unspecified):
                    logger.error(f"SSRF 拦截：恶意域名 {host} 解析到危险 IP {ip_str}！")
                    raise SSRFInterceptError(f"SSRF 拦截：域名解析到内部/保留地址")
            except ValueError as e:
                if "SSRF" in str(e): raise
        return resolved

def is_safe_url_host(url: str) -> bool:
    """🚀 堵死漏洞：拦截直接以纯 IP 形式绕过 DNS Resolver 的 SSRF 攻击 🚀"""
    try:
        host = urlparse(url).hostname
        if not host: return False
        try:
            ip = ipaddress.ip_address(host)
            if (ip.is_private or ip.is_loopback or ip.is_link_local or 
                ip.is_multicast or getattr(ip, 'is_reserved', False) or ip.is_unspecified):
                return False
        except ValueError:
            pass # 是域名，交由下游 SafeResolver 审查真实解析 IP
        return True
    except Exception:
        return False

async def get_dl_semaphore() -> asyncio.Semaphore:
    global _global_dl_semaphore
    lock = await get_composer_lock()
    async with lock:
        if _global_dl_semaphore is None:
            _global_dl_semaphore = asyncio.Semaphore(10)
    return _global_dl_semaphore

async def get_composer_session() -> aiohttp.ClientSession:
    global _composer_session
    lock = await get_composer_lock()
    async with lock:
        if _composer_session is None or _composer_session.closed:
            connector = aiohttp.TCPConnector(resolver=SafeResolver())
            _composer_session = aiohttp.ClientSession(connector=connector)
    return _composer_session

async def close_composer_session():
    global _composer_session, _global_dl_semaphore
    lock = await get_composer_lock()
    async with lock:
        if _composer_session and not _composer_session.closed:
            await _composer_session.close()
            _composer_session = None
        _global_dl_semaphore = None

async def download_image(session: aiohttp.ClientSession, semaphore: asyncio.Semaphore, url: str) -> Optional[bytes]:
    if not is_safe_url_host(url):
        logger.warning(f"安全防御：拦截非法直接 IP 请求: {url}")
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
                        resp.close()
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.debug(f"网络连接或超时 ({url}): {e}")
            return None
        except asyncio.CancelledError:
            raise
        except SSRFInterceptError:
            return None
        except Exception as e:
            logger.warning(f"未预料下载异常 ({url}): {e}")
            return None

async def download_image_batch(urls: list[str]) -> list[tuple[str, bytes]]:
    semaphore = await get_dl_semaphore()
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
    is_default_font = getattr(font, 'size', None) is None

    for i, img in enumerate(successful_images):
        row, col = i // columns, i % columns
        x_offset, y_offset = col * TILE_SIZE, row * TILE_SIZE
        collage.paste(img, (x_offset, y_offset))
        
        bg_box = [x_offset + 5, y_offset + 5, x_offset + 60, y_offset + 50]
        draw.rectangle(bg_box, fill="black")
        
        if is_default_font:
            txt_img = Image.new('RGBA', (40, 20), (0, 0, 0, 0))
            ImageDraw.Draw(txt_img).text((0, 0), str(i + 1), fill="white", font=font)
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
