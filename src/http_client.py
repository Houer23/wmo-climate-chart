"""请求层：主请求头构造、超时、指数退避重试、BOM 解码、本地缓存。

实测要点
--------
* 目标站点存在**间歇性 TLS 断连**（``UNEXPECTED_EOF_WHILE_READING``），
  因此重试不是可选项，而是必需项。
* 数据文件返回 ``Content-Type: application/xml`` 但实际内容是 **JSON**，
  且带 UTF-8 BOM，须按 ``utf-8-sig`` 解码。
* 无效 cityId 返回 **HTTP 404**，这类错误不重试。

此外本模块做**进程级请求节流**：批量成图时连续请求之间会排队等待，
默认 ``fetch.min_interval = 1.0`` 秒，即每秒最多 1 次请求。
"""

from __future__ import annotations

import hashlib
import json
import socket
import ssl
import threading
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

# 进程内共享的上一次请求时刻：同一次运行里的多个 HttpClient（如先取城市索引、
# 再逐个取城市数据）也一起排队，避免"两个客户端各自计数"导致连发。
_last_request_at: float = 0.0


def throttle(min_interval: float, logger=None, clock=None, sleep=None) -> float:
    """按最小间隔排队：距上次请求不足 ``min_interval`` 秒时先等待，返回等待秒数。

    间隔取自当次调用的配置；``min_interval <= 0`` 表示不限速。首次请求不等待。
    """
    global _last_request_at
    interval = float(min_interval or 0.0)
    if interval <= 0:
        return 0.0
    now_fn = clock or time.monotonic
    sleep_fn = sleep or time.sleep
    now = now_fn()
    wait = interval - (now - _last_request_at) if _last_request_at else 0.0
    if wait > 0:
        if logger is not None:
            logger.debug(f"限速：等待 {wait:.2f}s（相邻请求间隔不小于 {interval:g}s）")
        sleep_fn(wait)
    else:
        wait = 0.0
    _last_request_at = now_fn()
    return wait


