#!/usr/bin/env python
"""命令行入口：解析 WMO 城市气候数据，生成表格与气温降水统计图。

常用示例
--------
# 用默认配置生成北京（cityId 237）的表格与统计图
python wmo_climate.py --city-id 237

# 指定配置 + 覆盖输出目录
python wmo_climate.py --city-id 237 --profile presentation --out-dir output/demo

# 一次批量多个城市（其中一个失败不影响其他）
python wmo_climate.py --city-id 237 --city-id 1 --city-id 999999

# 批量也可以用逗号一次写完（等价于重复传参）
python wmo_climate.py --city-id 237,1,156
python wmo_climate.py --city 北京,香港,莫斯科

# 按城市名自动反查 cityId
python wmo_climate.py --city 北京 --city 香港

# 多城市对比（默认对比日均气温）
python wmo_climate.py --compare 237 --compare 1 --compare 156 --profile compare

# 临时改一个绘图参数（点路径覆盖）
python wmo_climate.py --city-id 237 --set series.rainfall.color=#ff7f0e --set figure.title.show=false

# 查看 / 导出配置
python wmo_climate.py --list-profiles
python wmo_climate.py --show-config --profile presentation
python wmo_climate.py --init-profile my_style

# 查城市编号
python wmo_climate.py --search 北京
python wmo_climate.py --list-cities --country 中国

说明
----
批量成图时请求会**排队限速**（默认每秒不超过 1 次请求，见 ``fetch.min_interval``），
以便对数据源保持礼貌；单个城市失败不影响其余城市。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import __version__
from src.city_index import CityLookupError
from src.config_loader import (
    ConfigError,
    coerce_value,
    dump_config,
    list_profiles,
    load_config,
    load_profiles_file,
    validate_config,
    write_template,
)
from src.http_client import FetchError
from src.models import coord_pair
from src.pipeline import (
    list_cities as list_cities_fn,
    resolve_city_ids,
    run_batch,
    run_compare,
)

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


class Logger:
    """极简日志器：统一缩进与级别前缀。"""

    def __init__(self, level: str = "INFO") -> None:
        self.threshold = LEVELS.get(str(level).upper(), 20)

    def _emit(self, tag: str, level: int, msg: str) -> None:
        if level >= self.threshold:
            print(f"[{tag:<7}] {msg}")

    def debug(self, msg: str) -> None:
        self._emit("DEBUG", 10, msg)

    def info(self, msg: str) -> None:
        self._emit("INFO", 20, msg)

    def warning(self, msg: str) -> None:
        self._emit("WARN", 30, msg)

    def error(self, msg: str) -> None:
        self._emit("ERROR", 40, msg)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wmo_climate",
        description="解析世界天气信息服务网（WMO）城市气候数据，生成表格与气温降水统计图。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("常用示例", 1)[-1],
    )

    target = parser.add_argument_group("目标城市")
    target.add_argument("--city-id", action="append", default=[], metavar="ID",
                        help="城市编号；可用逗号一次传多个（如 237,1,156），也可重复传入"
                             "（如 --city-id 237 --city-id 1），两者等价")
    target.add_argument("--city", action="append", default=[], metavar="名称",
                        help="城市名称，自动反查 cityId（如 --city 北京）；可用逗号一次传多个"
                             "（如 --city 北京,香港），也可重复传入")
    target.add_argument("--compare", action="append", default=[], metavar="ID或名称",
                        help="多城市对比：可传 cityId 或城市名，重复传入多个")
    target.add_argument("--compare-metric", metavar="元素",
                        help="对比元素：minTemp/maxTemp/meanTemp/rainfall/raindays")

    conf = parser.add_argument_group("配置")
    conf.add_argument("--profile", metavar="名称", help="配置名称；未指定则使用默认配置")
    conf.add_argument("--profiles-file", metavar="路径",
                      help="自定义 profiles 配置文件（YAML；兼容 JSON）")
    conf.add_argument("--set", action="append", default=[], metavar="键=值",
                      help="点路径覆盖配置，如 --set series.rainfall.color=#ff0000，可重复")
    conf.add_argument("--list-profiles", action="store_true", help="列出全部可用配置")
    conf.add_argument("--show-config", action="store_true", help="打印解析后的最终配置（YAML）")
    conf.add_argument("--init-profile", metavar="名称",
                      help="导出一份全量配置模板到 config/<名称>.yaml")
    conf.add_argument("--temp-unit", choices=["C", "F"], help="温度单位（快捷设置）")
    conf.add_argument("--rain-unit", choices=["mm", "inch"], help="降水单位（快捷设置）")

    out = parser.add_argument_group("输出")
    out.add_argument("--out-dir", metavar="目录", help="输出目录（默认取配置 output.out_dir）")
    out.add_argument("--no-chart", action="store_true", help="只出表格，不绘图")
    out.add_argument("--no-table", action="store_true", help="只绘图，不出表格")
    out.add_argument("--refresh-cache", action="store_true", help="忽略本地缓存，强制重新请求")
    out.add_argument("--refresh-index", action="store_true", help="强制重新拉取城市索引")
    out.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="日志级别")

    query = parser.add_argument_group("城市编号查询")
    query.add_argument("--search", metavar="关键词", help="按城市名搜索 cityId")
    query.add_argument("--list-cities", action="store_true", help="列出城市")
    query.add_argument("--country", metavar="国家/地区", help="配合 --list-cities 按国家筛选")
    query.add_argument("--limit", type=int, default=50, help="列表/搜索结果条数上限")

    parser.add_argument("--version", action="version", version=f"wmo_climate {__version__}")
    return parser


def _split_targets(values: list[str]) -> list[str]:
    """把批量目标参数展开成列表：按逗号分割、逐项去首尾空白、丢弃空项。

    只按**半角逗号**分割：WMO 城市名里用的是全角逗号（如「圣保罗，明尼苏达州」），
    若把全角逗号也当分隔符，这类城市名会被拆坏。
    """
    expanded: list[str] = []
    for raw in values or []:
        for piece in str(raw).split(","):
            text = piece.strip()
            if text:
                expanded.append(text)
    return expanded


def _dedup_targets(city_ids: list[int]) -> list[int]:
    """按首次出现顺序去重（重复城市既浪费时间也重复请求数据源）。"""
    seen: set[int] = set()
    unique: list[int] = []
    for city_id in city_ids:
        if city_id not in seen:
            seen.add(city_id)
            unique.append(city_id)
    return unique


def _parse_overrides(raw_items: list[str], args: argparse.Namespace) -> list[tuple[str, str]]:
    overrides: list[tuple[str, object]] = []
    for item in raw_items:
        if "=" not in item:
            raise ConfigError(f"--set 需要「键=值」格式，收到：{item}")
        key, _, value = item.partition("=")
        overrides.append((key.strip(), coerce_value(value)))
    if args.temp_unit:
        overrides.append(("data.temp_unit", args.temp_unit))
    if args.rain_unit:
        overrides.append(("data.rain_unit", args.rain_unit))
    if args.compare_metric:
        overrides.append(("compare.metric", args.compare_metric))
    if args.no_chart:
        overrides.append(("output.chart_formats", []))
    if args.no_table:
        overrides.append(("output.table_formats", []))
    if args.refresh_cache:
        overrides.append(("fetch.cache.enabled", False))
    if args.refresh_index:
        overrides.append(("fetch.cache.enabled", False))
    return overrides


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    profiles_path = Path(args.profiles_file) if args.profiles_file else None

    # ---- 与配置无关的查询动作 -------------------------------------------
    if args.list_profiles or args.init_profile:
        try:
            doc = load_profiles_file(profiles_path)
        except ConfigError as exc:
            print(f"错误：{exc}")
            return 2
        if args.list_profiles:
            print("可用配置：")
            for name, desc in list_profiles(doc):
                print(f"  {name:<16} {desc}")
            print("\n用法：--profile <名称>；未指定则使用 default。")
        if args.init_profile:
            try:
                cfg = load_config(args.init_profile, profiles_path)
            except ConfigError:
                cfg = load_config(None, profiles_path)
            # 允许传入 my_style.yaml / my_style.json，统一导出为 .yaml
            init_path = Path(args.init_profile)
            init_name = (init_path.stem
                         if init_path.suffix.lower() in (".yaml", ".yml", ".json")
                         else args.init_profile)
            target = Path(__file__).resolve().parent / "config" / f"{init_name}.yaml"
            write_template(target, cfg)
            print(f"已导出全量配置模板：{target}")
        return 0

    # ---- 组装配置 -------------------------------------------------------
    try:
        overrides = _parse_overrides(args.set, args)
        cfg = load_config(args.profile, profiles_path, overrides)
        warnings = validate_config(cfg)
    except ConfigError as exc:
        print(f"配置错误：{exc}")
        return 2

    logger = Logger(args.log_level or cfg["output"].get("log_level", "INFO"))
    for msg in warnings:
        logger.warning(msg)

    if args.show_config:
        print(dump_config(cfg))
        if not (args.city_id or args.city or args.compare or args.search
                or args.list_cities or args.init_profile):
            return 0

    out_dir = Path(args.out_dir).resolve() if args.out_dir else None

    # ---- 城市查询 -------------------------------------------------------
    if args.search or args.list_cities:
        try:
            entries = list_cities_fn(cfg, country=args.country, keyword=args.search,
                                     logger=logger if args.log_level == "DEBUG" else None,
                                     out_dir=out_dir, limit=args.limit)
        except (CityLookupError, FetchError) as exc:
            logger.error(str(exc))
            return 3
        if not entries:
            print("没有匹配的城市。")
            return 0
        print(f"{'cityId':>8}  {'城市':<18} {'国家/地区':<14} 坐标")
        for entry in entries:
            # 坐标列与 {lat} / {lon} 占位符共用一套显示口径（data.coord）
            coords = coord_pair(entry.latitude, entry.longitude, cfg)
            print(f"{entry.city_id:>8}  {entry.city_name:<18} {entry.mem_name:<14} {coords}")
        return 0

    # ---- 解析目标城市 ---------------------------------------------------
    # --city-id / --city 都支持「逗号一次传多个」与「重复传参」两种写法
    city_ids: list[int] = []
    for text in _split_targets(args.city_id):
        if not text.isdigit():
            print(f"错误：--city-id 需要整数编号，收到「{text}」。"
                  f"多个编号用逗号分隔，如 --city-id 237,1,156")
            return 2
        city_ids.append(int(text))
    compare_items: list[int] = []
    try:
        names = _split_targets(args.city)
        if names:
            resolved, _ = resolve_city_ids(cfg, names, logger, out_dir)
            city_ids.extend(resolved)
        for item in args.compare:
            text = str(item).strip()
            if text.isdigit():
                compare_items.append(int(text))
            else:
                resolved, _ = resolve_city_ids(cfg, [text], logger, out_dir)
                compare_items.extend(resolved)
    except (CityLookupError, FetchError) as exc:
        logger.error(str(exc))
        return 3

    unique_ids = _dedup_targets(city_ids)
    if len(unique_ids) != len(city_ids):
        logger.info(f"已去重 {len(city_ids) - len(unique_ids)} 个重复城市")
    city_ids = unique_ids

    if not city_ids and not compare_items:
        build_parser().print_help()
        print("\n提示：至少提供 --city-id / --city / --compare 之一。")
        return 0

    exit_code = 0

    # ---- 多城市对比 -----------------------------------------------------
    if compare_items:
        logger.info(f"开始多城市对比，共 {len(compare_items)} 个城市，"
                    f"配置：{cfg.get('profile_name')}（{cfg.get('profile_description') or '内置默认'}）")
        try:
            summary = run_compare(cfg, compare_items, logger, out_dir)
        except FetchError as exc:
            logger.error(str(exc))
            return 3
        print(summary.describe())
        if summary.compare_charts:
            print("对比图：")
            for path in summary.compare_charts:
                print(f"  {path}")
        if summary.fail_count and not summary.compare_charts:
            exit_code = 1

    # ---- 单城 / 批量 ----------------------------------------------------
    if city_ids:
        logger.info(f"开始处理 {len(city_ids)} 个城市，"
                    f"配置：{cfg.get('profile_name')}（{cfg.get('profile_description') or '内置默认'}）")
        try:
            summary = run_batch(cfg, city_ids, logger, out_dir)
        except FetchError as exc:
            logger.error(str(exc))
            return 3
        print(summary.describe())
        print(f"\n输出目录：{out_dir or (Path(__file__).resolve().parent / cfg['output']['out_dir'])}")
        if summary.fail_count:
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
