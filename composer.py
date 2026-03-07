# -*- coding: utf-8 -*-
import io
import math
import asyncio
import aiohttp
import socket
import ipaddress
from urllib.parse import urlparse
from typing import Optional, List, Tuple

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError, ImageOps
from astrbot.api import logger

TILE_SIZE = 300
MAX_IMAGE_SIZE = 10 * 1024 * 1024  

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
                    raise SSRFInterceptError("SSRF拦截机制生效：检测到受限网络地址。")
            except ValueError as e:
                if "SSRF" in str(e): raise
        return resolved

def is_safe_url_host(url: str) -> bool:
    try:
        host = urlparse(url).hostname
        if not host: return False
        try:
            ip = ipaddress.ip_address(host)
            if (ip.is_private or ip.is_loopback or ip.is_link_local or 
                ip.is_multicast or getattr(ip, 'is_reserved', False) or ip.is_unspecified):
                return False
        except ValueError:
            pass 
        return True
    except Exception:
        return False

def _get_large_font():
    try:
        return ImageFont.truetype("arial.ttf", 36)
    except IOError:
        try:
            return ImageFont.truetype("DejaVuSans.ttf", 36)
        except IOError:
            return ImageFont.load_default()

def _create_collage_sync(items: List[Tuple[str, bytes]]) -> Tuple[Optional[bytes], List[Tuple[str, bytes]]]:
    successful_images, valid_items = [], []
    for url, img_bytes in items:
        try:
            with Image.open(io.BytesIO(img_bytes)) as img:
                # 过滤低分辨率图像（单边小于 500 像素即视为缩略图抛弃）
                if img.width < 500 or img.height < 500:
                    logger.debug(f"剔除低分辨率图像 ({img.width}x{img.height}): {url}")
                    continue
                
                # 等比例无损裁剪，防止拉伸
                converted_img = ImageOps.fit(img.convert("RGB"), (TILE_SIZE, TILE_SIZE), method=Image.Resampling.LANCZOS)
                successful_images.append(converted_img)
                valid_items.append((url, img_bytes))
        except Exception as e:
            logger.debug(f"过滤损坏或无法识别格式的图像数据: {e}")
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

class ComposerManager:
    def __init__(self):
        self._session = None
        self._semaphore = None
        self._lock = None

    async def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _get_semaphore(self) -> asyncio.Semaphore:
        lock = await self._get_lock()
        async with lock:
            if self._semaphore is None:
                self._semaphore = asyncio.Semaphore(10)
        return self._semaphore

    async def _get_session(self) -> aiohttp.ClientSession:
        lock = await self._get_lock()
        async with lock:
            if self._session is None or self._session.closed:
                connector = aiohttp.TCPConnector(resolver=SafeResolver())
                self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close_all(self):
        lock = await self._get_lock()
        async with lock:
            if self._session and not self._session.closed:
                await self._session.close()
                self._session = None
            self._semaphore = None

    async def _download_image(self, url: str) -> Optional[bytes]:
        if not is_safe_url_host(url):
            return None

        semaphore = await self._get_semaphore()
        session = await self._get_session()

        async with semaphore:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"
            }
            # 快速失败机制：对于低响应速率的图床设定严格限制
            req_timeout = aiohttp.ClientTimeout(connect=5, sock_read=8)
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
                            # 遵从审查意见：依赖 async with 自动关闭连接，移除手动 resp.close()
                            return None
                        chunks.append(chunk)
                    return b"".join(chunks)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.debug(f"连接或传输超时 ({url}): {e}")
                return None
            except asyncio.CancelledError:
                raise
            except SSRFInterceptError:
                return None
            except Exception:
                return None

    async def download_image_batch(self, urls: List[str]) -> List[Tuple[str, bytes]]:
        tasks = [self._download_image(url) for url in urls]
        results = await asyncio.gather(*tasks)
        return [(u, r) for u, r in zip(urls, results) if r]

    async def create_collage_from_items(self, items: List[Tuple[str, bytes]]) -> Tuple[Optional[bytes], List[Tuple[str, bytes]]]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _create_collage_sync, items)