class _NoProxyHandler(urllib.request.ProxyHandler):
    """显式「直连」处理器：关掉代理，且保证真的被注册进 opener。

    不要直接用 ``ProxyHandler({})``：空映射不会生成任何 ``<scheme>_open`` 方法，
    ``OpenerDirector.add_handler`` 发现无可挂载方法就**不注册它**（只剩 ``build_opener``
    顺带跳过默认代理的副作用），既依赖实现细节、也无法从 ``opener.handlers`` 观察到。
    这里给各协议显式挂一个返回 ``None`` 的 ``*_open``——返回 ``None`` 表示"本处理器不
    处理"，请求继续交给链上后续的 ``HTTPHandler`` / ``HTTPSHandler``，即直连。
    """

    def __init__(self, schemes: tuple[str, ...] = ("http", "https", "ftp")) -> None:
        super().__init__({})
        for scheme in schemes:
            setattr(self, f"{scheme}_open", self._decline)

    @staticmethod
    def _decline(_req: Any, *_args: Any) -> None:
        return None


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
        self.min_interval = float(self.cfg.get("min_interval", 1.0) or 0.0)
        self.verify_ssl = bool(self.cfg.get("verify_ssl", True))
        self.proxy = (self.cfg.get("proxy") or "").strip()
        # 无显式代理时是否允许环境变量（HTTP_PROXY / HTTPS_PROXY / …）暗中接管。
        # 默认 false：代理只认 fetch.proxy，避免"配置写着空、实际走了 env 里的死代理"。
        self.use_env_proxy = bool(self.cfg.get("use_env_proxy", False))
        self.opener = self._build_opener()
        # DNS 预解析超时（秒）；0 = 不做预检查。详见 _precheck_dns。
        self.resolve_timeout = float(self.cfg.get("resolve_timeout", 10.0) or 0.0)
        self._resolved_hosts: set[str] = set()   # 进程内已确认可解析的主机，避免重复预解析

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
        note: str = "",
    ) -> FetchResult:
        """GET 请求，带缓存与指数退避重试。404 立即抛 CityNotFoundError。

        ``note`` 是这次请求的**业务说明**（如「页面校验 cityId 237」），只用于日志：发起前
        打一条 INFO「请求：…」，完成后打一条 DEBUG（状态码/字节数/耗时/第几次尝试）。这样
        "卡在哪个请求上"一眼可见——此前请求阶段完全静默，网络慢时看起来就像卡死。
        """
        desc = note or url
        if use_cache:
            cached = self._read_cache(url)
            if cached is not None:
                self._log("debug", f"命中缓存（{desc}）：{url}")
                return cached

        headers = dict(self.headers)
        if accept:
            headers["Accept"] = accept
        if extra_headers:
            headers.update(extra_headers)

        self._log("info", f"请求：{desc}")
        started = time.monotonic()
        last_error: Optional[Exception] = None
        for attempt in range(1, self.retries + 2):
            throttle(self.min_interval, self.logger)   # 连带重试一起限速，每秒最多 1 次请求
            self.request_count += 1
            try:
                result = self._request_once(url, headers)
                self._write_cache(url, result.status, result.content, result.headers)
                self._write_raw(url, result.content)
                result.attempts = attempt
                self._log("debug", f"完成（{desc}）：HTTP {result.status}，{len(result.content)} 字节，"
                                   f"{time.monotonic() - started:.1f}s，第 {attempt} 次尝试")
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
                self._log("warning", f"网络异常（{type(exc).__name__}: {exc}），"
                                     f"第 {attempt} 次尝试失败：{url}")

            if attempt <= self.retries:
                self.retry_count += 1
                delay = min(self.backoff * (2 ** (attempt - 1)), self.backoff_max)
                self._log("info", f"{delay:.1f}s 后重试（{attempt}/{self.retries}）…")
                time.sleep(delay)

        raise FetchError(
            f"请求失败，已重试 {self.retries} 次：{url}（原因：{type(last_error).__name__}: {last_error}）"
        )

    def _build_opener(self) -> urllib.request.OpenerDirector:
        """按配置构造 opener：代理与证书校验都只认**配置**，不让环境变量暗中接管。

        * ``fetch.proxy`` 非空 → 只走该代理；
        * ``fetch.proxy`` 为空 → **显式禁用**代理。挂上 ``_NoProxyHandler`` 会顶掉 urllib
          默认从 ``HTTP_PROXY`` / ``HTTPS_PROXY`` 等环境变量派生的那个 ProxyHandler；否则
          env 里的代理指向失效端口时，配置写 ``""`` 也会把请求全送去死地址（实测踩过）；
        * 仅 ``fetch.use_env_proxy=true`` 时才交回 urllib 默认行为（沿用 env 代理）。

        ``verify_ssl=false`` 用 ``HTTPSHandler(context=…)`` 生效，**代理模式下同样有效**
        —— 旧实现只给 opener 装了 ProxyHandler，会把该开关静默丢掉。
        """
        context: Optional[ssl.SSLContext] = None
        if not self.verify_ssl:
            context = ssl._create_unverified_context()  # noqa: S323 - 由配置显式开启
        if not self.proxy and self.use_env_proxy:
            return urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=context))
        handlers: list[Any] = [
            urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
            if self.proxy else _NoProxyHandler()
        ]
        if context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=context))
        return urllib.request.build_opener(*handlers)

    def _precheck_dns(self, url: str) -> None:
        """带超时的 DNS 预解析，避免坏 DNS 让进程"无限静默"。

        ``urllib`` 的 ``timeout`` 只作用于 socket 的连接与读写，**不覆盖** ``getaddrinfo``：
        DNS 被劫持或不可达时，进程会长时间既没有结果也没有告警——实测的"卡住不动"就有这种
        成因。这里把解析放进守护线程、只等 ``fetch.resolve_timeout`` 秒，超时按
        ``socket.timeout`` 抛出，交给既有的重试与告警链路处理。

        同一主机在进程内只要解析成功过一次就不再重复预检查；``fetch.resolve_timeout=0`` 可
        整体关闭该预检查（回到"完全交给系统解析"的行为）。
        """
        limit = self.resolve_timeout
        if limit <= 0:
            return
        host = urllib.parse.urlsplit(url).hostname
        if not host or host in self._resolved_hosts:
            return
        result: dict[str, Any] = {}
        done = threading.Event()

        def worker() -> None:
            try:
                socket.getaddrinfo(host, None)
            except BaseException as exc:        # noqa: BLE001 - 原样回传给主线程
                result["error"] = exc
            finally:
                done.set()

        threading.Thread(target=worker, name="dns-precheck", daemon=True).start()
        if not done.wait(limit):
            raise socket.timeout(f"DNS 解析超时（>{limit:g}s，host={host}）："
                                 f"请检查本机 DNS / hosts 与 VPN 分流")
        error = result.get("error")
        if error is not None:
            raise error
        self._resolved_hosts.add(host)

    def _request_once(self, url: str, headers: dict[str, str]) -> FetchResult:
        self._precheck_dns(url)               # DNS 不被 socket timeout 覆盖，单独兜一层超时
        req = urllib.request.Request(url, headers=headers, method="GET")
        response = self.opener.open(req, timeout=self.timeout)
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
