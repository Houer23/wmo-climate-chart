"""配置系统：内置完整默认值、深合并、多套 profiles、继承与样式复用、校验。

配置来源（后者覆盖前者）
----------------------------
1. 内置默认值 ``DEFAULTS``（本文件，含全部可配置项）
2. ``config/profiles.json`` 里的命名配置（可 ``extends`` 继承、可 ``apply_styles`` 复用样式）
3. 自定义配置（多文件，见 ``CUSTOM_PROFILES_PATH`` / ``CUSTOM_PROFILES_DIR``，按"上下顺序"加载，可跨文件 ``extends`` 继承）
4. 命令行 ``--set key.path=value`` 点路径覆盖

未指定 ``--profile`` 时使用 ``profiles.json`` 中的 ``default_profile``。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Optional

from .models import normalize_series_key

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_PROFILES_PATH = PROJECT_ROOT / "config" / "profiles.json"

# 自定义配置（多文件，按"上下顺序"加载）：
#   1) config/custom.json    —— 单文件（兼容旧用法）；若存在则最先加载
#   2) config/custom/*.json  —— 多文件目录，按文件名升序加载（忽略隐藏文件与子目录）
# 与内置 config/profiles.json 合并共存（同名时自定义优先）；后续文件可通过 extends 引用前文出现的同名配置。
CUSTOM_PROFILES_PATH = PROJECT_ROOT / "config" / "custom.json"
CUSTOM_PROFILES_DIR = PROJECT_ROOT / "config" / "custom"


class ConfigError(RuntimeError):
    """配置无法解析或非法。"""


def _series(**overrides: Any) -> dict[str, Any]:
    base = {
        # ---- 是否绘制与命名 ----
        "enabled": True,
        "label": "auto",             # auto = 按元素自动命名（降水名随 raintype 变化）
        # ---- 图形类型与归属轴 ----
        "chart_type": "smooth",      # line | smooth | bar | area | step | scatter | fill_between
        "axis": "primary",           # primary(温度轴) | secondary(降水轴)
        "scale_factor": 1.0,         # 数值缩放（如把降水日数压到与降水量同一量级）
        # ---- 线条样式 ----
        "color": "#000000",
        "alpha": 1.0,
        "linewidth": 2.0,
        "linestyle": "-",
        "zorder": 3,
        # ---- 数据点 ----
        "marker": "o",
        "markersize": 4.5,
        "markerfacecolor": "auto",   # auto = 与 color 一致
        "markeredgecolor": "auto",
        "markeredgewidth": 1.0,
        "markevery": 1,
        # ---- 柱状专有 ----
        "bar_width": 0.55,
        "bar_edgecolor": "none",
        "bar_linewidth": 0.0,
        "bar_align": "center",
        # ---- 平滑专有 ----
        "smooth_points": 240,
        # ---- 面积/区间带专有 ----
        "fill_alpha": 0.18,
        "fill_between_key": "min",   # 区间带的另一个边界元素（如 maxTemp 以 minTemp 为底）
        # ---- 数据标签 ----
        "data_labels": {
            "show": False,
            "fontsize": 8.5,
            "color": "auto",
            "format": "{:.1f}",
            "offset": 4.0,
            "rotation": 0,
            "position": "top",       # top | bottom | center
        },
        # ---- 图例顺序 ----
        "order_in_legend": 10,
    }
    base.update(overrides)
    return base


SERIES_DEFAULTS: dict[str, dict[str, Any]] = {
    # 官方页面配色：最高温 #eb6877 / 最低温 #0f91c4 / 降水 #46cbd4 / 平均温 #990000
    "minTemp": _series(
        label="日均最低气温", color="#0f91c4", chart_type="smooth",
        axis="primary", zorder=3, order_in_legend=20,
        data_labels={"show": False, "fontsize": 8.5, "color": "auto",
                     "format": "{:.1f}", "offset": -14.0, "rotation": 0, "position": "bottom"},
    ),
    "maxTemp": _series(
        label="日均最高气温", color="#eb6877", chart_type="smooth",
        axis="primary", zorder=3, order_in_legend=10,
        data_labels={"show": False, "fontsize": 8.5, "color": "auto",
                     "format": "{:.1f}", "offset": 8.0, "rotation": 0, "position": "top"},
    ),
    "meanTemp": _series(
        label="日均气温", color="#990000", chart_type="line", linestyle="--",
        linewidth=1.6, marker="", axis="primary", zorder=2, order_in_legend=30,
        enabled=False,
    ),
    "rainfall": _series(
        label="auto", color="#46cbd4", chart_type="bar", axis="secondary",
        alpha=0.9, bar_width=0.55, zorder=2, order_in_legend=40,
        data_labels={"show": False, "fontsize": 8.0, "color": "auto",
                     "format": "{:.0f}", "offset": 3.0, "rotation": 0, "position": "top"},
    ),
    "raindays": _series(
        label="平均降水日数", color="#0f6e56", chart_type="line", linestyle="-.",
        linewidth=1.6, marker="s", markersize=4.0, axis="tertiary",
        zorder=4, order_in_legend=50, scale_factor=1.0, enabled=False,
    ),
}

DEFAULTS: dict[str, Any] = {
    "profile_name": "default",
    "profile_description": "",

    # ================= A. 数据与字段口径 =================
    "data": {
        "lang": "zh",                      # 影响 URL 路径与月份标签语种
        "derive_mean_temp": True,          # 数据源 meanTemp 恒空 → 由 (min+max)/2 派生
        "temp_unit": "C",                  # C | F
        "rain_unit": "mm",                 # mm | inch
        "fill_missing": "gap",             # gap(断线) | zero(补0) | skip(跳过该点)
        "month_label_style": "1月",        # 1月 | 一月 | Jan | 01
        "include_annual": False,           # 表格/图表是否附年值
        "auto_disable_empty_series": True, # 某元素全无数据时自动不绘制
    },

    # ================= B. 请求层 =================
    "fetch": {
        "base_url": "https://worldweather.wmo.int",
        "city_page_path": "{lang}/city.html?cityId={city_id}",
        "data_path": "{lang}/json/{city_id}_{lang}.xml",
        "city_index_path": "{lang}/json/Country_{lang}.xml",
        "fetch_page_first": True,          # 先取 HTML 页做存在性校验与元信息核对
        "timeout": 30,
        "retries": 4,                      # 应对实测的间歇性 TLS 断连
        "backoff": 1.2,
        "backoff_max": 15,
        "min_interval": 1.0,               # 两次网络请求的最小间隔（秒）；批量成图时每秒 ≤ 1 次
        "verify_ssl": True,
        "proxy": "",
        "save_raw": False,
        "raw_dir": "cache/raw",
        "headers": {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
            "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                       "application/json;q=0.9,*/*;q=0.8"),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "identity",
            "Connection": "keep-alive",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Upgrade-Insecure-Requests": "1",
        },
        "cache": {
            "enabled": True,
            "ttl": 86400,
            "dir": "cache",
        },
    },

    # ================= C. 输出与文件 =================
    "output": {
        "out_dir": "output",
        "name_template": "{city}_{city_id}_climate_{profile}",       # 图片：带配置名后缀
        "table_name_template": "{city}_{city_id}_climate",           # 表格：不带配置名后缀
        "compare_name_template": "{city_count}城对比_{metric}_{profile}",
        "table_formats": ["csv", "md", "xlsx"],
        "chart_formats": ["png"],
        "chart_dpi": 144,
        "overwrite": "overwrite",          # overwrite | skip | timestamp
        "csv_encoding": "utf-8-sig",
        "log_level": "INFO",
    },

    # ================= D. 画布与整体版式 =================
    "figure": {
        "figsize": [12.0, 6.0],
        "dpi": 144,
        "facecolor": "#ebf1f5",
        "axes_facecolor": "auto",           # auto = 由 facecolor 向白色混合派生；也可给具体色值
        "edgecolor": "none",
        "layout": "tight",                 # tight | constrained | none
        "subplots_adjust": {"left": None, "right": None, "top": None,
                            "bottom": None, "hspace": None, "wspace": None},
        "font_family": ["Microsoft YaHei", "SimHei", "DejaVu Sans"],
        "font_size": 11,
        "font_weight": "normal",
        "axes_unicode_minus": True,
        "svg_fonttype": "none",             # none = SVG 文字保留为 <text>（可编辑）；path = 转轮廓路径
        "pdf_fonttype": 42,                 # 42 = TrueType 内嵌（可选中/可编辑）；3 = Type 3
        "title": {
            "show": True,
            "text": "{city} 气候统计",
            "fontsize": 16,
            "color": "#2c3e50",
            "fontweight": "bold",
            "pad": 14,
            "loc": "center",
        },
        "subtitle": {
            "show": False,
            "text": "{member} · {period}",
            "fontsize": 10.5,
            "color": "#596679",
            "fontweight": "normal",
            "pad": 6,
            "loc": "center",
        },
        "legend": {
            "show": True,
            "loc": "upper center",
            "bbox_to_anchor": [0.5, -0.12],
            "ncol": 0,                     # 0 = 按元素数量自动
            "fontsize": 10,
            "frameon": True,
            "framealpha": 0.9,
            "facecolor": "#ffffff",
            "edgecolor": "#c9d3dd",
            "title": "",
            "markerscale": 1.0,
            "columnspacing": 1.6,
            "handlelength": 2.0,
            "handletextpad": 0.6,
        },
        "grid": {
            "show": True,
            "axis": "y",                    # x | y | both
            "which": "major",               # major | minor | both
            "color": "#c9d3dd",
            "linestyle": "--",
            "linewidth": 0.7,
            "alpha": 0.75,
            "zorder": 0,
        },
        "background": {
            "bands": {
                "show": False,
                "mode": "season",           # season | alternate
                "hemisphere": "auto",       # auto | north | south（auto：纬度 < 0 视为南半球）
                "alternate_color": "#8fa8c0",
                "alternate_alpha": 0.06,
                "alpha": 0.07,
                "seasons": [
                    {"name": "冬", "months": [12, 1, 2], "color": "#4a6fa5"},
                    {"name": "春", "months": [3, 4, 5], "color": "#6aa84f"},
                    {"name": "夏", "months": [6, 7, 8], "color": "#e69138"},
                    {"name": "秋", "months": [9, 10, 11], "color": "#a64d79"},
                ],
            },
        },
        "zeroline": {
            "show": False,
            "color": "#8899aa",
            "linestyle": "-",
            "linewidth": 0.8,
            "alpha": 0.8,
        },
        "mean_rain_line": {                 # 平均降水线：12 个月降水量的平均水平线
            "show": False,                  # 默认不画，需显式置 true
            "axis": "auto",                 # auto = 跟随 rainfall 元素所在的轴
            "color": "auto",                # auto = 跟随 rainfall 元素的颜色
            "linestyle": "--",
            "linewidth": 1.4,
            "alpha": 1.0,
            "zorder": 3,                    # 高于降水柱（2），不会被柱子盖住
            "label": "平均降水",             # 空字符串 = 不进图例
            "annotate": True,               # 是否在线上标注平均值
            "annotate_template": "{label} {value:.1f} {unit}",
            "annotate_color": "auto",       # auto = 沿用线色
            "annotate_position": "right",    # left | center | right | ticks（ticks = 贴该轴刻度标签列）
            "annotate_side": "above",        # above | below | center（贴线上方/下方/垂直居中于线）
            "annotate_offset": [0.0, 4.0],   # 点偏移 [dx, dy]：dx 右为正；dy 取非负，方向由 side 决定
            "annotate_fontsize": 9.0,
        },
        "annotation": {
            "show_extremes": False,         # 标注最高/最低月
            "series": "meanTemp",
            "fontsize": 9,
            "color": "#a32d2d",
            "show_value": True,
            "avoid_overlap": True,          # 自动避让曲线/另一标注/平均降水线
            "gap": 2.0,                     # 碰撞判定的安全间隙（点）
            "max_distance": 52.0,           # 外推搜索的最远距离（点）
            "allow_flip": True,             # 允许翻到数据点另一侧
        },
        "credit": {
            "show": False,
            "text": "数据来源：世界天气信息服务网（WMO）",
            "fontsize": 8.5,
            "color": "#7a8899",
            "loc": "right",
            "pad": 6,
        },
    },

    # ================= E. 轴 =================
    "axes_primary": {
        "show": True,
        "side": "left",
        "label_text": "温度 (°C)",
        "label_position": "auto",           # auto | left | center | right
        "label_x": None,                    # 覆盖 label_position 的横坐标
        "label_y": 0.5,
        "label_fontsize": 12,
        "label_color": "#2c3e50",
        "label_fontweight": "bold",
        "label_rotation": 90,               # 0 | 90 | 270 | vertical
        "label_align": "auto",              # auto | left | center | right（标题水平对齐）
        "label_valign": "auto",             # auto | top | center | bottom（标题垂直基准）
        "label_pad": 10,
        "limit": [],                        # [] = 自动；否则 [min, max]
        "auto_pad_ratio": 0.14,
        "tick_start": None,
        "tick_step": None,
        "tick_count": None,
        "tick_round_to": None,
        "tick_format": "{:.0f}",
        "tick_fontsize": 10,
        "tick_color": "#5f666e",
        "tick_length": 4,
        "tick_width": 0.8,
        "tick_direction": "out",            # in | out | inout
        "scale": "linear",                  # linear | log
        "spine_show": True,
        "spine_color": "#c9d3dd",
        "spine_linewidth": 1.0,
        "hide_top_spine": True,
    },
    "axes_secondary": {
        "show": True,
        "side": "right",
        "label_text": "降水 (毫米)",
        "label_position": "auto",
        "label_x": None,
        "label_y": 0.5,
        "label_fontsize": 12,
        "label_color": "#2c3e50",
        "label_fontweight": "bold",
        "label_rotation": 270,
        "label_align": "auto",
        "label_valign": "auto",
        "label_pad": 12,
        "limit": [],
        "auto_pad_ratio": 0.18,
        "tick_start": 0,
        "tick_step": None,
        "tick_count": None,
        "tick_round_to": None,
        "tick_format": "{:.0f}",
        "tick_fontsize": 10,
        "tick_color": "#5f666e",
        "tick_length": 4,
        "tick_width": 0.8,
        "tick_direction": "out",
        "scale": "linear",
        "spine_show": True,
        "spine_color": "#c9d3dd",
        "spine_linewidth": 1.0,
        "hide_top_spine": True,
    },
    "axes_tertiary": {
        # 第三轴：用于量级差异大的元素（如"降水日数"与"降水量"）并置显示
        "show": True,
        "side": "right",
        "offset_points": 62,                # 相对右轴再向外偏移的点数
        "label_text": "降水日数 (天)",
        "label_position": "auto",
        "label_x": None,
        "label_y": 0.5,
        "label_fontsize": 12,
        "label_color": "#2c3e50",
        "label_fontweight": "bold",
        "label_rotation": 270,
        "label_align": "auto",
        "label_valign": "auto",
        "label_pad": 12,
        "limit": [],
        "auto_pad_ratio": 0.18,
        "tick_start": 0,
        "tick_step": None,
        "tick_count": None,
        "tick_round_to": None,
        "tick_format": "{:.0f}",
        "tick_fontsize": 10,
        "tick_color": "#5f666e",
        "tick_length": 4,
        "tick_width": 0.8,
        "tick_direction": "out",
        "scale": "linear",
        "spine_show": True,
        "spine_color": "#c9d3dd",
        "spine_linewidth": 1.0,
        "hide_top_spine": True,
    },
    "axes_x": {
        "show": True,
        "label_fontsize": 12,
        "label_color": "#2c3e50",
        "label_fontweight": "bold",
        "label_pad": 8,
        "tick_rotation": 0,
        "tick_fontsize": 10,
        "tick_color": "#5f666e",
        "tick_interval": 1,                 # 隔 N 个月显示一个标签
        "tick_length": 4,
        "tick_width": 0.8,
        "tick_direction": "out",
        "show_month_gridline": False,
        "grid_color": "#c9d3dd",
        "grid_alpha": 0.6,
        "show_spine": True,
        "spine_color": "#c9d3dd",
        "spine_linewidth": 1.0,
        "limit_pad": 0.5,                   # 左右留出半个月的空间
    },

    # ================= F. 绘图元素 =================
    "series": SERIES_DEFAULTS,

    # ================= G. 表格 =================
    "table": {
        "rows": ["minTemp", "maxTemp", "meanTemp", "rainfall", "raindays"],
        "row_labels": {                     # 留空则用元素默认名
            "minTemp": "日均最低气温",
            "maxTemp": "日均最高气温",
            "meanTemp": "日均气温",
            "rainfall": "auto",
            "raindays": "平均降水日数",
        },
        "transpose": False,                 # False: 行=元素, 列=月份
        "precision": {"temp": 1, "rainfall": 1, "raindays": 1, "annual": 1},
        "missing_placeholder": "—",
        "unit_in_label": True,              # 标签是否带单位，如"日均最低气温 (°C)"
        "include_annual": False,            # 是否加"年"列（温度取均值、降水取累加）
        "annual_label": "年",
        "show_period_note": True,
        "show_source_note": True,
        "show_title_row": True,             # CSV/XLSX 顶部是否带标题行
        "title_template": "{city} 气候统计（cityId {city_id}）",
        "note_template": "统计时段：{period}。气温单位 {temp_unit}，降水单位 {rain_unit}。",
        "xlsx": {
            "sheet_name": "气候数据",
            "freeze_header": True,
            "header_fill": "FF0EBFA2",
            "header_font_color": "FFFFFFFF",
            "zebra_fill": "FFF3F7FA",
            "column_width": 13,
            "first_column_width": 20,
            "border_color": "FFBCC9D4",
            "number_format": "0.0",
        },
    },

    # ================= H. 多城市对比图 =================
    "compare": {
        "metric": "meanTemp",               # 对比元素：minTemp|maxTemp|meanTemp|rainfall|raindays
        "chart_type": "smooth",             # 对比曲线类型（rainfall 时可选 bar）
        "figsize": [12.0, 6.0],
        "title_text": "{metric}对比 · {city}",
        "colors": [                         # 按顺序分配给城市；不足则循环或自动生成
            "#d85a30", "#185fa5", "#3b6d11", "#993556",
            "#854f0b", "#0f6e56", "#534ab7", "#a32d2d",
        ],
        "color_by": "order",                # order | city_id
        "linestyle_cycle": ["-", "--", "-.", ":"],
        "marker_cycle": ["o", "s", "^", "D", "v", "P", "X", "*"],
        "linewidth": 2.2,
        "markersize": 5.0,
        "alpha": 1.0,
        "bar_width": 0.8,
        "sort_by": "value_desc",            # none | value_desc | value_asc | name | city_id
        "show_value_range": True,           # 图例中显示该城市该元素的范围
        "label_template": "{city}",
        "legend_ncol": 0,
        "data_labels": {
            "show": False,
            "fontsize": 8.0,
            "format": "{:.1f}",
            "offset": 4.0,
        },
    },
}


# ---- 深合并 ------------------------------------------------------------

def deep_merge(base: Any, override: Any) -> Any:
    """递归合并：dict 合并，其余类型（含 list）整体替换。"""
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            if key in merged:
                merged[key] = deep_merge(merged[key], value)
            else:
                merged[key] = copy.deepcopy(value)
        return merged
    return copy.deepcopy(override)


def set_by_path(cfg: dict[str, Any], path: str, value: Any) -> None:
    """按点路径写入（不存在则创建）。"""
    parts = [p for p in path.split(".") if p]
    if not parts:
        raise ConfigError("--set 的键不能为空")
    node: Any = cfg
    for part in parts[:-1]:
        if not isinstance(node, dict):
            raise ConfigError(f"--set 路径非法：{path}")
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]
    if not isinstance(node, dict):
        raise ConfigError(f"--set 路径非法：{path}")
    node[parts[-1]] = value


def coerce_value(raw: str) -> Any:
    """把命令行字符串转成合适的 Python 类型（优先按 JSON 解析）。"""
    text = raw.strip()
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in ("null", "none"):
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return raw


# ---- 配置加载 ----------------------------------------------------------

def _load_custom_profiles(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """加载**单个**自定义配置文件，返回 (profiles, styles)。

    推荐结构（与 `profiles.json` 一致，`profiles` 下可放任意多个自定义配置）：

    ```jsonc
    {
      "styles":   { "my_style": { ... } },      // 可选，命名样式
      "profiles": { "简图": { ... }, "投屏": { ... } }
    }
    ```

    同时兼容 `--init-profile` 导出的**单配置全量模板**（整个文件即一个配置主体，
    配置名取 `"name"` 字段或文件名）；其中的 `profile_name` / `profile_description`
    属运行时字段，加载时忽略。文件不存在则视为无自定义配置。
    """
    if not path.exists():
        return {}, {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise ConfigError(f"自定义配置不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise ConfigError(f"自定义配置顶层必须是对象：{path}")

    if isinstance(payload.get("profiles"), dict):
        profiles = {str(k): v for k, v in payload["profiles"].items()}
        styles = payload.get("styles")
        return profiles, (dict(styles) if isinstance(styles, dict) else {})

    body = dict(payload)
    name = str(body.pop("name", "") or path.stem)
    body.pop("profile_name", None)
    body.pop("profile_description", None)
    file_styles = body.pop("styles", None)
    styles = dict(file_styles) if isinstance(file_styles, dict) else {}
    return {name: body}, styles


def _custom_config_sources() -> list[Path]:
    """返回有序的自定义配置文件列表（按"上下顺序"加载）。

    顺序：
    1. ``config/custom.json``（单文件，兼容旧用法；若存在）
    2. ``config/custom/*.json``（多文件目录，按文件名升序；忽略隐藏文件与子目录）

    靠后者定义的同名 profile / style 覆盖靠前者；任意文件中的 profile 均可通过
    ``extends`` 引用在更早文件（或内置、或外部文件）中定义的同名配置。
    """
    sources: list[Path] = []
    if CUSTOM_PROFILES_PATH.is_file():
        sources.append(CUSTOM_PROFILES_PATH)
    if CUSTOM_PROFILES_DIR.is_dir():
        sources.extend(
            p for p in sorted(CUSTOM_PROFILES_DIR.glob("*.json"))
            if p.is_file() and not p.name.startswith(".")
        )
    return sources


def load_profiles_file(path: Optional[Path] = None) -> dict[str, Any]:
    """读取内置/外部 profiles 文件，并合并全部自定义配置文件。

    合并顺序：内置或外部 ``profiles.json`` → 各自定义配置文件（按 ``_custom_config_sources`` 的顺序）。
    同名 profile / style 以**靠后**的自定义文件为准；来源文件缺失时忽略该来源。
    """
    target = Path(path) if path else DEFAULT_PROFILES_PATH
    payload: dict[str, Any] = {}
    if target.exists():
        try:
            payload = json.loads(target.read_text(encoding="utf-8-sig"))
        except ValueError as exc:
            raise ConfigError(f"配置文件不是合法 JSON：{target}（{exc}）") from exc
        if not isinstance(payload, dict):
            raise ConfigError(f"配置文件顶层必须是对象：{target}")

    payload.setdefault("styles", {})
    payload.setdefault("profiles", {})

    for src in _custom_config_sources():
        custom_profiles, custom_styles = _load_custom_profiles(src)
        if custom_styles:
            payload["styles"].update(custom_styles)
        if custom_profiles:
            payload["profiles"].update(custom_profiles)
    return payload


def list_profiles(profiles_doc: dict[str, Any]) -> list[tuple[str, str]]:
    """返回 [(名称, 说明), ...]，内置 default 始终可用。"""
    names = {"default": "内置默认：官方页面风格（最低/最高气温 + 降水柱）"}
    for name, prof in (profiles_doc.get("profiles") or {}).items():
        desc = prof.get("description", "") if isinstance(prof, dict) else ""
        names[name] = desc
    return sorted(names.items(), key=lambda kv: (kv[0] != "default", kv[0]))


def _normalize_series_keys(node: dict[str, Any]) -> dict[str, Any]:
    """把 series 下的别名键规范化为规范键（minTempC → minTemp）。"""
    series = node.get("series")
    if not isinstance(series, dict):
        return node
    merged: dict[str, Any] = {}
    for key, value in series.items():
        try:
            canon = normalize_series_key(key)
        except KeyError as exc:
            raise ConfigError(f"series 下存在无法识别的元素键：{key}") from exc
        merged[canon] = deep_merge(merged.get(canon, {}), value) if canon in merged else value
    result = dict(node)
    result["series"] = merged
    return result


def resolve_profile(
    name: Optional[str],
    profiles_doc: dict[str, Any],
    overrides: Optional[list[tuple[str, Any]]] = None,
) -> dict[str, Any]:
    """解析出最终配置：默认值 → 样式 → profiles 继承链 → 命令行覆盖。"""
    profiles = profiles_doc.get("profiles") or {}
    styles = profiles_doc.get("styles") or {}
    selected = name or profiles_doc.get("default_profile") or "default"

    cfg = copy.deepcopy(DEFAULTS)

    if selected == "default" and selected not in profiles:
        cfg["profile_name"] = "default"
        cfg["profile_description"] = "内置默认：官方页面风格（最低/最高气温 + 降水柱）"
    else:
        chain: list[dict[str, Any]] = []
        seen: list[str] = []
        cursor: Optional[str] = selected
        while cursor:
            if cursor in seen:
                raise ConfigError(f"配置继承出现循环：{' -> '.join(seen + [cursor])}")
            if cursor == "default" and cursor not in profiles:
                break
            prof = profiles.get(cursor)
            if prof is None:
                available = ", ".join(n for n, _ in list_profiles(profiles_doc))
                raise ConfigError(f"未找到配置「{cursor}」。可用配置：{available}")
            if not isinstance(prof, dict):
                raise ConfigError(f"配置「{cursor}」必须是对象")
            seen.append(cursor)
            chain.append(prof)
            nxt = prof.get("extends")
            cursor = nxt if isinstance(nxt, str) and nxt else None

        for prof in reversed(chain):
            # 先应用命名样式
            for style_name in prof.get("apply_styles") or []:
                style = styles.get(style_name)
                if style is None:
                    raise ConfigError(
                        f"配置「{selected}」引用了不存在的样式「{style_name}」。"
                        f"可用样式：{', '.join(styles) or '（无）'}"
                    )
                if not isinstance(style, dict):
                    raise ConfigError(f"样式「{style_name}」必须是对象")
                cfg = deep_merge(cfg, _normalize_series_keys(copy.deepcopy(style)))
            payload = {k: v for k, v in prof.items()
                       if k not in ("description", "extends", "apply_styles")}
            cfg = deep_merge(cfg, _normalize_series_keys(copy.deepcopy(payload)))

        cfg["profile_name"] = selected
        cfg["profile_description"] = (profiles.get(selected) or {}).get("description", "")

    for path, value in overrides or []:
        if path == "series":
            continue
        set_by_path(cfg, path, value)

    cfg = _normalize_series_keys(cfg)
    cfg["_profiles_doc"] = profiles_doc
    cfg["_available_profiles"] = [n for n, _ in list_profiles(profiles_doc)]
    return cfg


def load_config(
    profile: Optional[str] = None,
    profiles_path: Optional[Path] = None,
    overrides: Optional[list[tuple[str, Any]]] = None,
) -> dict[str, Any]:
    """一站式加载配置。"""
    doc = load_profiles_file(profiles_path)
    return resolve_profile(profile, doc, overrides)


# ---- 校验 --------------------------------------------------------------

def _walk_keys(schema: Any, actual: Any, prefix: str = "") -> list[str]:
    """找出实际配置里 schema 未定义的键。"""
    problems: list[str] = []
    if not isinstance(schema, dict) or not isinstance(actual, dict):
        return problems
    for key, value in actual.items():
        if key.startswith("_"):
            continue
        path = f"{prefix}.{key}" if prefix else key
        if key not in schema:
            # series 下的元素键已规范化，跳过别名噪音
            problems.append(path)
            continue
        problems.extend(_walk_keys(schema[key], value, path))
    return problems


def validate_config(cfg: dict[str, Any]) -> list[str]:
    """返回警告列表；发现致命问题时抛 ConfigError。"""
    warnings: list[str] = []

    # 1) 未知键（提示，不致命）
    unknown = _walk_keys(DEFAULTS, cfg)
    if unknown:
        warnings.append("以下配置项不是已知项，已被忽略（请核对拼写）：" + ", ".join(sorted(unknown)[:20]))

    # 2) 元素配置
    series = cfg.get("series") or {}
    if not isinstance(series, dict):
        raise ConfigError("series 必须是对象")
    enabled_primary = [k for k, v in series.items()
                       if isinstance(v, dict) and v.get("enabled") and v.get("axis") == "primary"]
    enabled_secondary = [k for k, v in series.items()
                         if isinstance(v, dict) and v.get("enabled") and v.get("axis") == "secondary"]
    if not enabled_primary and not enabled_secondary:
        raise ConfigError("配置中没有启用任何绘图元素（series 下全部 enabled=false）")
    if enabled_secondary and not cfg["axes_secondary"].get("show"):
        warnings.append("有元素使用副轴（axis=secondary），但 axes_secondary.show=false，副轴元素将不被绘制")

    # 3) 单位一致性
    temp_unit = (cfg["data"].get("temp_unit") or "C").upper()
    if temp_unit not in ("C", "F"):
        raise ConfigError("data.temp_unit 只能是 C 或 F")
    rain_unit = (cfg["data"].get("rain_unit") or "mm").lower()
    if rain_unit not in ("mm", "inch"):
        raise ConfigError("data.rain_unit 只能是 mm 或 inch")

    # 4) 表格行
    for key in cfg["table"].get("rows") or []:
        try:
            normalize_series_key(key)
        except KeyError as exc:
            raise ConfigError(f"table.rows 中存在无法识别的元素键：{key}") from exc

    # 5) 输出格式
    table_fmts = [f.lower() for f in cfg["output"].get("table_formats") or []]
    for fmt in table_fmts:
        if fmt not in ("csv", "md", "markdown", "xlsx", "json"):
            raise ConfigError(f"不支持的表格格式：{fmt}（可选 csv/md/xlsx/json）")
    chart_fmts = [f.lower() for f in cfg["output"].get("chart_formats") or []]
    for fmt in chart_fmts:
        if fmt not in ("png", "svg", "pdf", "jpg", "jpeg", "webp"):
            raise ConfigError(f"不支持的图片格式：{fmt}")

    # 6) 布局与排版
    if cfg["figure"].get("layout") not in ("tight", "constrained", "none"):
        raise ConfigError("figure.layout 只能是 tight / constrained / none")
    if str(cfg["figure"].get("svg_fonttype", "none")).lower() not in ("none", "path"):
        raise ConfigError("figure.svg_fonttype 只能是 none（文字元素）或 path（轮廓路径）")
    try:
        pdf_fonttype = int(cfg["figure"].get("pdf_fonttype", 42))
    except (TypeError, ValueError):
        pdf_fonttype = -1
    if pdf_fonttype not in (3, 42):
        raise ConfigError("figure.pdf_fonttype 只能是 42（TrueType）或 3（Type 3）")

    bands = (cfg["figure"].get("background") or {}).get("bands") or {}
    hemisphere = str(bands.get("hemisphere", "auto")).lower()
    if hemisphere not in ("auto", "north", "south"):
        raise ConfigError("figure.background.bands.hemisphere 只能是 auto / north / south"
                          f"（当前：{bands.get('hemisphere')}）")

    line_axis = str((cfg["figure"].get("mean_rain_line") or {}).get("axis", "auto")).lower()
    if line_axis not in ("auto", "primary", "secondary", "tertiary"):
        raise ConfigError("figure.mean_rain_line.axis 只能是 auto / primary / secondary / tertiary"
                          f"（当前：{(cfg['figure'].get('mean_rain_line') or {}).get('axis')}）")

    line_cfg = cfg["figure"].get("mean_rain_line") or {}
    line_pos = str(line_cfg.get("annotate_position", "right")).lower()
    if line_pos not in ("left", "center", "right", "ticks"):
        raise ConfigError("figure.mean_rain_line.annotate_position 只能是 left / center / right / ticks"
                          f"（当前：{line_cfg.get('annotate_position')}）")
    line_side = str(line_cfg.get("annotate_side", "above")).lower()
    if line_side not in ("above", "below", "center"):
        raise ConfigError("figure.mean_rain_line.annotate_side 只能是 above / below / center"
                          f"（当前：{line_cfg.get('annotate_side')}）")

    return warnings


def dump_config(cfg: dict[str, Any]) -> str:
    """输出可读的最终配置（剔除内部键）。"""
    clean = {k: v for k, v in cfg.items() if not k.startswith("_")}
    return json.dumps(clean, ensure_ascii=False, indent=2)


def write_template(path: Path, cfg: Optional[dict[str, Any]] = None) -> Path:
    """导出一份全量配置模板（含全部可配置项）。"""
    payload = copy.deepcopy(cfg if cfg is not None else DEFAULTS)
    payload = {k: v for k, v in payload.items() if not k.startswith("_")}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
