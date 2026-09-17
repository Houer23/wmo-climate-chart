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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import table_writer  # noqa: E402
from src.chart import render_city_chart, render_comparison_chart  # noqa: E402
from src.city_index import CityIndex, CityEntry, _flatten  # noqa: E402
from src.config_loader import (  # noqa: E402
    ConfigError,
    DEFAULTS,
    load_config,
    load_profiles_file,
    resolve_profile,
    validate_config,
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
    """自定义配置支持多文件：按上下顺序加载，后者可 extends 前者。"""
    import src.config_loader as cl

    tmp = ROOT / "tests" / "_output" / "custom_test"
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "00_base.json").write_text(
        json.dumps({"profiles": {"c_base": {"figure": {"figsize": [1, 1], "dpi": 10}}}}),
        encoding="utf-8")
    (tmp / "01_derived.json").write_text(
        json.dumps({"profiles": {"c_sub": {"extends": "c_base", "figure": {"dpi": 99}}}}),
        encoding="utf-8")
    (tmp / "10_dup_a.json").write_text(
        json.dumps({"profiles": {"c_dup": {"figure": {"dpi": 11}}}}), encoding="utf-8")
    (tmp / "10_dup_b.json").write_text(
        json.dumps({"profiles": {"c_dup": {"figure": {"dpi": 22}}}}), encoding="utf-8")

    saved_dir, saved_file = cl.CUSTOM_PROFILES_DIR, cl.CUSTOM_PROFILES_PATH
    cl.CUSTOM_PROFILES_DIR = tmp
    cl.CUSTOM_PROFILES_PATH = ROOT / "config" / "nonexistent_custom.json"
    try:
        doc = load_profiles_file()
        profiles = doc.get("profiles") or {}
        check("多文件自定义配置均被加载",
              {"c_base", "c_sub", "c_dup"} <= set(profiles),
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
    finally:
        cl.CUSTOM_PROFILES_DIR = saved_dir
        cl.CUSTOM_PROFILES_PATH = saved_file


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
    run("表格层：基础四格式", lambda: test_tables(tmp))
    run("表格层：变体（年列/英制/极简/空值）", lambda: test_tables_variants(tmp))
    run("绘图层：全部配置渲染", test_all_profiles_render)
    run("绘图层：边界城市渲染", test_edge_city_render)
    run("绘图层：元素全部关闭", test_series_toggle)
    run("绘图层：多城市对比", test_compare_render)
    run("城市索引：反查与筛选", test_city_index_flatten)

    if "--network" in sys.argv:
        run("联网：真实请求与 404", test_network)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]

    print(f"\n断言总数：{len(RESULTS)}    通过：{passed}    失败：{len(failed)}")
    if failed:
        print("\n失败明细：")
        for name, detail in failed:
            print(f"  ✗ {name}")
            if detail:
                print(f"      {detail.splitlines()[0] if detail else ''}")
    print(f"\n渲染核对图目录：{VERIFY_DIR}")
    print(f"表格测试产物目录：{tmp}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
