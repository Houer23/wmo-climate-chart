"""编排层：把请求 → 解析 → 表格 → 图表串起来，并处理批量与多城市对比。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import table_writer
from .chart import (
    ChartError,
    render_city_chart,
    render_comparison_chart,
    render_multi_city_chart,
)
from .city_index import CityEntry, CityIndex, load_city_index
from .config_loader import resolve_grid
from .http_client import (
    CityNotFoundError,
    FetchError,
    HttpClient,
    city_data_url,
    city_page_url,
)
from .models import CityClimate, city_display_name, format_coord
from .parser import NoClimateDataError, ParseError, parse_city_text

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


@dataclass
class CityResult:
    """单个城市的处理结果。"""

    city_id: int
    city_name: str = ""
    ok: bool = False
    error: str = ""
    city: Optional[CityClimate] = None
    tables: list[Path] = field(default_factory=list)
    charts: list[Path] = field(default_factory=list)
    series: list[str] = field(default_factory=list)  # 本次实际绘出的要素名（图例名）
    profile: str = ""


@dataclass
class RunSummary:
    results: list[CityResult] = field(default_factory=list)
    compare_charts: list[Path] = field(default_factory=list)
    compare_series: list[str] = field(default_factory=list)  # 对比图实际绘出的要素
    multi_charts: list[Path] = field(default_factory=list)   # 多图产物
    multi_series: list[str] = field(default_factory=list)    # 多图实际绘出的要素
    grid: str = ""                                           # 多图画布，如 "2x2"
    drawn_ids: list[int] = field(default_factory=list)       # 多图里真正画出来的 cityId
    skipped_ids: list[int] = field(default_factory=list)     # 超出画布格数、未绘制的 cityId
    mode: str = "city"  # city | compare | multi

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    def describe(self) -> str:
        if self.mode in ("compare", "multi"):
            head = f"取数成功 {self.ok_count} 个，失败 {self.fail_count} 个"
            if self.mode == "compare":
                if self.compare_series:
                    head += f"，对比要素：{'、'.join(self.compare_series)}"
            else:
                if self.grid:
                    head += f"，画布 {self.grid}"
                if self.multi_series:
                    head += f"，绘出要素：{'、'.join(self.multi_series)}"
                if self.skipped_ids:
                    head += f"，超出格数未绘制 {len(self.skipped_ids)} 个"
            lines = [head]
            for r in self.results:
                state = "成功" if r.ok else "失败"
                suffix = "" if r.ok else f"：{r.error}"
                if self.mode == "multi" and r.ok:
                    suffix = ("（已绘制）" if r.city_id in self.drawn_ids
                              else "（超出画布格数，未绘制）")
                lines.append(f"  [{state}] {r.city_name or 'cityId ' + str(r.city_id)}"
                             f"（{r.city_id}）{suffix}")
            return "\n".join(lines)
        lines = [f"成功 {self.ok_count} 个，失败 {self.fail_count} 个"]
        for r in self.results:
            if r.ok:
                series_note = f"，绘出要素：{'、'.join(r.series)}" if r.series else ""
                lines.append(f"  [成功] {r.city_name}（{r.city_id}）："
                             f"{len(r.tables)} 个表格，{len(r.charts)} 张图{series_note}")
            else:
                lines.append(f"  [失败] cityId {r.city_id}：{r.error}")
        return "\n".join(lines)


# ---- 路径 --------------------------------------------------------------

def _resolve_dir(cfg: dict[str, Any], value: str, out_root: Optional[Path] = None) -> Path:
    """相对路径基于项目根目录解析；out_root 可覆盖输出根。"""
    path = Path(value)
    if path.is_absolute():
        return path
    base = out_root if (out_root is not None and out_root.is_absolute()) else PROJECT_ROOT
    return (base / path).resolve()


def paths_for(cfg: dict[str, Any], out_dir: Optional[Path] = None) -> dict[str, Path]:
    return {
        "out_dir": out_dir.resolve() if out_dir else _resolve_dir(cfg, cfg["output"]["out_dir"]),
        "cache_dir": _resolve_dir(cfg, (cfg["fetch"].get("cache") or {}).get("dir", "cache")),
        "raw_dir": _resolve_dir(cfg, cfg["fetch"].get("raw_dir", "cache/raw")),
    }


def make_client(cfg: dict[str, Any], logger=None, out_dir: Optional[Path] = None) -> HttpClient:
    dirs = paths_for(cfg, out_dir)
    return HttpClient(cfg["fetch"], logger=logger,
                      cache_dir=dirs["cache_dir"], raw_dir=dirs["raw_dir"])


# ---- 文件名 ------------------------------------------------------------

def _safe(text: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", str(text)).strip(" ._")
    return cleaned or "unnamed"


def _format_basename(cfg: dict[str, Any], city: CityClimate, key: str, default: str) -> str:
    """按模板生成文件名主干；模板为空时回退默认值。"""
    template = cfg["output"].get(key) or default
    return _safe(str(template).format(
        city=city_display_name(city, cfg) or f"city{city.city_id}",
        city_id=city.city_id,
        member=city.member.mem_name,
        station=city.station_name,
        profile=cfg.get("profile_name", ""),
        # 文档已声明 {lat} / {lon} 可用于文件名模板，这里补齐（否则 .format 会抛 KeyError）
        lat=format_coord(city.latitude, "lat", cfg),
        lon=format_coord(city.longitude, "lon", cfg),
    ))


def output_basename(cfg: dict[str, Any], city: CityClimate) -> str:
    """图片文件名主干（默认**带**配置名后缀）。"""
    return _format_basename(cfg, city, "name_template", "{city}_{city_id}_climate_{profile}")


def table_basename(cfg: dict[str, Any], city: CityClimate) -> str:
    """表格文件名主干（默认**不带**配置名后缀）。"""
    return _format_basename(cfg, city, "table_name_template", "{city}_{city_id}_climate")


def compare_basename(cfg: dict[str, Any], cities: list[CityClimate]) -> str:
    template = cfg["output"].get("compare_name_template", "{city_count}城对比_{metric}")
    metric = str((cfg.get("compare") or {}).get("metric", "metric"))
    names = "_".join(c.city_name for c in cities[:4]) or "compare"
    return _safe(template.format(
        city_count=len(cities), metric=metric, cities=names,
        profile=cfg.get("profile_name", ""),
    ))


def multi_basename(cfg: dict[str, Any], cities: list[CityClimate], grid: str) -> str:
    """多图文件名主干。占位符：``{city_count}`` / ``{cities}`` / ``{grid}`` / ``{profile}``。"""
    template = cfg["output"].get("multi_name_template", "{city_count}城多图_{grid}_{profile}")
    names = "_".join(city.city_name for city in cities[:4]) or "multi"
    return _safe(template.format(
        city_count=len(cities), cities=names, grid=grid,
        profile=cfg.get("profile_name", ""),
    ))


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return path.with_name(f"{path.stem}_{stamp}{path.suffix}")


def _apply_overwrite(paths: list[Path], policy: str) -> list[Path]:
    """按覆盖策略返回真正要写的路径；skip 时返回空列表。"""
    if policy == "timestamp":
        return [_unique(p) for p in paths]
    if policy == "skip":
        return [p for p in paths if not p.exists()]
    return paths


# ---- 取数 --------------------------------------------------------------

def _extract_page_title(html: str) -> str:
    match = _TITLE_RE.search(html)
    if not match:
        return ""
    text = re.sub(r"\s+", " ", match.group(1)).strip()
    return text


def verify_city_page(client: HttpClient, cfg: dict[str, Any], city_id: int, logger=None) -> str:
    """请求城市 HTML 页做存在性校验（实测页面本身不含气候数值）。

    返回页面标题，仅用于日志与提示，不参与数据解析。
    """
    url = city_page_url(cfg["fetch"], city_id, cfg["data"].get("lang", "zh"))
    try:
        result = client.get(url, use_cache=True, accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.8")
    except CityNotFoundError as exc:
        raise CityNotFoundError(f"城市页面不存在，cityId={city_id} 可能无效（{exc}）") from exc
    title = _extract_page_title(result.text)
    if logger:
        logger.debug(f"页面校验通过：cityId={city_id} 标题={title or '(无)'}")
    return title


def fetch_city(client: HttpClient, cfg: dict[str, Any], city_id: int, logger=None) -> CityClimate:
    """取一个城市的气候数据并解析。"""
    if cfg["fetch"].get("fetch_page_first", True):
        try:
            verify_city_page(client, cfg, city_id, logger)
        except FetchError as exc:
            # 页面校验失败不阻断：数据文件才是权威来源
            if logger:
                logger.warning(f"页面校验未通过，继续尝试数据文件：{exc}")

    url = city_data_url(cfg["fetch"], city_id, cfg["data"].get("lang", "zh"))
    result = client.get(url, accept="application/json,text/xml,*/*;q=0.8")
    city = parse_city_text(result.text, city_id=city_id, lang=cfg["data"].get("lang", "zh"))

    if city.mean_derived and logger:
        logger.info("数据源未提供平均气温，已按 (日均最高 + 日均最低) / 2 派生")
    if not city.rain_unit and logger:
        logger.debug("数据源未提供降水单位，按毫米处理")
    return city


# ---- 单城市流程 --------------------------------------------------------

def process_city(client: HttpClient, cfg: dict[str, Any], city_id: int,
                 logger=None, out_dir: Optional[Path] = None) -> CityResult:
    result = CityResult(city_id=city_id, profile=str(cfg.get("profile_name", "")))
    dirs = paths_for(cfg, out_dir)
    target_dir = dirs["out_dir"]
    try:
        city = fetch_city(client, cfg, city_id, logger)
    except (CityNotFoundError, NoClimateDataError, ParseError, FetchError) as exc:
        result.error = str(exc)
        if logger:
            logger.error(f"cityId {city_id} 取数失败：{exc}")
        return result

    result.city = city
    result.city_name = city.city_name
    target_dir.mkdir(parents=True, exist_ok=True)
    chart_base = output_basename(cfg, city)   # 图：带配置名后缀
    table_base = table_basename(cfg, city)    # 表格：不带配置名后缀
    policy = str(cfg["output"].get("overwrite", "overwrite"))

    # 表格
    table_paths = [target_dir / f"{table_base}{ext}"
                   for ext in _table_extensions(cfg)]
    try:
        for path in _apply_overwrite(table_paths, policy):
            table_writer.WRITERS[path.suffix.lstrip(".").lower()][0](path, city, cfg)
            result.tables.append(path)
    except Exception as exc:  # noqa: BLE001 - 表格失败不应阻断图表
        if logger:
            logger.error(f"表格写出失败：{exc}")

    # 图表
    chart_paths = [target_dir / f"{chart_base}.{fmt}"
                   for fmt in (cfg["output"].get("chart_formats") or [])]
    try:
        writable = _apply_overwrite(chart_paths, policy)
        if writable:
            report: dict[str, Any] = {}
            result.charts.extend(render_city_chart(city, cfg, writable, logger, report))
            result.series = [str(item.get("label", "")) for item in report.get("series") or []]
    except ChartError as exc:
        if logger:
            logger.warning(f"绘图跳过：{exc}")
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.error(f"绘图失败：{exc}")

    result.ok = True
    if logger:
        series_note = f"；绘出要素：{'、'.join(result.series)}" if result.series else ""
        logger.info(f"{city.city_name}（{city.city_id}）完成："
                    f"{len(result.tables)} 个表格，{len(result.charts)} 张图{series_note}")
    return result


def _table_extensions(cfg: dict[str, Any]) -> list[str]:
    exts: list[str] = []
    for fmt in cfg["output"].get("table_formats") or []:
        entry = table_writer.WRITERS.get(str(fmt).lower())
        if entry and entry[1] not in exts:
            exts.append(entry[1])
    return exts


# ---- 批量 --------------------------------------------------------------

def run_batch(cfg: dict[str, Any], city_ids: list[int], logger=None,
              out_dir: Optional[Path] = None) -> RunSummary:
    client = make_client(cfg, logger, out_dir)
    summary = RunSummary()
    for city_id in city_ids:
        summary.results.append(process_city(client, cfg, city_id, logger, out_dir))
    if logger:
        logger.info(f"请求统计：共 {client.request_count} 次请求，其中重试 {client.retry_count} 次")
    return summary


# ---- 多城市对比 --------------------------------------------------------

def run_compare(cfg: dict[str, Any], city_ids: list[int], logger=None,
                out_dir: Optional[Path] = None) -> RunSummary:
    client = make_client(cfg, logger, out_dir)
    summary = RunSummary(mode="compare")
    cities: list[CityClimate] = []
    for city_id in city_ids:
        result = CityResult(city_id=city_id, profile=str(cfg.get("profile_name", "")))
        try:
            city = fetch_city(client, cfg, city_id, logger)
            result.city = city
            result.city_name = city.city_name
            result.ok = True
            cities.append(city)
        except (CityNotFoundError, NoClimateDataError, ParseError, FetchError) as exc:
            result.error = str(exc)
            if logger:
                logger.error(f"对比：cityId {city_id} 取数失败：{exc}")
        summary.results.append(result)

    if len(cities) < 1:
        if logger:
            logger.error("没有可用于对比的城市")
        return summary

    dirs = paths_for(cfg, out_dir)
    dirs["out_dir"].mkdir(parents=True, exist_ok=True)
    basename = compare_basename(cfg, cities)
    policy = str(cfg["output"].get("overwrite", "overwrite"))
    charts = [dirs["out_dir"] / f"{basename}.{fmt}"
              for fmt in (cfg["output"].get("chart_formats") or [])]
    writable = _apply_overwrite(charts, policy)
    if writable:
        try:
            report: dict[str, Any] = {}
            summary.compare_charts = render_comparison_chart(cities, cfg, writable, logger, report)
            summary.compare_series = [str(item) for item in report.get("series") or []]
            if logger:
                series_note = f"（要素：{'、'.join(summary.compare_series)}）" if summary.compare_series else ""
                logger.info(f"对比图已生成：{len(summary.compare_charts)} 张{series_note}")
        except ChartError as exc:
            if logger:
                logger.error(f"对比图生成失败：{exc}")
    return summary


# ---- 多图（多城市同画布） ----------------------------------------------

def run_multi(cfg: dict[str, Any], city_ids: list[int], logger=None,
              out_dir: Optional[Path] = None) -> RunSummary:
    """多图：把多个城市画进**同一张画布**。

    排列取 ``multi.grid``（``auto`` = 1×N 全横排；``2x2`` = 2 列 2 行），按行优先填充。
    城市数少于格子时，空位不画并给出提示；多于格子时，只画前 ``列×行`` 个并提示哪些被
    略过——两种情况都**只告警、不报错**。某个城市取数失败时同样告警并留空。
    """
    client = make_client(cfg, logger, out_dir)
    summary = RunSummary(mode="multi")
    cities: list[CityClimate] = []
    for city_id in city_ids:
        result = CityResult(city_id=city_id, profile=str(cfg.get("profile_name", "")))
        try:
            city = fetch_city(client, cfg, city_id, logger)
        except (CityNotFoundError, NoClimateDataError, ParseError, FetchError) as exc:
            result.error = str(exc)
            if logger:
                logger.error(f"多图：cityId {city_id} 取数失败：{exc}")
            summary.results.append(result)
            continue
        result.city = city
        result.city_name = city.city_name
        result.ok = True
        cities.append(city)
        summary.results.append(result)

    if not cities:
        if logger:
            logger.error("没有可用于多图的城市")
        return summary

    cols, rows = resolve_grid(cfg.get("multi") or {}, len(cities))
    capacity = cols * rows
    drawn = cities[:capacity]
    summary.grid = f"{cols}x{rows}"
    summary.drawn_ids = [city.city_id for city in drawn]
    summary.skipped_ids = [city.city_id for city in cities[capacity:]]
    if logger:
        if len(cities) < capacity:
            logger.warning(
                f"城市数量 {len(cities)} 与画布 {cols}x{rows}（{capacity} 格）不匹配："
                f"按行优先只画前 {len(cities)} 格，剩余 {capacity - len(cities)} 格留空")
        elif summary.skipped_ids:
            names = "、".join(city.city_name for city in cities[capacity:])
            logger.warning(
                f"城市数量 {len(cities)} 超过画布 {cols}x{rows}（{capacity} 格）："
                f"只画前 {capacity} 个，以下 {len(summary.skipped_ids)} 个未绘制：{names}")

    dirs = paths_for(cfg, out_dir)
    dirs["out_dir"].mkdir(parents=True, exist_ok=True)
    basename = multi_basename(cfg, drawn, summary.grid)
    policy = str(cfg["output"].get("overwrite", "overwrite"))
    charts = [dirs["out_dir"] / f"{basename}.{fmt}"
              for fmt in (cfg["output"].get("chart_formats") or [])]
    writable = _apply_overwrite(charts, policy)
    if writable:
        try:
            report: dict[str, Any] = {}
            summary.multi_charts = render_multi_city_chart(
                drawn, cfg, writable, logger, report, grid=(cols, rows))
            summary.multi_series = [str(item.get("label", ""))
                                    for item in report.get("series") or []]
            if logger:
                series_note = (f"（要素：{'、'.join(summary.multi_series)}）"
                               if summary.multi_series else "")
                logger.info(f"多图已生成：{len(summary.multi_charts)} 张{series_note}")
        except ChartError as exc:
            if logger:
                logger.error(f"多图生成失败：{exc}")
    if logger:
        logger.info(f"请求统计：共 {client.request_count} 次请求，其中重试 {client.retry_count} 次")
    return summary


# ---- 城市名解析 --------------------------------------------------------

def resolve_city_ids(cfg: dict[str, Any], names: list[str], logger=None,
                     out_dir: Optional[Path] = None) -> tuple[list[int], CityIndex]:
    """把城市名列表解析为 cityId 列表。"""
    client = make_client(cfg, logger, out_dir)
    dirs = paths_for(cfg, out_dir)
    index = load_city_index(client, cfg, dirs["cache_dir"] / "city_index.json", logger)
    ids: list[int] = []
    for name in names:
        entry: CityEntry = index.resolve_one(name)
        if logger:
            logger.info(f"「{name}」→ cityId {entry.city_id}（{entry.city_name}，{entry.mem_name}）")
        ids.append(entry.city_id)
    return ids, index


def list_cities(cfg: dict[str, Any], country: Optional[str] = None,
                keyword: Optional[str] = None, logger=None,
                out_dir: Optional[Path] = None, limit: int = 200) -> list[CityEntry]:
    client = make_client(cfg, logger, out_dir)
    dirs = paths_for(cfg, out_dir)
    index = load_city_index(client, cfg, dirs["cache_dir"] / "city_index.json", logger)
    if country:
        return index.list_by_country(country)[:limit]
    if keyword:
        return index.search(keyword, limit=limit)
    return index.entries[:limit]
