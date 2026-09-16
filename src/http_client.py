"""请求层：主请求头构造、超时、指数退避重试、BOM 解码、本地缓存。

实测要点
--------
* 目标站点存在**间歇性 TLS 断连**（``UNEXPECTED_EOF_WHILE_READING``），
  因此重试不是可选项，而是必需项。
* 数据文件返回 ``Content-Type: application/xml`` 但实际内容是 **JSON**，
  且带 UTF-8 BOM，须按 ``utf-8-sig`` 解码。
* 无效 cityId 返回 **HTTP 404**，这类错误不重试。
"""

from __future__ import annotations

import hashlib
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "identity",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}

# 这些 HTTP 状态码值得重试（服务端临时问题）
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    """网络层失败（已在内部重试过）。"""


class CityNotFoundError(FetchError):
    """cityId 不存在（HTTP 404）或城市没有气候数据文件。"""


@dataclass
class FetchResult:
    url: str
    status: int
    content: bytes
    text: str
    from_cache: bool = False
    attempts: int = 1
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.text)


def decode_bytes(content: bytes) -> str:
    """按 utf-8-sig 优先解码，退化到 gbk / latin-1。"""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


class HttpClient:
    """带重试与缓存的极简 HTTP 客户端（仅标准库）。"""

    def __init__(
        self,
        fetch_cfg: dict[str, Any],
        logger=None,
        cache_dir: Optional[Path] = None,
        raw_dir: Optional[Path] = None,
    ) -> None:
        self.cfg = fetch_cfg or {}
        self.logger = logger
        self.headers = dict(DEFAULT_HEADERS)
        self.headers.update(self.cfg.get("headers") or {})
        self.timeout = float(self.cfg.get("timeout", 30))
        self.retries = int(self.cfg.get("retries", 4))
        self.backoff = float(self.cfg.get("backoff", 1.2))
        self.backoff_max = float(self.cfg.get("backoff_max", 15))
        self.verify_ssl = bool(self.cfg.get("verify_ssl", True))
        self.proxy = (self.cfg.get("proxy") or "").strip()

        cache_cfg = self.cfg.get("cache") or {}
        self.cache_enabled = bool(cache_cfg.get("enabled", True))
        self.cache_ttl = float(cache_cfg.get("ttl", 86400))
        self.cache_dir = cache_dir
        if self.cache_enabled and self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.save_raw = bool(self.cfg.get("save_raw", False))
        self.raw_dir = raw_dir
        if self.save_raw and self.raw_dir is not None:
            self.raw_dir.mkdir(parents=True, exist_ok=True)

        # 请求计数（便于测试与统计）
        self.request_count = 0
        self.retry_count = 0

    # ---- 日志 -----------------------------------------------------------
    def _log(self, level: str, msg: str) -> None:
        if self.logger is None:
            return
        getattr(self.logger, level, self.logger.info)(msg)

    # ---- 缓存 -----------------------------------------------------------
    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
        assert self.cache_dir is not None
        return self.cache_dir / f"{key}.bin", self.cache_dir / f"{key}.meta.json"

    def _read_cache(self, url: str) -> Optional[FetchResult]:
        if not (self.cache_enabled and self.cache_dir):
            return None
        data_path, meta_path = self._cache_paths(url)
        if not (data_path.exists() and meta_path.exists()):
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        age = time.time() - float(meta.get("ts", 0))
        if age > self.cache_ttl:
            return None
        content = data_path.read_bytes()
        return FetchResult(
            url=url,
            status=int(meta.get("status", 200)),
            content=content,
            text=decode_bytes(content),
            from_cache=True,
            attempts=0,
            headers=meta.get("headers") or {},
        )

    def _write_cache(self, url: str, status: int, content: bytes, headers: dict[str, str]) -> None:
        if not (self.cache_enabled and self.cache_dir):
            return
        data_path, meta_path = self._cache_paths(url)
        try:
            data_path.write_bytes(content)
            meta_path.write_text(
                json.dumps(
                    {"url": url, "status": status, "ts": time.time(), "headers": headers},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError as exc:  # 缓存失败不影响主流程
            self._log("debug", f"缓存写入失败：{exc}")

    def _write_raw(self, url: str, content: bytes) -> None:
        if not (self.save_raw and self.raw_dir):
            return
        name = urllib.parse.urlparse(url).path.strip("/").replace("/", "_") or "index"
        try:
            (self.raw_dir / name).write_bytes(content)
        except OSError as exc:
            self._log("debug", f"原始响应落盘失败：{exc}")

    # ---- 请求 -----------------------------------------------------------
    def get(
        self,
        url: str,
        use_cache: bool = True,
        accept: Optional[str] = None,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> FetchResult:
        """GET 请求，带缓存与指数退避重试。404 立即抛 CityNotFoundError。"""
        if use_cache:
            cached = self._read_cache(url)
            if cached is not None:
                self._log("debug", f"命中缓存：{url}")
                return cached

        headers = dict(self.headers)
        if accept:
            headers["Accept"] = accept
        if extra_headers:
            headers.update(extra_headers)

        last_error: Optional[Exception] = None
        for attempt in range(1, self.retries + 2):
            self.request_count += 1
            try:
                result = self._request_once(url, headers)
                self._write_cache(url, result.status, result.content, result.headers)
                self._write_raw(url, result.content)
                result.attempts = attempt
                return result
            except CityNotFoundError:
                raise
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise CityNotFoundError(f"资源不存在（HTTP 404）：{url}") from exc
                if exc.code not in RETRY_STATUS:
                    raise FetchError(f"HTTP {exc.code}：{url}") from exc
                last_error = exc
                self._log("warning", f"HTTP {exc.code}，第 {attempt} 次尝试失败：{url}")
            except (urllib.error.URLError, ssl.SSLError, socket.timeout, TimeoutError, OSError) as exc:
                last_error = exc
                self._log("warning", f"网络异常（{type(exc).__name__}），第 {attempt} 次尝试失败：{url}")

            if attempt <= self.retries:
                self.retry_count += 1
                delay = min(self.backoff * (2 ** (attempt - 1)), self.backoff_max)
                self._log("info", f"{delay:.1f}s 后重试（{attempt}/{self.retries}）…")
                time.sleep(delay)

        raise FetchError(
            f"请求失败，已重试 {self.retries} 次：{url}（原因：{type(last_error).__name__}: {last_error}）"
        )

    def _request_once(self, url: str, headers: dict[str, str]) -> FetchResult:
        req = urllib.request.Request(url, headers=headers, method="GET")
        context: Optional[ssl.SSLContext] = None
        if not self.verify_ssl:
            context = ssl._create_unverified_context()  # noqa: S323 - 由配置显式开启
        if self.proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
            )
            response = opener.open(req, timeout=self.timeout)
        else:
            response = urllib.request.urlopen(req, timeout=self.timeout, context=context)
        with response:
            content = response.read()
            resp_headers = {k: v for k, v in response.headers.items()}
            status = int(getattr(response, "status", 200) or 200)
        return FetchResult(url=url, status=status, content=content, text=decode_bytes(content), headers=resp_headers)


# ---- URL 构造 ----------------------------------------------------------

def build_url(template: str, base_url: str, **kwargs: Any) -> str:
    """按模板拼 URL；模板中的占位参数缺失时保持原样以便报错定位。"""
    path = template
    for key, value in kwargs.items():
        path = path.replace("{" + key + "}", str(value))
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def city_page_url(cfg: dict[str, Any], city_id: int, lang: str) -> str:
    return build_url(
        cfg.get("city_page_path", "{lang}/city.html?cityId={city_id}"),
        cfg.get("base_url", "https://worldweather.wmo.int"),
        lang=lang,
        city_id=city_id,
    )


def city_data_url(cfg: dict[str, Any], city_id: int, lang: str) -> str:
    return build_url(
        cfg.get("data_path", "{lang}/json/{city_id}_{lang}.xml"),
        cfg.get("base_url", "https://worldweather.wmo.int"),
        lang=lang,
        city_id=city_id,
    )


def city_index_url(cfg: dict[str, Any], lang: str) -> str:
    return build_url(
        cfg.get("city_index_path", "{lang}/json/Country_{lang}.xml"),
        cfg.get("base_url", "https://worldweather.wmo.int"),
        lang=lang,
    )
