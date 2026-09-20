"""回归测试：覆盖解析容错、表格、全部配置的绘图、边界与异常路径。

运行：
    python tests/test_regression.py            # 离线（用 tests/fixtures 中的真实样本）
    python tests/test_regression.py --network  # 额外跑联网用例

设计说明
--------
夹具（``tests/fixtures/*.json``）是真实的 WMO 响应，因此这些用例可以在无网络环境下
稳定复现，覆盖了实测发现的全部边界：PPT/Rainfall/空 raintype、空降水值、
无气候数据城市、负气温等。
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

import numpy as np
import yaml
from matplotlib.text import Text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import table_writer  # noqa: E402
from src.chart import (  # noqa: E402
    render_city_chart,
    render_comparison_chart,
    render_multi_city_chart,
)
from src.city_index import CityIndex, CityEntry, _flatten  # noqa: E402
from src.config_loader import (  # noqa: E402
    ConfigError,
    DEFAULTS,
    apply_marks,
    dump_config,
    load_config,
    load_profiles_file,
    resolve_profile,
    validate_config,
    write_template,
)
from src.models import month_label, normalize_series_key  # noqa: E402
from src.parser import (  # noqa: E402
    NoClimateDataError,
    available_series,
    parse_city_text,
    parse_number,
)

FIXTURES = ROOT / "tests" / "fixtures"
VERIFY_DIR = ROOT / "tests" / "_output" / "render"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(condition), detail if not condition else ""))


def run(name: str, func) -> None:
    try:
        func()
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((name, False, f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"))


def fixture(city_id: int) -> str:
    path = FIXTURES / f"{city_id}_zh.json"
    if not path.exists():
        raise FileNotFoundError(f"缺少夹具 {path}，请先运行 scripts/fetch_fixtures.py")
    return path.read_text(encoding="utf-8")


def city(city_id: int):
    return parse_city_text(fixture(city_id), city_id=city_id, lang="zh")


# ======================= 1. 解析层 =======================

def test_parse_number_tolerance() -> None:
    cases = {
        "1.5": 1.5, "": None, None: None, "NULL": None, "null": None,
        "-9.4": -9.4, " 3 ": 3.0, "N/A": None, "—": None, "abc": None,
        12: 12.0, "1,234": 1234.0,
    }
    for raw, expected in cases.items():
        got = parse_number(raw)
        check(f"parse_number({raw!r}) == {expected}", got == expected, f"实际 {got!r}")


def test_beijing_values() -> None:
    beijing = city(237)
    check("北京城市名解析", beijing.city_name == "北京", beijing.city_name)
    check("北京所属机构", beijing.member.mem_name == "中国", beijing.member.mem_name)
    check("北京月份数 12", len(beijing.months) == 12, str(len(beijing.months)))
    jan = beijing.months[0]
    check("北京 1 月最低气温 -9.4", jan.min_temp == -9.4, str(jan.min_temp))
    check("北京 1 月最高气温 1.6", jan.max_temp == 1.6, str(jan.max_temp))
    check("北京 1 月降水量 3.0", jan.rainfall == 3.0, str(jan.rainfall))
    check("北京 1 月降水日数 2.0", jan.rain_days == 2.0, str(jan.rain_days))
    check("平均气温由 (最高+最低)/2 派生", jan.mean_temp == -3.9, str(jan.mean_temp))
    check("派生态标记", jan.mean_temp_derived is True)
    check("统计时段解析", beijing.period_note() == "1961–1990", beijing.period_note())
    check("存在负气温", min(m.min_temp for m in beijing.months) < 0)


def test_raintype_variants() -> None:
    bj = city(237)
    check("PPT 城市降水名", bj.rain_label(sentence=True) == "平均总降水", bj.rain_label(True))
    hk = city(1)
    check("Rainfall 城市降水名", hk.rain_label(sentence=True) == "平均总降水")
    check("香港为小数降水", hk.months[0].rainfall == 33.2, str(hk.months[0].rainfall))
    empty = city(2184)
    check("空 raintype 不报错", empty.raintype == "", repr(empty.raintype))
    check("空 rainunit 回退毫米", empty.rain_unit_label() == "毫米", empty.rain_unit_label())


def test_missing_values() -> None:
    # 500 塔里：降水量与降水日数 12 个月全缺，只有气温
    tuli = city(500)
    missing_rain = [m for m in tuli.months if m.rainfall is None]
    check("存在降水空值月份", len(missing_rain) == 12, f"{len(missing_rain)} 个月")
    check("全缺元素的年值为 None", tuli.annual("rainfall") is None)
    check("气温仍可用", tuli.annual("meanTemp") is not None)
    mapping = available_series(tuli)
    check("available_series 正确区分有无数据",
          mapping["rainfall"] is False and mapping["maxTemp"] is True, str(mapping))

    # 2034 圣保罗：降水日数全缺，气温与降水量正常
    paul = city(2034)
    check("降水日数全缺被识别", available_series(paul)["raindays"] is False)
    check("降水量仍可用", paul.annual("rainfall") is not None)

    # 1729 卢森格林：气温全缺，只有降水
    lux = city(1729)
    mapping_lux = available_series(lux)
    check("纯降水城市气温全缺", mapping_lux["minTemp"] is False and mapping_lux["rainfall"] is True,
          str(mapping_lux))


def test_no_climate_city() -> None:
    try:
        parse_city_text(fixture(2685), city_id=2685, lang="zh")
    except NoClimateDataError as exc:
        check("无气候数据抛出明确异常", "没有气候" in str(exc) or "气候" in str(exc), str(exc))
    else:
        check("无气候数据抛出明确异常", False, "未抛出异常")


def test_month_label_and_alias() -> None:
    check("月份标签 1月", month_label(1, "1月") == "1月")
    check("月份标签 一月", month_label(1, "一月") == "一月")
    check("月份标签 Jan", month_label(1, "Jan") == "Jan")
    check("月份标签 01", month_label(1, "01") == "01")
    check("元素别名 minTempC", normalize_series_key("minTempC") == "minTemp")
    check("元素别名 大小写", normalize_series_key("RAINFALL") == "rainfall")


def test_coord_formatting() -> None:
    """经纬度：内部存数值，显示文案按 data.coord 现算（方向符号/单位/小数位可配）。"""
    from src import chart as chart_mod
    from src.chart import _context
    from src.models import coord_pair, format_coord
    from src.pipeline import output_basename

    check("默认：字母方向符号跟在数字后",
          format_coord(116.283333, "lon") == "116.28°E"
          and format_coord(39.933333, "lat") == "39.93°N",
          f"{format_coord(116.283333, 'lon')} {format_coord(39.933333, 'lat')}")
    check("南纬/西经换用 S/W",
          format_coord(-33.87, "lat") == "33.87°S" and format_coord(-70.66, "lon") == "70.66°W",
          f"{format_coord(-33.87, 'lat')} {format_coord(-70.66, 'lon')}")
    check("0 度按正方向显示", format_coord(0.0, "lat") == "0.00°N", format_coord(0.0, "lat"))

    signed = {"data": {"coord": {"style": "signed"}}}
    check("纯数字模式：西经/南纬为负",
          format_coord(-33.87, "lat", signed) == "-33.87°"
          and format_coord(116.28, "lon", signed) == "116.28°",
          f"{format_coord(-33.87, 'lat', signed)} {format_coord(116.28, 'lon', signed)}")
    hanzi = {"data": {"coord": {"direction": "hanzi"}}}
    check("汉字方向符号",
          format_coord(116.28, "lon", hanzi) == "116.28°东"
          and format_coord(-33.87, "lat", hanzi) == "33.87°南",
          f"{format_coord(116.28, 'lon', hanzi)} {format_coord(-33.87, 'lat', hanzi)}")
    check("可关闭单位",
          format_coord(116.28, "lon", {"data": {"coord": {"unit": False}}}) == "116.28E")
    check("单位可换成汉字",
          format_coord(116.28, "lon", {"data": {"coord": {"unit_text": "度"}}}) == "116.28度E")
    check("小数位可配",
          format_coord(116.2833, "lon", {"data": {"coord": {"decimals": 4}}}) == "116.2833°E")
    check("配置取值可用中文别名",
          format_coord(-33.87, "lat", {"data": {"coord": {"style": "纯数字",
                                                          "direction": "字母"}}}) == "-33.87°")
    check("缺经纬度返回空串", format_coord(None, "lon") == "")
    check("成对文案：纬度在前、任一缺失为空串",
          coord_pair(39.933333, 116.283333) == "39.93°N, 116.28°E" and coord_pair(None, 116.28) == "",
          coord_pair(39.933333, 116.283333))

    # 落到模板：{lat} / {lon} 进标题（格式跟随配置），文件名模板同样可用（此前会 KeyError）
    bj = city(237)
    cfg = load_config(None, None, [("figure.title.text", "{lat} / {lon}"),
                                   ("output.name_template", "{city}_{lon}"),
                                   ("data.coord.direction", "hanzi")])
    context = _context(bj, cfg)
    check("{lat}/{lon} 上下文按配置给出",
          context["lat"] == "39.93°北" and context["lon"] == "116.28°东",
          f"{context['lat']} / {context['lon']}")
    fig = _render_figure(cfg, bj)
    check("标题模板可渲染 {lat}/{lon}",
          fig.axes[0].get_title() == "39.93°北 / 116.28°东", fig.axes[0].get_title())
    chart_mod.plt.close(fig)
    check("文件名模板支持 {lat}/{lon}（不再 KeyError）",
          output_basename(cfg, bj) == "北京_116.28°东", output_basename(cfg, bj))

    # 非法取值直接报错
    for key, value in (("style", "x"), ("direction", "x"), ("decimals", "a"), ("decimals", -1)):
        bad = load_config(None, None, [(f"data.coord.{key}", value)])
        try:
            validate_config(bad)
        except ConfigError as exc:
            check(f"非法 coord.{key}={value!r} 报错", key in str(exc), str(exc))
        else:
            check(f"非法 coord.{key}={value!r} 报错", False, "未抛出")


# ======================= 2. 配置层 =======================

def test_config_merge_and_set() -> None:
    cfg = load_config(None)
    check("默认配置元素数 5", len(cfg["series"]) == 5, str(len(cfg["series"])))
    check("默认启用 3 个元素", sum(1 for s in cfg["series"].values() if s.get("enabled")) == 3)
    cfg2 = load_config(None, None, [("series.rainfall.color", "#ff0000"),
                                    ("output.chart_formats", ["png", "svg"])])
    check("--set 覆盖元素配色", cfg2["series"]["rainfall"]["color"] == "#ff0000")
    check("--set 覆盖数组", cfg2["output"]["chart_formats"] == ["png", "svg"])
    cfg3 = load_config(None, None, [("series.rainDays.enabled", True)])
    check("--set 元素别名归一", cfg3["series"]["raindays"]["enabled"] is True)


def test_profiles_resolution() -> None:
    doc = load_profiles_file()
    names = list((doc.get("profiles") or {}).keys())
    check("profiles.json 至少 10 套配置", len(names) >= 10, str(len(names)))
    for name in names:
        cfg = resolve_profile(name, doc)
        check(f"配置 {name} 可解析", isinstance(cfg, dict) and "series" in cfg)
    full = load_config("full")
    check("full 启用全部 5 个元素",
          sum(1 for s in full["series"].values() if s.get("enabled")) == 5)
    check("full 使用第三轴", full["series"]["raindays"]["axis"] == "tertiary")
    paper = load_config("paper")
    check("paper 继承样式生效", paper["figure"]["facecolor"] == "#ffffff")
    compare_rain = load_config("compare_rain")
    check("compare_rain 继承 compare", compare_rain["compare"]["metric"] == "rainfall")
    check("compare_rain 图例沿用父配置 ncol",
          compare_rain["compare"]["legend_ncol"] == 5, str(compare_rain["compare"]["legend_ncol"]))


def test_config_errors() -> None:
    try:
        load_config("不存在的配置名")
    except ConfigError as exc:
        check("未知配置名报错", "未找到配置" in str(exc), str(exc))
    else:
        check("未知配置名报错", False, "未抛出")

    cfg = load_config(None)
    for key in list(cfg["series"].keys()):
        cfg["series"][key]["enabled"] = False
    try:
        validate_config(cfg)
    except ConfigError as exc:
        check("全部元素关闭时报错", "没有启用" in str(exc), str(exc))
    else:
        check("全部元素关闭时报错", False, "未抛出")

    cfg2 = load_config(None, None, [("data.temp_unit", "K")])
    try:
        validate_config(cfg2)
    except ConfigError as exc:
        check("非法温度单位报错", "temp_unit" in str(exc), str(exc))
    else:
        check("非法温度单位报错", False, "未抛出")


def test_config_unknown_key_warning() -> None:
    cfg = load_config(None, None, [("figure.不存在的项", 1)])
    warnings = validate_config(cfg)
    check("未知配置项给出告警", any("不存在的项" in w for w in warnings), str(warnings))


def test_custom_config_multi_file() -> None:
    """自定义配置支持多文件（YAML）：按上下顺序加载，后者可 extends 前者；旧 JSON 兼容且优先级更低。"""
    import src.config_loader as cl

    tmp = ROOT / "tests" / "_output" / "custom_test"
    tmp.mkdir(parents=True, exist_ok=True)
    for stale in tmp.iterdir():          # 清理上次运行的残留，保证结果可复现
        if stale.is_file():
            stale.unlink()

    (tmp / "00_base.yaml").write_text(
        "profiles:\n  c_base:\n    figure: {figsize: [1, 1], dpi: 10}\n", encoding="utf-8")
    (tmp / "01_derived.yaml").write_text(
        "profiles:\n  c_sub:\n    extends: c_base\n    figure: {dpi: 99}\n", encoding="utf-8")
    (tmp / "10_dup_a.yaml").write_text(
        "profiles:\n  c_dup:\n    figure: {dpi: 11}\n", encoding="utf-8")
    (tmp / "10_dup_b.yaml").write_text(
        "profiles:\n  c_dup:\n    figure: {dpi: 22}\n", encoding="utf-8")
    # 旧 JSON 配置仍可读取（JSON 是 YAML 子集），但同名 YAML 覆盖之
    (tmp / "20_legacy.json").write_text(
        json.dumps({"profiles": {"c_legacy": {"figure": {"dpi": 33}},
                                 "c_dup": {"figure": {"dpi": 44}}}}),
        encoding="utf-8")

    saved_dir, saved_file = cl.CUSTOM_PROFILES_DIR, cl.CUSTOM_PROFILES_PATH
    cl.CUSTOM_PROFILES_DIR = tmp
    cl.CUSTOM_PROFILES_PATH = ROOT / "config" / "nonexistent_custom.yaml"
    try:
        doc = load_profiles_file()
        profiles = doc.get("profiles") or {}
        check("多文件自定义配置均被加载",
              {"c_base", "c_sub", "c_dup", "c_legacy"} <= set(profiles),
              str(sorted(profiles.keys())))
        base = resolve_profile("c_base", doc)
        check("前序配置 c_base 保留 figsize", base["figure"]["figsize"] == [1, 1],
              str(base["figure"]["figsize"]))
        check("前序配置 c_base 保留 dpi=10", base["figure"]["dpi"] == 10,
              str(base["figure"]["dpi"]))
        sub = resolve_profile("c_sub", doc)
        check("子配置跨文件继承 c_base 的 figsize", sub["figure"]["figsize"] == [1, 1],
              str(sub["figure"]["figsize"]))
        check("子配置自身项 dpi=99 生效", sub["figure"]["dpi"] == 99,
              str(sub["figure"]["dpi"]))
        dup = resolve_profile("c_dup", doc)
        check("同名配置按上下顺序覆盖（后序 dpi=22）", dup["figure"]["dpi"] == 22,
              str(dup["figure"]["dpi"]))
        check("旧 JSON 配置仍可加载（dpi=33）",
              resolve_profile("c_legacy", doc)["figure"]["dpi"] == 33,
              str(resolve_profile("c_legacy", doc)["figure"]["dpi"]))
    finally:
        cl.CUSTOM_PROFILES_DIR = saved_dir
        cl.CUSTOM_PROFILES_PATH = saved_file


def test_config_files_are_yaml() -> None:
    """配置一律为 YAML：内置/自定义配置文件、--show-config 与 --init-profile 输出均为 YAML。"""
    import src.config_loader as cl

    check("内置配置改用 YAML", cl.DEFAULT_PROFILES_PATH.suffix == ".yaml"
          and cl.DEFAULT_PROFILES_PATH.is_file(), str(cl.DEFAULT_PROFILES_PATH))
    check("自定义配置改用 YAML", cl.CUSTOM_PROFILES_PATH.suffix == ".yaml"
          and cl.CUSTOM_PROFILES_PATH.is_file(), str(cl.CUSTOM_PROFILES_PATH))
    check("config 下已无旧 JSON 配置",
          not list((ROOT / "config").glob("*.json"))
          and not list((ROOT / "config" / "custom").glob("*.json")),
          str([str(p) for p in (ROOT / "config").glob("*.json")]))

    check("YAML 配置可解析 default_profile",
          (load_profiles_file().get("default_profile")) == "default")

    tmp = ROOT / "tests" / "_output" / "custom_test"
    tmp.mkdir(parents=True, exist_ok=True)
    template = write_template(tmp / "tpl.yaml", load_config("compact"))
    payload = yaml.safe_load(template.read_text(encoding="utf-8"))
    check("--init-profile 模板为合法 YAML", isinstance(payload, dict)
          and payload.get("profile_name") == "compact", str(template))
    check("模板含全量可配置分组",
          all(key in payload for key in ("data", "fetch", "output", "figure",
                                         "axes_primary", "series", "table", "compare")))
    dumped = yaml.safe_load(dump_config(load_config("compact")))
    check("--show-config 输出为合法 YAML",
          isinstance(dumped, dict) and dumped["figure"]["figsize"] == [9.0, 4.6],
          str(dumped.get("figure", {}).get("figsize")))
    check("导出的模板不含内部键",
          not any(str(k).startswith("_") for k in payload), str(sorted(payload)[:3]))


# ======================= 3. 表格层 =======================

def test_tables(tmp: Path) -> None:
    cfg = load_config(None)
    cfg["output"]["table_formats"] = ["csv", "md", "xlsx", "json"]
    beijing = city(237)
    written = table_writer.write_tables(beijing, cfg, tmp, "237_默认", logger=None)
    check("四种表格格式全部写出", len(written) == 4, str([p.name for p in written]))
    md = (tmp / "237_默认.md").read_text(encoding="utf-8")
    check("Markdown 含标题", "北京 气候统计" in md)
    check("Markdown 含统计时段", "1961–1990" in md)
    check("Markdown 含 12 个月份列", md.count("| 1月 |") >= 1 and "| 12月 |" in md)
    csv_text = (tmp / "237_默认.csv").read_text(encoding="utf-8-sig")
    check("CSV 含降水行", "平均总降水 (毫米)" in csv_text)
    payload = json.loads((tmp / "237_默认.json").read_text(encoding="utf-8"))
    check("JSON 结构完整", payload["cityName"] == "北京" and len(payload["months"]) == 12)
    check("表格默认不含年列", "| 年 |" not in md)


def test_tables_variants(tmp: Path) -> None:
    annual = load_config("annual_table")
    written = table_writer.write_tables(city(237), annual, tmp, "237_年列", logger=None)
    md = (tmp / "237_年列.md").read_text(encoding="utf-8")
    check("年列配置写出多格式", len(written) == 4, str([p.name for p in written]))
    check("转置布局：月份成为行", "| 月份 |" in md and "日均最低气温 (°C)" in md)
    check("转置布局：月份不重复成两列", "| 1月 | 1月 |" not in md, "出现重复月份列")
    check("含年行", any(line.startswith("| 年 |") for line in md.splitlines()),
          "未见年行")

    fahrenheit = load_config("fahrenheit")
    table_writer.write_tables(city(237), fahrenheit, tmp, "237_英制", logger=None)
    md_f = (tmp / "237_英制.md").read_text(encoding="utf-8")
    check("英制单位温度标 °F", "(°F)" in md_f)
    check("英制单位降水标英寸", "(英寸)" in md_f)

    minimal = load_config("minimal")
    table_writer.write_tables(city(237), minimal, tmp, "237_极简", logger=None)
    md_m = (tmp / "237_极简.md").read_text(encoding="utf-8")
    check("极简配置不写标题行", "###" not in md_m)

    annual_cols = load_config(None, None, [("table.include_annual", True)])
    table_writer.write_tables(city(237), annual_cols, tmp, "237_年列不转置", logger=None)
    md_ac = (tmp / "237_年列不转置.md").read_text(encoding="utf-8")
    check("不转置时追加年列", "| 年 |" in md_ac, md_ac.splitlines()[4] if len(md_ac.splitlines()) > 4 else "")

    # 空值：塔里（降水全缺）、圣保罗（降水日数全缺）
    tuli = city(500)
    written = table_writer.write_tables(tuli, load_config(None), tmp, "500_降水全缺", logger=None)
    md_e = (tmp / "500_降水全缺.md").read_text(encoding="utf-8")
    check("空值用占位符呈现", "—" in md_e, "未见占位符")
    check("含空值城市表格仍写出", len(written) == 3, str(len(written)))
    xlsx = tmp / "500_降水全缺.xlsx"
    check("含空值城市 XLSX 写出成功", xlsx.exists() and xlsx.stat().st_size > 2000,
          f"{(xlsx.stat().st_size if xlsx.exists() else 0)} bytes")

    # 纯降水城市（气温全缺）
    table_writer.write_tables(city(1729), load_config(None), tmp, "1729_气温全缺", logger=None)
    md_r = (tmp / "1729_气温全缺.md").read_text(encoding="utf-8")
    check("纯降水城市表格写出", "平均总降水" in md_r)


# ======================= 4. 绘图层 =======================

def _render(cfg, city_obj, out_dir: Path, tag: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{tag}.png"
    render_city_chart(city_obj, cfg, [path], logger=None)
    return path


def test_all_profiles_render() -> None:
    doc = load_profiles_file()
    names = ["default", *sorted((doc.get("profiles") or {}).keys())]
    beijing = city(237)
    target = VERIFY_DIR / "配置核对"
    for name in names:
        if name.startswith("compare"):
            continue
        cfg = load_config(name)
        path = _render(cfg, beijing, target, f"北京_{name}")
        check(f"配置 {name} 渲染成功", path.exists() and path.stat().st_size > 5000,
              f"{(path.stat().st_size if path.exists() else 0)} bytes")


def test_edge_city_render() -> None:
    target = VERIFY_DIR / "边界用例"
    cfg = load_config(None)
    hk = city(1)
    path = _render(cfg, hk, target, "香港_默认")
    check("香港（小数降水）渲染成功", path.exists() and path.stat().st_size > 5000)

    spain = city(2184)
    path = _render(cfg, spain, target, "希洪_空单位")
    check("空 raintype 城市渲染成功", path.exists() and path.stat().st_size > 5000)

    siberia = city(1007)
    path = _render(cfg, siberia, target, "新西伯利亚_含0降水")
    check("含 0 值降水渲染成功", path.exists() and path.stat().st_size > 5000)

    tuli = city(500)
    path = _render(cfg, tuli, target, "塔里_降水全缺自动降级")
    check("降水全缺城市自动降级为纯气温图", path.exists() and path.stat().st_size > 5000)

    path = _render(load_config("full"), tuli, target, "塔里_full配置_降水全缺")
    check("降水全缺 + full 配置不崩溃", path.exists() and path.stat().st_size > 5000)

    lux = city(1729)
    path = _render(cfg, lux, target, "卢森格林_气温全缺")
    check("气温全缺城市渲染成功", path.exists() and path.stat().st_size > 5000)

    full = load_config("full")
    path = _render(full, hk, target, "香港_全元素三轴")
    check("三轴（含降水日数）渲染成功", path.exists() and path.stat().st_size > 5000)


def test_series_toggle() -> None:
    cfg = load_config(None, None, [("series.minTemp.enabled", False),
                                   ("series.maxTemp.enabled", False),
                                   ("series.rainfall.enabled", False)])
    target = VERIFY_DIR / "边界用例"
    try:
        _render(cfg, city(237), target, "应为空")
    except Exception as exc:  # noqa: BLE001
        check("关闭全部元素时给出明确错误", "没有任何可绘制" in str(exc), str(exc))
    else:
        check("关闭全部元素时给出明确错误", False, "未报错")


def _render_figure(cfg, city_obj):
    """渲染但**不落盘**，返回 Figure 供层级断言使用。"""
    from src import chart as chart_mod

    captured: dict = {}
    original = chart_mod._save
    chart_mod._save = lambda fig, out_paths, cfg: (captured.__setitem__("fig", fig), out_paths)[1]
    try:
        render_city_chart(city_obj, cfg, [], logger=None)
    finally:
        chart_mod._save = original
    return captured["fig"]


def test_background_bands_layering() -> None:
    """背景色带必须落在降水柱之下（季节/隔月两种模式）。

    两种成立方式：色带画在**更低的坐标轴**（轴 zorder 更小 = 先绘制 = 在下），
    或与柱子同轴时**艺术家 zorder 更低**。
    """
    from src import chart as chart_mod

    beijing = city(237)
    for profile, expected in (("seasonal", 12), ("alt_bands", 6)):
        fig = _render_figure(load_config(profile), beijing)
        band_axes = [a for a in fig.axes if any(p.get_zorder() == 0 for p in a.patches)]
        bar_axes = [a for a in fig.axes if any(p.get_zorder() >= 2 for p in a.patches)]
        band_patches = [p for a in band_axes for p in a.patches if p.get_zorder() == 0]

        check(f"{profile}：色带数量 {expected}", len(band_patches) == expected, str(len(band_patches)))
        check(f"{profile}：降水柱存在且独占一轴", len(bar_axes) == 1, str(len(bar_axes)))
        check(f"{profile}：色带只占一个坐标轴", len(band_axes) == 1, str(len(band_axes)))
        if band_axes and bar_axes:
            band_axis, bar_axis = band_axes[0], bar_axes[0]
            check(f"{profile}：色带轴不高于柱状图轴",
                  band_axis.get_zorder() <= bar_axis.get_zorder(),
                  f"色带轴 zorder={band_axis.get_zorder()} 柱轴 zorder={bar_axis.get_zorder()}")
            if band_axis is bar_axis:
                top_band = max(p.get_zorder() for p in band_axis.patches if p.get_zorder() == 0)
                bar_patches = [p for p in bar_axis.patches if p.get_zorder() >= 2]
                check(f"{profile}：同轴时柱子压在色带之上",
                      min(b.get_zorder() for b in bar_patches) > top_band,
                      "同轴层级异常")
        if profile == "seasonal":
            slots = sorted({round(p.get_xy()[0] + 0.5) for p in band_patches})
            check("季节色带覆盖 1-12 月各槽位", slots == list(range(12)), str(slots))
        chart_mod.plt.close(fig)


def _band_colors(cfg, city_obj) -> dict:
    """返回 {月份槽位: 色带色值}，用于断言季节色带对位（同槽多层时取最后绘制的一层）。"""
    from src import chart as chart_mod

    fig = _render_figure(cfg, city_obj)
    slots: dict = {}
    for axis in fig.axes:
        for patch in axis.patches:
            if patch.get_zorder() != 0:
                continue
            slot = int(round(patch.get_xy()[0] + 0.5))
            rgb = tuple(int(round(c * 255)) for c in patch.get_facecolor()[:3])
            slots[slot] = "#%02x%02x%02x" % rgb
    chart_mod.plt.close(fig)
    return slots


def city_any(city_id: int):
    """夹具优先，其次 samples（个别城市只在 samples 里有）。"""
    for path in (FIXTURES / f"{city_id}_zh.json", FIXTURES / "samples" / f"{city_id}.json"):
        if path.exists():
            return parse_city_text(path.read_text(encoding="utf-8"),
                                   city_id=city_id, lang="zh")
    raise FileNotFoundError(city_id)


def _extreme_overlaps(fig) -> list:
    """极值标注的问题清单：压曲线 / 压平均降水线 / 两标注互压 / 横向越界。

    判定用「纯文字盒 + 2pt 间隙」对折线点云做命中测试，与绘制实现同一套几何。
    """
    from src import chart as chart_mod

    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    ax = fig.axes[0]
    annos = [t for a in fig.axes for t in a.texts
             if "最高" in t.get_text() or "最低" in t.get_text()]
    if len(annos) < 2:
        return []
    annos.sort(key=lambda t: 0 if "最高" in t.get_text() else 1)
    raw = [Text.get_window_extent(t, renderer) for t in annos]
    boxes = [chart_mod._grow_box(b, 4.0) for b in raw]
    clouds = []
    for axis in fig.axes:
        for ln in axis.lines:
            if str(ln.get_color()) == "#c9d3dd" and ln.get_label() != "平均降水":
                continue                                  # 网格线不算障碍
            pts = np.asarray(ln.get_xydata(), dtype=float)
            if pts.size:
                clouds.append(chart_mod._densify(ln.get_transform().transform(pts)))
    notes = [tag for tag, box in zip(("最高", "最低"), boxes)
             if any(chart_mod._cloud_hits_box(pts, box) for pts in clouds)]
    if (boxes[0].x0 < boxes[1].x1 and boxes[1].x0 < boxes[0].x1
            and boxes[0].y0 < boxes[1].y1 and boxes[1].y0 < boxes[0].y1):
        notes.append("两标注互压")
    ab = ax.get_window_extent()
    if any(b.x0 < ab.x0 or b.x1 > ab.x1 for b in raw):     # 越界看裸文字盒
        notes.append("横向越界")
    if any(b.y0 < ab.y0 or b.y1 > ab.y1 for b in raw):
        notes.append("纵向越界")
    return notes


def _extreme_case(city_id: int, **over):
    """渲染并返回 (问题清单, [最高偏移, 最低偏移])。"""
    from src import chart as chart_mod

    sets = [("figure.mean_rain_line.show", True),
            ("figure.annotation.show_extremes", True)] + list(over.items())
    cfg = load_config("seasonal", None, sets)
    fig = _render_figure(cfg, city_any(city_id))
    notes = _extreme_overlaps(fig)
    annos = sorted([t for a in fig.axes for t in a.texts
                    if "最高" in t.get_text() or "最低" in t.get_text()],
                   key=lambda t: 0 if "最高" in t.get_text() else 1)
    offs = [[round(float(v), 1) for v in t.xyann] for t in annos]
    chart_mod.plt.close(fig)
    return notes, offs


def test_extremes_label_avoidance() -> None:
    """最高/最低气温标注自动避让：不压曲线/平均降水线，无事则原地不动。"""
    # 1) 关闭避让时，这几个城市确实压线（同时也是对本测试判定逻辑的自检）
    baseline = {cid: _extreme_case(cid, **{"figure.annotation.avoid_overlap": False})[0]
                for cid in (237, 1007, 1, 2034)}
    check("关闭避让时北京/新西伯利亚/香港/圣保罗存在压线",
          all(baseline[cid] for cid in (237, 1007, 1, 2034)), str(baseline))

    # 2) 开启避让（默认）后不再压线
    for cid in (237, 1007, 1, 2034, 2150):
        notes, offs = _extreme_case(cid)
        check(f"cityId {cid} 极值标注避让后无压线", notes == [], f"{notes} 偏移={offs}")
        check(f"cityId {cid} 仍保持高低分居两侧",
              offs[0][1] > 0 > offs[1][1], str(offs))

    # 3) 本来就不撞的城市保持原偏移（不乱动）
    for cid in (156, 2184):
        notes, offs = _extreme_case(cid)
        check(f"cityId {cid} 无冲突时不移动", offs == [[0.0, 16.0], [0.0, -22.0]],
              f"{offs} {notes}")

    # 4) 关闭避让后完全回到固定偏移
    notes, offs = _extreme_case(237, **{"figure.annotation.avoid_overlap": False})
    check("avoid_overlap=false 恢复固定偏移", offs == [[0.0, 16.0], [0.0, -22.0]],
          str(offs))
    check("avoid_overlap=false 时确实压线（说明它真的没被移动）", notes != [], str(notes))

    # 5) allow_flip=false 时仍留在偏好侧
    notes, offs = _extreme_case(1007, **{"figure.annotation.allow_flip": False})
    check("allow_flip=false 时仍分居两侧", offs[0][1] > 0 > offs[1][1], str(offs))

    # 6) max_distance 限制搜索半径
    _notes, offs = _extreme_case(1007, **{"figure.annotation.max_distance": 20.0})
    check("max_distance 限制外推距离",
          all(abs(v) <= 20.0 for o in offs for v in o), str(offs))

    # 7) gap 放大后仍不压线
    notes, _offs = _extreme_case(237, **{"figure.annotation.gap": 10.0})
    check("gap 放大后仍无压线", notes == [], str(notes))

    # 8) 箭头锚点仍是原数据点（避让只动文字，不动指向）
    from src import chart as chart_mod
    cfg = load_config("seasonal", None, [("figure.annotation.show_extremes", True)])
    bei = city(237)
    fig = _render_figure(cfg, bei)
    annos = [t for a in fig.axes for t in a.texts
             if "最高" in t.get_text() or "最低" in t.get_text()]
    values = bei.values("meanTemp")
    hi = max(range(len(values)), key=lambda i: values[i])
    lo = min(range(len(values)), key=lambda i: values[i])
    hi_anno = [a for a in annos if "最高" in a.get_text()][0]
    lo_anno = [a for a in annos if "最低" in a.get_text()][0]
    check("最高标注锚点=最高月数据点",
          abs(hi_anno.xy[0] - hi) < 1e-6 and abs(hi_anno.xy[1] - values[hi]) < 1e-6,
          str(hi_anno.xy))
    check("最低标注锚点=最低月数据点",
          abs(lo_anno.xy[0] - lo) < 1e-6 and abs(lo_anno.xy[1] - values[lo]) < 1e-6,
          str(lo_anno.xy))
    chart_mod.plt.close(fig)


def test_extremes_label_avoidance_cramped_axis() -> None:
    """小画布 + 固定量程下，最低月标注必须留在绘图区内（乌兰巴托 + 简图系配置）。

    该组合把标注空间压得很窄：最低月气温 -20.8 落在固定量程 -30~30 的下沿附近，
    平均降水线（22.5 mm）又正好横在文字下方，再往下挪一档就整体掉出坐标区底边
    （实测会压住月份刻度）。旧算法只能二选一（压线 / 出界），现在会额外派生
    "最小幅度推回区内"的候选，保证有解时不出界。
    """
    from src import chart as chart_mod

    for profile in ("横1", "横150", "横300", "横900", "简2"):
        fig = _render_figure(load_config(profile), city_any(229))
        notes = _extreme_overlaps(fig)
        annos = [t for a in fig.axes for t in a.texts
                 if "最高" in t.get_text() or "最低" in t.get_text()]
        annos.sort(key=lambda t: 0 if "最高" in t.get_text() else 1)
        offs = [[round(float(v), 1) for v in t.xyann] for t in annos]
        check(f"{profile}：乌兰巴托极值标注不越界不压线", notes == [], f"{notes} 偏移={offs}")
        if profile == "横300":
            check("横300：最低标注仍留在数据点下方（未翻边）",
                  len(offs) == 2 and offs[1][1] < 0, str(offs))
        chart_mod.plt.close(fig)

    # 关闭避让时确实越界（证明这个用例考的就是避让逻辑本身）
    cfg = load_config("横300", None, [("figure.annotation.avoid_overlap", False)])
    fig = _render_figure(cfg, city_any(229))
    check("横300：关闭避让时最低标注确实越界", _extreme_overlaps(fig) != [],
          str(_extreme_overlaps(fig)))
    chart_mod.plt.close(fig)


def _horizontal_lines(fig) -> list:
    """所有水平常量线 (所在坐标轴, Line2D)；axhline 生成的两点同值线。"""
    found = []
    for axis in fig.axes:
        for line in axis.lines:
            ys = list(line.get_ydata())
            if len(ys) == 2 and ys[0] == ys[1]:
                found.append((axis, line))
    return found


def _legend_labels(fig) -> list:
    """图例文案（含轴级图例与画布级图例）。"""
    legends = list(fig.legends) + [a.get_legend() for a in fig.axes if a.get_legend() is not None]
    return [t.get_text() for lg in legends for t in lg.get_texts()]


def test_mean_rain_line() -> None:
    """平均降水线：默认不画；显式开启后按 12 个月降水均值画一条水平线。"""
    from src import chart as chart_mod

    beijing = city(237)
    expect_mm = sum(v for v in beijing.values("rainfall") if v is not None) / 12.0

    # 1) 默认关闭：不产生该元素，也不进图例
    check("默认配置 mean_rain_line.show 为 false",
          load_config(None)["figure"]["mean_rain_line"]["show"] is False)
    fig = _render_figure(load_config(None), beijing)
    check("默认配置不画平均降水线",
          not any(ln.get_color() == "#46cbd4" for _a, ln in _horizontal_lines(fig)),
          str([ln.get_color() for _a, ln in _horizontal_lines(fig)]))
    check("默认配置图例无平均降水",
          not any("平均降水" in t for t in _legend_labels(fig)), str(_legend_labels(fig)))
    chart_mod.plt.close(fig)

    # 2) 显式开启
    cfg = load_config(None, None, [("figure.mean_rain_line.show", True)])
    fig = _render_figure(cfg, beijing)
    hits = [(a, ln) for a, ln in _horizontal_lines(fig) if ln.get_label() == "平均降水"]
    check("开启后画出平均降水线", len(hits) == 1, str(len(hits)))
    if hits:
        axis, line = hits[0]
        check("平均降水线取值 = 12 个月均值",
              abs(float(line.get_ydata()[0]) - expect_mm) < 1e-6,
              f"{line.get_ydata()[0]} != {expect_mm}")
        check("平均降水线画在降水柱所在轴", len(axis.patches) > 0,
              f"该轴 patch 数 {len(axis.patches)}")
        check("颜色跟随 rainfall 元素", line.get_color() == "#46cbd4", line.get_color())
        check("线型为虚线", line.get_linestyle() == "--", line.get_linestyle())
    check("平均降水线进入图例",
          any("平均降水" in t for t in _legend_labels(fig)), str(_legend_labels(fig)))
    texts = [t.get_text() for a in fig.axes for t in a.texts]
    check("线上标注均值与单位", any(f"{expect_mm:.1f}" in t and "毫米" in t for t in texts),
          str(texts))
    annot = [t for a in fig.axes for t in a.texts if "平均降水" in t.get_text()]
    check("标注颜色默认沿用线色",
          bool(annot) and annot[0].get_color() == "#46cbd4",
          str(annot[0].get_color() if annot else None))
    check("标注默认贴右上（ha=right / va=bottom / 偏移 0,4）",
          bool(annot) and annot[0].get_ha() == "right" and annot[0].get_va() == "bottom"
          and list(annot[0].xyann) == [0.0, 4.0],
          str((annot[0].get_ha(), annot[0].get_va(), list(annot[0].xyann)) if annot else None))
    chart_mod.plt.close(fig)

    # 2c) 标注位置可调：贴左端 + 线下方
    cfg = load_config(None, None, [("figure.mean_rain_line.show", True),
                                   ("figure.mean_rain_line.annotate_position", "left"),
                                   ("figure.mean_rain_line.annotate_side", "below"),
                                   ("figure.mean_rain_line.annotate_offset", [2.0, 6.0])])
    fig = _render_figure(cfg, beijing)
    fig.canvas.draw()
    annot = [t for a in fig.axes for t in a.texts if "平均降水" in t.get_text()][0]
    check("annotate_position=left 时左对齐且 dy 取负",
          annot.get_ha() == "left" and annot.get_va() == "top"
          and list(annot.xyann) == [2.0, -6.0],
          str((annot.get_ha(), annot.get_va(), list(annot.xyann))))
    axis_bbox = annot.axes.get_window_extent()
    text_bbox = annot.get_window_extent(fig.canvas.get_renderer())
    expect_x0 = axis_bbox.x0 + 2.0 * fig.dpi / 72       # 左边缘 + dx(2pt)
    check("标注左边缘 = 绘图区左边缘 + dx",
          abs(text_bbox.x0 - expect_x0) < 1.0,
          f"文本 x0={text_bbox.x0:.1f} 期望 {expect_x0:.1f}")
    line_y = annot.axes.transData.transform(
        (0, float([ln for _a, ln in _horizontal_lines(fig)
                   if ln.get_label() == "平均降水"][0].get_ydata()[0])))[1]
    check("annotate_side=below 时文字整体落在线下方",
          text_bbox.y1 <= line_y, f"文本 y1={text_bbox.y1:.1f} 线 y={line_y:.1f}")
    chart_mod.plt.close(fig)

    # 2d) annotate_position=ticks：横向贴副轴刻度标签列，纵向仍沿用 side+offset
    cfg = load_config(None, None, [("figure.mean_rain_line.show", True),
                                   ("figure.mean_rain_line.annotate_position", "ticks")])
    fig = _render_figure(cfg, beijing)
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    ax2 = [a for a in fig.axes if any(p.get_zorder() >= 2 for p in a.patches)][0]
    tick_boxes = [t.get_window_extent(renderer)
                  for t in ax2.yaxis.get_majorticklabels() if t.get_text().strip()]
    anno = [t for a in fig.axes for t in a.texts if "平均降水" in t.get_text()][0]
    text_box = anno.get_window_extent(renderer)
    check("ticks 模式与刻度标签左边缘对齐",
          abs(text_box.x0 - min(b.x0 for b in tick_boxes)) < 1.0,
          f"标注 x0={text_box.x0:.1f} 刻度 x0={min(b.x0 for b in tick_boxes):.1f}")
    check("ticks 模式把标注推到绘图区之外",
          text_box.x0 > ax2.get_window_extent().x1,
          f"标注 x0={text_box.x0:.1f} 轴右边缘={ax2.get_window_extent().x1:.1f}")
    check("ticks 模式横向偏移与 ha/va 规则不变",
          anno.get_ha() == "left" and anno.get_va() == "bottom"
          and float(anno.xyann[0]) == 7.5,
          str((anno.get_ha(), anno.get_va(), list(anno.xyann))))
    check("ticks 模式与刻度标签纵向不再重叠（自动避让）",
          not any(b.y1 > text_box.y0 and b.y0 < text_box.y1
                  and b.x1 > text_box.x0 and b.x0 < text_box.x1 for b in tick_boxes),
          f"标注 y {text_box.y0:.1f}..{text_box.y1:.1f} 刻度 "
          f"{[(round(b.y0, 1), round(b.y1, 1)) for b in tick_boxes]}")
    check("ticks 模式确实触发了避让（纵向不再是基准 4pt）",
          abs(float(anno.xyann[1]) - 4.0) > 0.5, str(list(anno.xyann)))
    chart_mod.plt.close(fig)

    # 2e) ticks 模式在刻度位于左侧时自动镜像
    cfg = load_config(None, None, [("figure.mean_rain_line.show", True),
                                   ("figure.mean_rain_line.annotate_position", "ticks"),
                                   ("axes_secondary.side", "left")])
    fig = _render_figure(cfg, beijing)
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    ax2 = [a for a in fig.axes if any(p.get_zorder() >= 2 for p in a.patches)][0]
    tick_boxes = [t.get_window_extent(renderer)
                  for t in ax2.yaxis.get_majorticklabels() if t.get_text().strip()]
    anno = [t for a in fig.axes for t in a.texts if "平均降水" in t.get_text()][0]
    text_box = anno.get_window_extent(renderer)
    check("ticks 模式在刻度居左时右对齐到刻度列右边缘",
          anno.get_ha() == "right"
          and abs(text_box.x1 - max(b.x1 for b in tick_boxes)) < 1.0,
          f"ha={anno.get_ha()} 标注 x1={text_box.x1:.1f} "
          f"刻度 x1={max(b.x1 for b in tick_boxes):.1f}")
    check("ticks 模式在刻度居左时刻度列位于绘图区左侧",
          text_box.x1 < ax2.get_window_extent().x0,
          f"标注 x1={text_box.x1:.1f} 轴左边缘={ax2.get_window_extent().x0:.1f}")
    check("ticks 模式刻度居左时同样避让刻度标签",
          not any(b.y1 > text_box.y0 and b.y0 < text_box.y1
                  and b.x1 > text_box.x0 and b.x0 < text_box.x1 for b in tick_boxes),
          f"标注 y {text_box.y0:.1f}..{text_box.y1:.1f}")
    chart_mod.plt.close(fig)

    # 2g) annotate_side=center：文字垂直中心落在均值线上（绘图区内，无刻度标签需要避让）
    cfg = load_config(None, None, [("figure.mean_rain_line.show", True),
                                   ("figure.mean_rain_line.annotate_position", "right"),
                                   ("figure.mean_rain_line.annotate_side", "center")])
    fig = _render_figure(cfg, beijing)
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    ax2 = [a for a in fig.axes if any(p.get_zorder() >= 2 for p in a.patches)][0]
    anno = [t for a in fig.axes for t in a.texts if "平均降水" in t.get_text()][0]
    text_box = anno.get_window_extent(renderer)
    line_y = ax2.transData.transform((0, expect_mm))[1]
    check("annotate_side=center 时文字中心落在均值线上",
          anno.get_va() == "center"
          and abs((text_box.y0 + text_box.y1) / 2 - line_y) < 1.0,
          f"va={anno.get_va()} 文字中心={(text_box.y0 + text_box.y1) / 2:.1f} 线 y={line_y:.1f}")
    check("annotate_side=center 时 offset 纵向分量不生效",
          list(anno.xyann) == [0.0, 0.0], str(list(anno.xyann)))
    check("绘图区内且不相撞时不做任何避让",
          abs((text_box.y0 + text_box.y1) / 2 - line_y) < 1.0, "")

    # 2h) center 与 ticks 叠加：居中让位于"不压刻度标签"，横向仍贴列
    cfg = load_config(None, None, [("figure.mean_rain_line.show", True),
                                   ("figure.mean_rain_line.annotate_position", "ticks"),
                                   ("figure.mean_rain_line.annotate_side", "center")])
    fig = _render_figure(cfg, beijing)
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    ax2 = [a for a in fig.axes if any(p.get_zorder() >= 2 for p in a.patches)][0]
    tick_boxes = [t.get_window_extent(renderer)
                  for t in ax2.yaxis.get_majorticklabels() if t.get_text().strip()]
    anno = [t for a in fig.axes for t in a.texts if "平均降水" in t.get_text()][0]
    text_box = anno.get_window_extent(renderer)
    line_y = ax2.transData.transform((0, expect_mm))[1]
    check("center + ticks：横向仍贴刻度列",
          abs(text_box.x0 - min(b.x0 for b in tick_boxes)) < 1.0,
          f"标注 x0={text_box.x0:.1f} 刻度 x0={min(b.x0 for b in tick_boxes):.1f}")
    check("center + ticks：避让优先，纵向不再与刻度标签重叠",
          not any(b.y1 > text_box.y0 and b.y0 < text_box.y1
                  and b.x1 > text_box.x0 and b.x0 < text_box.x1 for b in tick_boxes),
          f"标注 y {text_box.y0:.1f}..{text_box.y1:.1f}")
    check("center + ticks：让位后中心不再与均值线重合（避让代价）",
          abs((text_box.y0 + text_box.y1) / 2 - line_y) > 1.0,
          f"中心={(text_box.y0 + text_box.y1) / 2:.1f} 线 y={line_y:.1f}")
    chart_mod.plt.close(fig)

    # 2f) 换画布尺寸仍严格对齐（偏移以点为单位，与布局无关）
    cfg = load_config(None, None, [("figure.mean_rain_line.show", True),
                                   ("figure.mean_rain_line.annotate_position", "ticks"),
                                   ("figure.figsize", [16.0, 8.0])])
    fig = _render_figure(cfg, beijing)
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    ax2 = [a for a in fig.axes if any(p.get_zorder() >= 2 for p in a.patches)][0]
    tick_boxes = [t.get_window_extent(renderer)
                  for t in ax2.yaxis.get_majorticklabels() if t.get_text().strip()]
    anno = [t for a in fig.axes for t in a.texts if "平均降水" in t.get_text()][0]
    text_box = anno.get_window_extent(renderer)
    check("ticks 模式换 16x8 画布仍与刻度列对齐",
          abs(text_box.x0 - min(b.x0 for b in tick_boxes)) < 1.0,
          f"标注 x0={text_box.x0:.1f} 刻度 x0={min(b.x0 for b in tick_boxes):.1f}")
    chart_mod.plt.close(fig)

    # 2b) 标注颜色可独立于线色
    cfg = load_config(None, None, [("figure.mean_rain_line.show", True),
                                   ("figure.mean_rain_line.annotate_color", "#a32d2d")])
    fig = _render_figure(cfg, beijing)
    annot = [t for a in fig.axes for t in a.texts if "平均降水" in t.get_text()]
    check("annotate_color 生效",
          bool(annot) and annot[0].get_color() == "#a32d2d",
          str(annot[0].get_color() if annot else None))
    hits = [ln for _a, ln in _horizontal_lines(fig) if ln.get_label() == "平均降水"]
    check("改标注颜色不影响线色",
          bool(hits) and hits[0].get_color() == "#46cbd4",
          str(hits[0].get_color() if hits else None))
    chart_mod.plt.close(fig)

    # 3) label 置空：仍画线，但不进图例
    cfg = load_config(None, None, [("figure.mean_rain_line.show", True),
                                   ("figure.mean_rain_line.label", "")])
    fig = _render_figure(cfg, beijing)
    check("label 为空时仍画线",
          any(abs(float(ln.get_ydata()[0]) - expect_mm) < 1e-6
              for _a, ln in _horizontal_lines(fig)), "")
    check("label 为空时不进图例",
          not any("平均降水" in t for t in _legend_labels(fig)), str(_legend_labels(fig)))
    chart_mod.plt.close(fig)

    # 4) 降水全缺的城市：不画（也不报错）
    tuli = city(500)
    fig = _render_figure(load_config(None, None, [("figure.mean_rain_line.show", True)]), tuli)
    check("降水全缺时不画平均降水线",
          not any(ln.get_label() == "平均降水" for _a, ln in _horizontal_lines(fig)))
    chart_mod.plt.close(fig)

    # 5) 英制单位：均值随之换算，标注文案用英寸
    cfg_in = load_config("fahrenheit", None, [("figure.mean_rain_line.show", True)])
    expect_in = expect_mm / 25.4
    fig = _render_figure(cfg_in, beijing)
    hits = [ln for _a, ln in _horizontal_lines(fig) if ln.get_label() == "平均降水"]
    check("英制配置下均值为英寸",
          bool(hits) and abs(float(hits[0].get_ydata()[0]) - expect_in) < 1e-6,
          str(hits[0].get_ydata()[0] if hits else None))
    check("英制配置下标注写英寸",
          any("英寸" in t.get_text() for a in fig.axes for t in a.texts))
    chart_mod.plt.close(fig)

    # 6) axis=auto 跟随 rainfall 元素所在轴（rain_only 把降水放在主轴）
    cfg_rain = load_config("rain_only", None, [("figure.mean_rain_line.show", True)])
    fig = _render_figure(cfg_rain, beijing)
    hits = [(a, ln) for a, ln in _horizontal_lines(fig) if ln.get_label() == "平均降水"]
    check("rain_only 下平均降水线落在主轴",
          bool(hits) and hits[0][0] is fig.axes[0], f"axes={[fig.axes.index(a) for a, _ in hits]}")
    check("rain_only 下颜色跟随该配置的 rainfall 颜色",
          bool(hits) and hits[0][1].get_color() == "#4a9fd8",
          str(hits[0][1].get_color() if hits else None))
    chart_mod.plt.close(fig)

    # 7) 非法 axis 被校验拦下
    bad = load_config(None)
    bad["figure"]["mean_rain_line"]["axis"] = "upside"
    try:
        validate_config(bad)
    except ConfigError as exc:
        check("非法 mean_rain_line.axis 报错", "mean_rain_line" in str(exc), str(exc))
    else:
        check("非法 mean_rain_line.axis 报错", False, "未抛出")

    bad = load_config(None)
    bad["figure"]["mean_rain_line"]["annotate_position"] = "middle"
    try:
        validate_config(bad)
    except ConfigError as exc:
        check("非法 annotate_position 报错", "annotate_position" in str(exc), str(exc))
    else:
        check("非法 annotate_position 报错", False, "未抛出")

    bad = load_config(None)
    bad["figure"]["mean_rain_line"]["annotate_side"] = "left"
    try:
        validate_config(bad)
    except ConfigError as exc:
        check("非法 annotate_side 报错", "annotate_side" in str(exc), str(exc))
    else:
        check("非法 annotate_side 报错", False, "未抛出")


def test_seasonal_bands_hemisphere() -> None:
    """季节色带按半球自动反季：南半球城市冬夏、春秋互换。"""
    winter, spring, summer, autumn = "#4a6fa5", "#6aa84f", "#e69138", "#a64d79"
    north, south = city(237), city(1729)   # 北京 39.93N / 卢森格林 -36.06S
    check("夹具纬度符号正确", north.latitude > 0 and south.latitude < 0,
          f"{north.latitude} / {south.latitude}")

    cfg = load_config("seasonal")
    north_colors = _band_colors(cfg, north)
    check("北半球：12/1/2 月为冬色",
          all(north_colors.get(i) == winter for i in (11, 0, 1)), str(north_colors))
    check("北半球：6/7/8 月为夏色",
          all(north_colors.get(i) == summer for i in (5, 6, 7)), str(north_colors))

    south_colors = _band_colors(cfg, south)
    check("南半球：12/1/2 月自动变为夏色",
          all(south_colors.get(i) == summer for i in (11, 0, 1)), str(south_colors))
    check("南半球：6/7/8 月自动变为冬色",
          all(south_colors.get(i) == winter for i in (5, 6, 7)), str(south_colors))
    check("南半球：3/4/5 月为秋色、9/10/11 月为春色",
          all(south_colors.get(i) == autumn for i in (2, 3, 4))
          and all(south_colors.get(i) == spring for i in (8, 9, 10)), str(south_colors))

    force_north = load_config("seasonal", None,
                              [("figure.background.bands.hemisphere", "north")])
    check("hemisphere=north 时南半球城市保持原样",
          _band_colors(force_north, south).get(0) == winter)
    force_south = load_config("seasonal", None,
                              [("figure.background.bands.hemisphere", "south")])
    check("hemisphere=south 时北半球城市也反季",
          _band_colors(force_south, north).get(0) == summer)

    no_lat = city(1729)
    no_lat.latitude = None
    check("纬度缺失时按北半球处理", _band_colors(cfg, no_lat).get(0) == winter)

    alt_slots = sorted(_band_colors(load_config("alt_bands"), south))
    check("alternate 模式与半球无关", alt_slots == [0, 2, 4, 6, 8, 10], str(alt_slots))

    bad = load_config("seasonal")
    bad["figure"]["background"]["bands"]["hemisphere"] = "southpole"
    try:
        validate_config(bad)
    except ConfigError as exc:
        check("非法 hemisphere 报错", "hemisphere" in str(exc), str(exc))
    else:
        check("非法 hemisphere 报错", False, "未抛出")


def test_compare_render() -> None:
    cfg = load_config("compare")
    cities = [city(237), city(1), city(156)]
    out_dir = VERIFY_DIR / "多城市对比"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "三城对比_气温.png"
    render_comparison_chart(cities, cfg, [path], logger=None)
    check("三城市气温对比渲染成功", path.exists() and path.stat().st_size > 5000)

    cfg_rain = load_config("compare_rain")
    path2 = out_dir / "三城对比_降水.png"
    render_comparison_chart(cities, cfg_rain, [path2], logger=None)
    check("三城市降水对比渲染成功", path2.exists() and path2.stat().st_size > 5000)

    cfg_bar = load_config("compare", None, [("compare.chart_type", "bar")])
    path3 = out_dir / "三城对比_柱状.png"
    render_comparison_chart(cities, cfg_bar, [path3], logger=None)
    check("对比柱状图渲染成功", path3.exists() and path3.stat().st_size > 5000)


# ======================= 4b. 多图（多城市同画布） =======================

def test_parse_grid() -> None:
    """排列方式解析：前=列数、后=行数；多种分隔符等价；非法值报错。"""
    from src.config_loader import ConfigError as CfgErr
    from src.config_loader import parse_grid, resolve_grid

    check("parse_grid('2x2') == (2, 2)", parse_grid("2x2") == (2, 2), str(parse_grid("2x2")))
    check("parse_grid('3x2') == (3, 2)（前=列、后=行）",
          parse_grid("3x2") == (3, 2), str(parse_grid("3x2")))
    for text in ("2X2", "2×2", "2*2", "2,2", "2，2", " 2 x 2 "):
        check(f"parse_grid({text!r}) 与 2x2 等价",
              parse_grid(text) == (2, 2), str(parse_grid(text)))
    check("auto / 空值 → (0, 0) 表示自动",
          parse_grid("auto") == (0, 0) and parse_grid(None) == (0, 0) and parse_grid("") == (0, 0))
    check("resolve_grid(auto, 3) → 1 行 3 列（全横排）",
          resolve_grid({"grid": "auto"}, 3) == (3, 1), str(resolve_grid({"grid": "auto"}, 3)))
    check("resolve_grid 显式网格原样返回", resolve_grid({"grid": "2x3"}, 6) == (2, 3))
    for bad in ("abc", "2", "2x", "x2", "0x2", "2x0", "2x2x2", "-1x2"):
        try:
            parse_grid(bad)
        except CfgErr:
            check(f"parse_grid({bad!r}) 报错", True)
        else:
            check(f"parse_grid({bad!r}) 报错", False, "未抛异常")

    bad_cfg = load_config(None)
    bad_cfg["multi"]["grid"] = "2x"
    try:
        validate_config(bad_cfg)
    except CfgErr as exc:
        check("非法 multi.grid 被校验拦下", "multi.grid" in str(exc), str(exc))
    else:
        check("非法 multi.grid 被校验拦下", False, "未抛出")
    bad_cfg = load_config(None)
    bad_cfg["multi"]["legend"] = "many"
    try:
        validate_config(bad_cfg)
    except CfgErr as exc:
        check("非法 multi.legend 被校验拦下", "multi.legend" in str(exc), str(exc))
    else:
        check("非法 multi.legend 被校验拦下", False, "未抛出")
    bad_cfg = load_config(None)
    bad_cfg["multi"]["share_ylim"] = "all"
    try:
        validate_config(bad_cfg)
    except CfgErr as exc:
        check("非法 multi.share_ylim 被校验拦下", "share_ylim" in str(exc), str(exc))
    else:
        check("非法 multi.share_ylim 被校验拦下", False, "未抛出")


def _multi_figure(cfg, cities, grid=None):
    """渲染多图但**不落盘**，返回 (Figure, report)。"""
    from src import chart as chart_mod

    captured: dict = {}
    original = chart_mod._save
    chart_mod._save = lambda fig, out_paths, cfg: (captured.__setitem__("fig", fig), out_paths)[1]
    try:
        report: dict = {}
        render_multi_city_chart(cities, cfg, [], None, report, grid=grid)
        captured["report"] = report
    finally:
        chart_mod._save = original
    fig = captured["fig"]
    fig.canvas.draw()
    return fig, captured.get("report") or {}


def _legend_count(fig) -> int:
    return len(list(fig.legends) + [a.get_legend() for a in fig.axes if a.get_legend() is not None])


def test_multi_city_chart() -> None:
    """多图：同一画布多城市；最左列留左轴、最右列留右轴；同行量程统一；空位不画。"""
    from src import chart as chart_mod

    cities = [city(237), city(1), city(156), city(1007)]
    cfg = load_config(None, None, [("multi.grid", "2x2")])
    fig, report = _multi_figure(cfg, cities, (2, 2))

    # 每格一套主轴 + 副轴（主/副轴由 subplots 与 twinx 依次创建，顺序即面板顺序）
    check("2x2 四格各有一套主轴+副轴", len(fig.axes) == 8, str(len(fig.axes)))
    primaries, secondaries = fig.axes[:4], fig.axes[4:8]

    check("四格都绘制（无空位）",
          [a.get_visible() for a in primaries] == [True] * 4,
          str([a.get_visible() for a in primaries]))
    check("每格标题含对应城市名",
          [a.get_title() for a in primaries] == [f"{c.city_name} 气候统计" for c in cities],
          str([a.get_title() for a in primaries]))
    check("每格横轴刻度保持原样（12 个月）",
          all(len(a.get_xticklabels()) == 12 for a in primaries),
          str([len(a.get_xticklabels()) for a in primaries]))

    # 轴上刻度：最左列保留左轴 → 只有第 0 列可见；最右列保留右轴 → 只有第 1 列可见
    left_shown = [bool(a.yaxis.get_majorticklabels()) for a in primaries]
    check("最左列保留左轴刻度、右列隐藏", left_shown == [True, False, True, False], str(left_shown))
    right_shown = [bool(a.yaxis.get_majorticklabels()) for a in secondaries]
    check("最右列保留右轴刻度、左列隐藏", right_shown == [False, True, False, True], str(right_shown))
    check("隐藏侧连轴脊一起收掉",
          [a.spines["left"].get_visible() for a in primaries] == [True, False, True, False]
          and [a.spines["right"].get_visible() for a in secondaries]
          == [False, True, False, True],
          f"{[a.spines['left'].get_visible() for a in primaries]} "
          f"{[a.spines['right'].get_visible() for a in secondaries]}")
    check("其余子图不写纵轴标题",
          [bool(a.get_ylabel()) for a in primaries] == [True, False, True, False],
          str([a.get_ylabel() for a in primaries]))
    check("底部横轴脊照旧保留", all(a.spines["bottom"].get_visible() for a in primaries))

    # 量程：同一行统一（含副轴），不同行各自独立
    check("同一行主轴量程一致",
          primaries[0].get_ylim() == primaries[1].get_ylim()
          and primaries[2].get_ylim() == primaries[3].get_ylim(),
          f"{primaries[0].get_ylim()} {primaries[1].get_ylim()} "
          f"{primaries[2].get_ylim()} {primaries[3].get_ylim()}")
    check("同一行副轴量程一致",
          secondaries[0].get_ylim() == secondaries[1].get_ylim(),
          f"{secondaries[0].get_ylim()} {secondaries[1].get_ylim()}")
    check("不同行量程相互独立（北京与澳门数据不同）",
          primaries[0].get_ylim() != primaries[2].get_ylim(),
          f"{primaries[0].get_ylim()} vs {primaries[2].get_ylim()}")
    check("report 记下画布与城市", report.get("grid") == "2x2" and report.get("drawn") == 4,
          str(report))
    check("整幅只画一个图例（multi.legend=figure 默认）", _legend_count(fig) == 1,
          str(_legend_count(fig)))
    chart_mod.plt.close(fig)

    # share_ylim=none：同一行也各画各的
    cfg_none = load_config(None, None, [("multi.grid", "2x2"), ("multi.share_ylim", "none")])
    fig_none, _ = _multi_figure(cfg_none, cities, (2, 2))
    check("share_ylim=none 时同一行也不统一",
          fig_none.axes[0].get_ylim() != fig_none.axes[1].get_ylim(),
          f"{fig_none.axes[0].get_ylim()} vs {fig_none.axes[1].get_ylim()}")
    chart_mod.plt.close(fig_none)

    # 图例三态
    cfg_none_legend = load_config(None, None, [("multi.legend", "none")])
    fig_nl, _ = _multi_figure(cfg_none_legend, cities, (4, 1))
    check("multi.legend=none 时不画图例", _legend_count(fig_nl) == 0, str(_legend_count(fig_nl)))
    chart_mod.plt.close(fig_nl)
    cfg_per = load_config(None, None, [("multi.legend", "per_chart")])
    fig_per, _ = _multi_figure(cfg_per, cities, (4, 1))
    check("multi.legend=per_chart 时每格一个图例", _legend_count(fig_per) == 4,
          str(_legend_count(fig_per)))
    chart_mod.plt.close(fig_per)

    # 默认排列：1×N 全横排
    fig_auto, report_auto = _multi_figure(load_config(None), cities)
    check("默认排列为 1 行 N 列", report_auto.get("grid") == "4x1", str(report_auto))
    left_flags = [fig_auto.axes[i].spines["left"].get_visible() for i in range(4)]
    check("1 行 4 列时只最左格留左轴", left_flags == [True, False, False, False], str(left_flags))
    chart_mod.plt.close(fig_auto)

    # 城市数少于格子：空位不画
    fig_few, report_few = _multi_figure(cfg, cities[:3], (2, 2))
    check("城市不足时末格隐藏",
          [a.get_visible() for a in fig_few.axes[:4]] == [True, True, True, False],
          str([a.get_visible() for a in fig_few.axes[:4]]))
    check("城市不足时 report.drawn 记实际格数", report_few.get("drawn") == 3, str(report_few))
    chart_mod.plt.close(fig_few)

    # 画布尺寸 = 单个 figsize × (列, 行)；multi.figsize 可覆盖
    fig_size, _ = _multi_figure(cfg, cities, (2, 2))
    base = load_config(None)["figure"]["figsize"]
    check("画布按行列放大", fig_size.get_size_inches().tolist() == [base[0] * 2, base[1] * 2],
          str(fig_size.get_size_inches().tolist()))
    chart_mod.plt.close(fig_size)
    cfg_fig = load_config(None, None, [("multi.figsize", [8.0, 5.0])])
    fig_override, _ = _multi_figure(cfg_fig, cities, (2, 2))
    check("multi.figsize 覆盖整幅画布",
          fig_override.get_size_inches().tolist() == [8.0, 5.0],
          str(fig_override.get_size_inches().tolist()))
    chart_mod.plt.close(fig_override)


def test_run_multi_offline(tmp: Path) -> None:
    """多图编排：城市数与画布不匹配时只告警不报错，且能落盘。"""
    import src.pipeline as pipeline_mod

    class _Rec:
        def __init__(self) -> None:
            self.msgs: list[tuple[str, str]] = []

        def debug(self, msg: str) -> None:
            self.msgs.append(("DEBUG", str(msg)))

        def info(self, msg: str) -> None:
            self.msgs.append(("INFO", str(msg)))

        def warning(self, msg: str) -> None:
            self.msgs.append(("WARN", str(msg)))

        def error(self, msg: str) -> None:
            self.msgs.append(("ERROR", str(msg)))

        def notes(self) -> str:
            return "\n".join(f"{lv}: {m}" for lv, m in self.msgs)

    out_dir = VERIFY_DIR / "多图"
    out_dir.mkdir(parents=True, exist_ok=True)
    original = pipeline_mod.fetch_city
    pipeline_mod.fetch_city = lambda client, cfg, city_id, logger: city(city_id)
    try:
        # 1) 4 城 2x2：正好填满
        logger = _Rec()
        cfg = load_config(None, None, [("multi.grid", "2x2")])
        summary = pipeline_mod.run_multi(cfg, [237, 1, 156, 1007], logger, out_dir)
        check("多图落盘成功", len(summary.multi_charts) == 1
              and summary.multi_charts[0].exists()
              and summary.multi_charts[0].stat().st_size > 5000,
              str(summary.multi_charts))
        check("画布记入汇总", summary.grid == "2x2" and summary.skipped_ids == [],
              f"{summary.grid} {summary.skipped_ids}")
        check("4 城填满 2x2 时不告警", "不匹配" not in logger.notes(), logger.notes())
        check("汇总列出已绘制城市",
              all("已绘制" in ln for ln in summary.describe().splitlines()[1:]),
              summary.describe())

        # 2) 3 城塞 2x2：留空 + 告警，不报错
        logger = _Rec()
        summary = pipeline_mod.run_multi(cfg, [237, 1, 156], logger, out_dir)
        check("城市数不足时告警（不报错）",
              any("不匹配" in m and "留空" in m for _lv, m in logger.msgs), logger.notes())
        check("城市数不足时仍出图", len(summary.multi_charts) == 1, str(summary.multi_charts))

        # 3) 5 城塞 2x2：超出部分不绘制 + 告警
        logger = _Rec()
        summary = pipeline_mod.run_multi(cfg, [237, 1, 156, 1007, 2184], logger, out_dir)
        check("城市数超出时告警并列出未绘制城市",
              any("超过画布" in m and "希洪" in m for _lv, m in logger.msgs), logger.notes())
        check("超出格数的城市记入 skipped_ids",
              summary.skipped_ids == [2184] and len(summary.drawn_ids) == 4,
              f"{summary.skipped_ids} {summary.drawn_ids}")
        check("超出部分在城市清单里标注未绘制",
              "超出画布格数，未绘制" in summary.describe(), summary.describe())
        check("超出时仍正常出图", len(summary.multi_charts) == 1, str(summary.multi_charts))

        # 4) 取数失败的城市：留空 + 告警，其余照画
        def _boom(client, cfg, city_id, logger):
            if city_id == 1:
                raise pipeline_mod.FetchError("模拟取数失败")
            return city(city_id)

        pipeline_mod.fetch_city = _boom
        logger = _Rec()
        summary = pipeline_mod.run_multi(load_config(None, None, [("multi.grid", "2x2")]),
                                         [237, 1, 156], logger, out_dir)
        check("取数失败的城市告警且不影响其余",
              any(lv == "ERROR" and "取数失败" in m for lv, m in logger.msgs)
              and len(summary.multi_charts) == 1, logger.notes())
        check("失败城市不计入失败退出条件之外的统计",
              summary.fail_count == 1 and summary.ok_count == 2,
              f"{summary.fail_count} {summary.ok_count}")

        # 5) 文件名含画布尺寸，不同排列不会互相覆盖
        pipeline_mod.fetch_city = lambda client, cfg, city_id, logger: city(city_id)
        cfg_row = load_config(None)
        row_summary = pipeline_mod.run_multi(cfg_row, [237, 1], _Rec(), out_dir)
        check("默认横排与 2x2 的产物文件名不冲突",
              row_summary.multi_charts[0].name != summary.multi_charts[0].name
              and "2x1" in row_summary.multi_charts[0].name,
              f"{row_summary.multi_charts[0].name} / {summary.multi_charts[0].name}")
    finally:
        pipeline_mod.fetch_city = original


# ======================= 5. 城市索引 =======================

def test_cli_batch_targets() -> None:
    """CLI 批量目标解析：逗号分割、逐项去空白、去重，且不误拆全角逗号城市名。"""
    import wmo_climate as cli

    check("逗号分割并逐项去空白",
          cli._split_targets(["237, 1 ,156"]) == ["237", "1", "156"],
          str(cli._split_targets(["237, 1 ,156"])))
    check("重复传参与逗号写法等价",
          cli._split_targets(["237", "1, 156"]) == ["237", "1", "156"],
          str(cli._split_targets(["237", "1, 156"])))
    check("丢弃空项", cli._split_targets([",237,,1,"]) == ["237", "1"],
          str(cli._split_targets([",237,,1,"])))
    check("全角逗号的城市名保持完整（圣保罗，明尼苏达州）",
          cli._split_targets(["圣保罗，明尼苏达州"]) == ["圣保罗，明尼苏达州"],
          str(cli._split_targets(["圣保罗，明尼苏达州"])))
    check("城市名同样支持逗号批量",
          cli._split_targets(["北京, 香港 ,莫斯科"]) == ["北京", "香港", "莫斯科"],
          str(cli._split_targets(["北京, 香港 ,莫斯科"])))
    check("去重保持首次出现顺序",
          cli._dedup_targets([237, 1, 237, 156, 1]) == [237, 1, 156],
          str(cli._dedup_targets([237, 1, 237, 156, 1])))
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(["--city-id", "237,abc"])
    check("非法 city-id 报错退出码 2（且不发起请求）", code == 2, str(code))
    check("错误提示含用法示例",
          "--city-id" in buf.getvalue() and "237,1,156" in buf.getvalue(), buf.getvalue())

    # main 的批量接线：把 run_batch 换成记录器，验证 argv → 列表 → 逐个处理（不打网络）
    class _Summary:
        fail_count = 0
        compare_charts: list = []

        def describe(self) -> str:
            return "(批量记录器)"

    seen: list[list[int]] = []
    original = cli.run_batch
    cli.run_batch = lambda cfg, ids, logger, out_dir: (seen.append(list(ids)), _Summary())[1]
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["--city-id", "237, 1 ,237", "--no-chart", "--no-table"])
    finally:
        cli.run_batch = original
    check("main 把逗号列表解析成城市列表并去重后交给批量处理",
          seen == [[237, 1]] and code == 0, f"seen={seen} code={code}")


def test_cli_mark() -> None:
    """--mark/--m 绘图微调：档位、累计、封顶、大小写、告警不中断。"""
    from src import chart as chart_mod

    def marked(overrides, marks):
        cfg = load_config(None, None, overrides)
        notes, warns = apply_marks(cfg, marks)
        return cfg, notes, warns

    # ---- h / c：气温轴量程整体平移（上下限同向），累计封顶 ±3 档 ----
    for token, shift in (("h", 10.0), ("hh", 20.0), ("hhh", 30.0),
                         ("c", -10.0), ("cc", -20.0), ("ccc", -30.0)):
        cfg, _, _ = marked([], [token])
        check(f"{token}：气温轴 {shift:+g}", cfg["axes_primary"]["limit_shift"] == shift,
              str(cfg["axes_primary"]["limit_shift"]))
    cfg, _, warns = marked([], ["hhhh"])
    check("hhhh 截取为 3 档并告警", cfg["axes_primary"]["limit_shift"] == 30.0
          and any("最多" in w for w in warns), str(warns))
    cfg, _, _ = marked([], ["h,c"])
    check("h 与 c 相互抵消", cfg["axes_primary"]["limit_shift"] == 0.0,
          str(cfg["axes_primary"]["limit_shift"]))
    cfg, _, _ = marked([], ["h", "hh"])                     # 重复给出，档位累计
    check("多个标记档位累计（h+hh=+30）", cfg["axes_primary"]["limit_shift"] == 30.0,
          str(cfg["axes_primary"]["limit_shift"]))
    cfg, _, _ = marked([("axes_primary.limit_shift", 5)], ["h"])
    check("h 在既有 limit_shift 上累计", cfg["axes_primary"]["limit_shift"] == 15.0,
          str(cfg["axes_primary"]["limit_shift"]))

    # ---- r：降水轴上限档位（多数字相加、封顶 4000、保留现有下限） ----
    for token, upper in (("r0", 50.0), ("r12", 250.0), ("r02", 200.0), ("r12345", 2050.0)):
        cfg, _, _ = marked([], [token])
        check(f"{token}：降水轴上限 {upper:g}",
              cfg["axes_secondary"]["limit"] == [0.0, upper],
              str(cfg["axes_secondary"]["limit"]))
    cfg, _, warns = marked([], ["r12345", "r12345"])
    check("r 档位相加封顶 4000 并告警", cfg["axes_secondary"]["limit"] == [0.0, 4000.0]
          and any("截取" in w for w in warns), str(warns))
    cfg, _, warns = marked([], ["r19"])
    check("r 忽略 0-5 之外数字（r19=100）",
          cfg["axes_secondary"]["limit"] == [0.0, 100.0] and warns, str(warns))
    cfg, _, warns = marked([], ["r7"])
    check("r 无有效档位只告警、不改配置",
          cfg["axes_secondary"]["limit"] == [] and warns, str(warns))
    cfg, _, _ = marked([("axes_secondary.limit", [20, 600])], ["r1"])
    check("r 保留现有下限", cfg["axes_secondary"]["limit"] == [20.0, 100.0],
          str(cfg["axes_secondary"]["limit"]))

    # ---- ts / rs：刻度步长（小于量程 1/10 不生效） ----
    cfg, notes, _ = marked([("axes_primary.limit", [-30, 30]),
                            ("axes_secondary.limit", [0, 900])], ["ts10", "rs100"])
    check("ts/rs 设置刻度步长",
          cfg["axes_primary"]["tick_step"] == 10.0 and cfg["axes_secondary"]["tick_step"] == 100.0,
          f"{cfg['axes_primary']['tick_step']} {cfg['axes_secondary']['tick_step']}")
    cfg, notes, _ = marked([("axes_primary.limit", [-30, 30]),
                            ("axes_secondary.limit", [0, 900])], ["ts5", "rs50"])
    check("步长小于量程 1/10 时不生效",
          cfg["axes_primary"]["tick_step"] is None and cfg["axes_secondary"]["tick_step"] is None,
          f"{cfg['axes_primary']['tick_step']} {cfg['axes_secondary']['tick_step']}")
    cfg, _, warns = marked([("axes_secondary.limit", [0, 900])], ["rs50"])
    check("过密步长给出告警并说明下限",
          any("过密" in w and "90" in w for w in warns), str(warns))
    cfg, _, _ = marked([("axes_secondary.limit", [0, 900])], ["rs90", "rs91"])
    check("步长等于量程 1/10 时生效（后值覆盖）",
          cfg["axes_secondary"]["tick_step"] == 91.0, str(cfg["axes_secondary"]["tick_step"]))
    cfg, _, warns = marked([("axes_primary.limit", [-30, 30])], ["ts0", "ts-3"])
    check("步长须为正数，0/负数只告警",
          cfg["axes_primary"]["tick_step"] is None and len(warns) == 2, str(warns))
    cfg, notes, _ = marked([], ["ts10"])                # 自动量程：无法预判，照常写入并注明
    check("自动量程时步长照常写入且注明未预判",
          cfg["axes_primary"]["tick_step"] == 10.0
          and any("未预判" in n for n in notes), f"{cfg['axes_primary']['tick_step']} {notes}")

    # ---- t / p / tt：字号（tt0 隐藏标题） ----
    cfg, _, _ = marked([], ["t14", "p12", "tt18"])
    check("t/p/tt 字号生效",
          cfg["axes_primary"]["label_fontsize"] == 14.0
          and cfg["axes_secondary"]["label_fontsize"] == 12.0
          and cfg["figure"]["title"]["fontsize"] == 18.0
          and cfg["figure"]["title"]["show"] is True,
          f"{cfg['axes_primary']['label_fontsize']} "
          f"{cfg['axes_secondary']['label_fontsize']} {cfg['figure']['title']}")
    cfg, _, _ = marked([], ["tt0"])
    check("tt0 隐藏图表标题", cfg["figure"]["title"]["show"] is False
          and cfg["figure"]["title"]["fontsize"] == 0.0, str(cfg["figure"]["title"]))
    cfg, _, _ = marked([("figure.title.show", False)], ["tt18"])
    check("tt>0 恢复显示标题", cfg["figure"]["title"]["show"] is True)
    cfg, _, _ = marked([], ["T14", "P12"])                  # 大小写等价
    check("字号标记大小写等价", cfg["axes_primary"]["label_fontsize"] == 14.0
          and cfg["axes_secondary"]["label_fontsize"] == 12.0)

    # ---- cy / cn：城市名是否取逗号前第一段 ----
    cfg, notes, _ = marked([], ["cy"])
    check("cy：城市名取逗号前第一段（短名）",
          cfg["data"]["city_short_name"] is True
          and any("短名" in n for n in notes), f"{cfg['data']['city_short_name']} {notes}")
    cfg, notes, _ = marked([], ["cn"])
    check("cn：城市名使用完整名称",
          cfg["data"]["city_short_name"] is False
          and any("完整名称" in n for n in notes), f"{cfg['data']['city_short_name']} {notes}")
    cfg, _, _ = marked([("data.city_short_name", True)], ["cn"])
    check("cn 覆盖既有短名设置", cfg["data"]["city_short_name"] is False)
    cfg, _, _ = marked([], ["CY", "Cn"])                     # 大小写等价、后者覆盖
    check("cy/cn 大小写等价且后者覆盖", cfg["data"]["city_short_name"] is False)

    # ---- 无法识别的标记：只告警，不中断，不影响其余标记 ----
    cfg, _, warns = marked([], ["hh", "x9", "r12", "z", "p20"])
    check("无法识别的标记只告警",
          any("x9" in w for w in warns) and any("z" in w for w in warns)
          and cfg["axes_primary"]["limit_shift"] == 20.0
          and cfg["axes_secondary"]["limit"] == [0.0, 250.0]
          and cfg["axes_secondary"]["label_fontsize"] == 20.0,
          str(warns))

    # ---- 空白与逗号：去除所有空格，逗号分隔 ----
    cfg, _, _ = marked([], [" h , r12 "])
    check("逗号分隔且空格全去除", cfg["axes_primary"]["limit_shift"] == 10.0
          and cfg["axes_secondary"]["limit"] == [0.0, 250.0],
          f"{cfg['axes_primary']['limit_shift']} {cfg['axes_secondary']['limit']}")

    # ---- 落到绘图：limit_shift 让自动量程整体平移 ----
    base = _render_figure(load_config(None), city(237))
    shifted = _render_figure(load_config(None, None, [("axes_primary.limit_shift", 10)]), city(237))
    expected = [v + 10 for v in base.axes[0].get_ylim()]
    check("limit_shift 使自动量程整体平移",
          all(abs(a - b) < 1e-9 for a, b in zip(expected, shifted.axes[0].get_ylim())),
          f"{base.axes[0].get_ylim()} -> {shifted.axes[0].get_ylim()}")
    chart_mod.plt.close(base)
    chart_mod.plt.close(shifted)


def test_city_display_name() -> None:
    """data.city_short_name 控制城市名是否取逗号前第一段。"""
    from src.models import CityClimate, city_display_name

    full = CityClimate(city_id=1, city_name="洛杉矶，加利福尼亚州")
    comma = CityClimate(city_id=2, city_name="London, United Kingdom")
    none = CityClimate(city_id=3, city_name="Singapore")
    cfg_full = {}                                  # 无 data 段 → 完整名称
    cfg_short = {"data": {"city_short_name": True}}
    cfg_false = {"data": {"city_short_name": False}}

    check("默认/无配置：完整名称", city_display_name(full, cfg_full) == "洛杉矶，加利福尼亚州")
    check("city_short_name=True：取逗号前第一段", city_display_name(full, cfg_short) == "洛杉矶")
    check("city_short_name=False：完整名称", city_display_name(full, cfg_false) == "洛杉矶，加利福尼亚州")
    check("无逗号时短名=全名", city_display_name(none, cfg_short) == "Singapore")
    check("半角逗号同样截取前段", city_display_name(comma, cfg_short) == "London")
    check("CityIndexEntry 同款属性也可取（无 city_name 兜底空串）",
          city_display_name(object(), cfg_short) == "")


def test_request_throttle() -> None:
    """批量请求排队限速：相邻网络请求间隔不小于 fetch.min_interval（默认 1 秒）。"""
    import shutil

    from src import http_client as hc

    check("默认限速为每秒 1 次请求",
          float(load_config(None)["fetch"]["min_interval"]) == 1.0,
          str(load_config(None)["fetch"].get("min_interval")))

    # 1) 纯函数：首次不等待，随后的请求补足间隔
    state = {"t": 1000.0}
    slept: list[float] = []

    def fake_sleep(sec: float) -> None:
        slept.append(round(sec, 3))
        state["t"] += sec

    hc._last_request_at = 0.0
    sent: list[float] = []
    for bump in (0.0, 0.2, 0.0, 3.0):        # 立刻、0.2s 后、再立刻、隔了 3s 再发
        state["t"] += bump
        hc.throttle(1.0, clock=lambda: state["t"], sleep=fake_sleep)
        sent.append(state["t"])
    gaps = [round(b - a, 3) for a, b in zip(sent, sent[1:])]
    check("相邻请求间隔均不小于 1s（即每秒最多 1 次请求）",
          all(g >= 1.0 - 1e-9 for g in gaps), f"间隔={gaps}")
    check("首次与已超间隔的请求不等待，其余精确补足",
          slept == [0.8, 1.0], str(slept))

    hc._last_request_at = 0.0
    no_sleep: list[float] = []
    hc.throttle(0.0, clock=lambda: state["t"], sleep=no_sleep.append)
    check("min_interval=0 时不限速", no_sleep == [], str(no_sleep))

    # 2) 每个真实请求（含重试）都排队；命中缓存则不排队
    calls: list[float] = []
    original = hc.throttle
    hc.throttle = lambda interval, logger=None, clock=None, sleep=None: calls.append(interval)
    cache_dir = ROOT / "tests" / "_output" / "throttle_cache"
    shutil.rmtree(cache_dir, ignore_errors=True)
    try:
        client = hc.HttpClient({"min_interval": 0.5}, cache_dir=cache_dir)
        client._request_once = lambda url, headers: hc.FetchResult(
            url=url, status=200, content=b"{}", text="{}")
        client.get("https://example.invalid/a")
        client.get("https://example.invalid/b")
        check("每个真实请求前都按 min_interval 排队", calls == [0.5, 0.5], str(calls))

        calls.clear()
        client.get("https://example.invalid/a")       # 命中缓存
        check("命中缓存不排队也不计数",
              calls == [] and client.request_count == 2, f"{calls} / {client.request_count}")
    finally:
        hc.throttle = original
        hc._last_request_at = 0.0
        shutil.rmtree(cache_dir, ignore_errors=True)


def test_proxy_config() -> None:
    """代理与证书校验只认配置，不被环境变量暗中接管。

    实测踩过的坑：env 里留着指向失效端口的 ``HTTP_PROXY``／``HTTPS_PROXY`` 时，
    即使 ``fetch.proxy`` 为空（语义是"直连"），urllib 的默认 opener 也会把请求送去
    那个死端口，表现为全量 ``URLError: [WinError 10061] 目标计算机积极拒绝``。
    """
    import os
    import ssl
    import urllib.request

    from src import http_client as hc

    saved = {k: os.environ.get(k) for k in
             ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}
    # 只写、不按小写名清理：Windows 上 os.environ 大小写不敏感，pop("http_proxy") 会把
    # 刚设好的 HTTP_PROXY 一起删掉（本测试第一版就栽在这上面）。
    os.environ["HTTP_PROXY"] = "http://127.0.0.1:7897"
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7897"

    def proxies_of(client) -> dict:
        handler = [h for h in client.opener.handlers
                   if isinstance(h, urllib.request.ProxyHandler)][0]
        return dict(handler.proxies)

    def ssl_context_of(client):
        handlers = [h for h in client.opener.handlers
                    if isinstance(h, urllib.request.HTTPSHandler)]
        return getattr(handlers[0], "_context", None) if handlers else None

    try:
        base = dict(load_config(None)["fetch"])
        check("默认 use_env_proxy 为 false", base.get("use_env_proxy") is False,
              str(base.get("use_env_proxy")))
        check("env 里确实有代理（前提成立）",
              urllib.request.getproxies().get("https") == "http://127.0.0.1:7897",
              str(urllib.request.getproxies()))

        direct = hc.HttpClient(dict(base), cache_dir=None)
        check("proxy 为空时忽略环境变量代理（直连）", proxies_of(direct) == {},
              str(proxies_of(direct)))
        check("直连处理器真的注册进了 opener（不是靠 ProxyHandler({}) 的副作用）",
              any(isinstance(h, hc._NoProxyHandler) for h in direct.opener.handlers),
              str([type(h).__name__ for h in direct.opener.handlers]))

        env = hc.HttpClient(dict(base, use_env_proxy=True), cache_dir=None)
        check("use_env_proxy=true 时才沿用环境变量代理",
              proxies_of(env).get("https") == "http://127.0.0.1:7897",
              str(proxies_of(env)))

        explicit = hc.HttpClient(dict(base, proxy="http://127.0.0.1:7890"), cache_dir=None)
        check("显式 proxy 生效且不被 env 覆盖",
              proxies_of(explicit).get("https") == "http://127.0.0.1:7890",
              str(proxies_of(explicit)))

        # 注意：Python 3.13 的 HTTPSHandler 会把 context=None 落成"默认校验"上下文，
        # 所以判据是 verify_mode（CERT_REQUIRED）而非 _context 是否为 None。
        ctx_verified = ssl_context_of(direct)
        check("verify_ssl=true 时沿用默认证书校验",
              ctx_verified is not None and ctx_verified.verify_mode != ssl.CERT_NONE,
              f"{ctx_verified} verify_mode={getattr(ctx_verified, 'verify_mode', None)}")
        unverified = hc.HttpClient(
            dict(base, proxy="http://127.0.0.1:7890", verify_ssl=False), cache_dir=None)
        ctx = ssl_context_of(unverified)
        check("verify_ssl=false 在代理模式下同样生效",
              ctx is not None and ctx.verify_mode == ssl.CERT_NONE, str(ctx))
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_city_index_flatten() -> None:
    raw = json.loads((ROOT / "tests" / "fixtures" / "country_index_zh.json").read_text(encoding="utf-8-sig"))
    entries = _flatten(raw)
    check("索引城市数 > 3000", len(entries) > 3000, str(len(entries)))
    check("索引含语言键未污染", all(e.city_id > 0 for e in entries))
    index = CityIndex(entries)
    found = index.resolve_one("北京")
    check("按名称反查北京", found.city_id == 237 and found.mem_name == "中国",
          f"{found.city_id} {found.mem_name}")
    hk = index.resolve_one("香港")
    check("按名称反查香港", hk.city_id == 1 and hk.mem_name == "中国香港",
          f"{hk.city_id} {hk.mem_name}")
    check("搜索无结果返回空", index.search("这个城市肯定不存在zzz") == [])
    check("按国家筛选可用", len(index.list_by_country("中国")) > 10)


# ======================= 6. 联网用例（可选） =======================

def test_network() -> None:
    from src.http_client import CityNotFoundError, HttpClient, city_data_url
    cfg = load_config(None)
    client = HttpClient(cfg["fetch"], logger=None,
                        cache_dir=ROOT / "cache")
    url = city_data_url(cfg["fetch"], 237, "zh")
    result = client.get(url, use_cache=False)
    check("联网取北京数据成功", result.status == 200 and len(result.content) > 1000,
          f"{result.status} {len(result.content)}")
    try:
        client.get(city_data_url(cfg["fetch"], 999999, "zh"), use_cache=False)
    except CityNotFoundError:
        check("非法 cityId 抛 CityNotFoundError", True)
    else:
        check("非法 cityId 抛 CityNotFoundError", False, "未抛出")


# ======================= 主流程 =======================

def main() -> int:
    import shutil

    tmp = ROOT / "tests" / "_output" / "tables"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print("WMO 城市气候工具 · 回归测试")
    print("=" * 72)

    run("解析层：数值容错", test_parse_number_tolerance)
    run("解析层：北京数值与派生", test_beijing_values)
    run("解析层：raintype 变体", test_raintype_variants)
    run("解析层：缺失值处理", test_missing_values)
    run("解析层：无气候数据城市", test_no_climate_city)
    run("解析层：月份标签与元素别名", test_month_label_and_alias)
    run("显示层：经纬度格式", test_coord_formatting)
    run("配置层：深合并与 --set", test_config_merge_and_set)
    run("配置层：多套配置解析", test_profiles_resolution)
    run("配置层：非法配置报错", test_config_errors)
    run("配置层：未知配置项告警", test_config_unknown_key_warning)
    run("配置层：自定义配置多文件继承", test_custom_config_multi_file)
    run("配置层：配置一律为 YAML", test_config_files_are_yaml)
    run("表格层：基础四格式", lambda: test_tables(tmp))
    run("表格层：变体（年列/英制/极简/空值）", lambda: test_tables_variants(tmp))
    run("绘图层：全部配置渲染", test_all_profiles_render)
    run("绘图层：边界城市渲染", test_edge_city_render)
    run("绘图层：元素全部关闭", test_series_toggle)
    run("绘图层：背景色带层级", test_background_bands_layering)
    run("绘图层：季节色带半球反季", test_seasonal_bands_hemisphere)
    run("绘图层：平均降水线", test_mean_rain_line)
    run("绘图层：极值标注自动避让", test_extremes_label_avoidance)
    run("绘图层：极值标注窄空间避让", test_extremes_label_avoidance_cramped_axis)
    run("绘图层：多城市对比", test_compare_render)
    run("绘图层：多图排列解析", test_parse_grid)
    run("绘图层：多图（多城市同画布）", test_multi_city_chart)
    run("编排层：多图告警与落盘", lambda: test_run_multi_offline(tmp))
    run("城市索引：反查与筛选", test_city_index_flatten)
    run("CLI：批量目标解析", test_cli_batch_targets)
    run("CLI：--mark 绘图微调", test_cli_mark)
    run("显示层：城市短名开关", test_city_display_name)
    run("请求层：批量排队限速", test_request_throttle)
    run("请求层：代理与证书只认配置", test_proxy_config)

    if "--network" in sys.argv:
        run("联网：真实请求与 404", test_network)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]

    print(f"\n断言总数：{len(RESULTS)}    通过：{passed}    失败：{len(failed)}")
    if failed:
        print("\n失败明细：")
        for name, detail in failed:
            # 用 GBK 可编码的「×」而非 U+2717，避免 cp936 控制台在打印失败明细时崩掉
            print(f"  × {name}")
            if detail:
                print(f"      {detail.splitlines()[0] if detail else ''}")
    print(f"\n渲染核对图目录：{VERIFY_DIR}")
    print(f"表格测试产物目录：{tmp}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
