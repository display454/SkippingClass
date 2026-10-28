"""Opt-in external images: public HTTPS only, pinned connection, raster cache."""
from __future__ import annotations

import hashlib
import io
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urljoin, urlsplit


def remote_image_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if len(value) > 2000 or parsed.scheme != "https" or not host or parsed.username or parsed.password:
            return False
        if any(ord(character) < 33 for character in value) or any(character in value for character in ('\\', '<', '>', '"')):
            return False
        if parsed.port not in (None, 443) or host == "localhost" or "." not in host:
            return False
        if host.endswith((".localhost", ".local", ".internal")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return True
    except (ValueError, TypeError):
        return False


def cache_image(url: str, directory: Path) -> Path:
    import certifi
    import urllib3
    from PIL import Image
    original = url
    for _ in range(4):
        if not remote_image_url(url):
            raise ValueError("只允许公网 HTTPS 图片")
        host = urlsplit(url).hostname
        addresses = {entry[4][0] for entry in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError("图片地址指向本机或内网，已拒绝")
        parsed = urlsplit(url)
        # Pin the actual TCP peer as well as validating DNS; preserve certificate/SNI checks.
        pool = urllib3.HTTPSConnectionPool(sorted(addresses)[0], server_hostname=host,
                                         assert_hostname=host, cert_reqs="CERT_REQUIRED",
                                         ca_certs=certifi.where(), timeout=urllib3.Timeout(connect=5, read=10))
        response = None
        try:
            response = pool.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""),
                                    headers={"Host": host}, preload_content=False, redirect=False, retries=False)
            if response.status in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers.get("Location", ""))
                continue
            if response.status != 200 or not response.headers.get("Content-Type", "").lower().startswith("image/"):
                raise ValueError("外链没有返回可用图片")
            data = bytearray()
            for block in response.stream(65536):
                data.extend(block)
                if len(data) > 5 * 1024 * 1024:
                    raise ValueError("图片超过 5MB 限制")
        finally:
            if response is not None:
                response.close()
            pool.close()
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP", "GIF"} or image.width * image.height > 20_000_000:
                raise ValueError("图片类型或像素数量不受支持")
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / (hashlib.sha256(original.encode()).hexdigest()[:24] + ".png")
            image.convert("RGB").save(target)
            return target
    raise ValueError("图片重定向次数过多")
