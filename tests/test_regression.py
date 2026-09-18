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
from src.chart import render_city_chart, render_comparison_chart  # noqa: E402
from src.city_index import CityIndex, CityEntry, _flatten  # noqa: E402
from src.config_loader import (  # noqa: E402
    ConfigError,
    DEFAULTS,
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
    run("城市索引：反查与筛选", test_city_index_flatten)
    run("CLI：批量目标解析", test_cli_batch_targets)
    run("请求层：批量排队限速", test_request_throttle)

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
