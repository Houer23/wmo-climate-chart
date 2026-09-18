"""绘图层：matplotlib 双轴气温-降水统计图，全部绘图参数由配置驱动。

无 scipy 依赖：平滑曲线用自实现的三次 Hermite 插值（Catmull-Rom 风格）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.text import Text  # noqa: E402
from matplotlib.ticker import FuncFormatter, MaxNLocator  # noqa: E402
from matplotlib.transforms import Bbox  # noqa: E402

from .models import CityClimate, format_coord, normalize_series_key  # noqa: E402

TEMP_KEYS = ("minTemp", "maxTemp", "meanTemp")
RAIN_KEYS = ("rainfall", "raindays")
AXIS_KEYS = ("primary", "secondary", "tertiary")

# 绘制顺序：柱状类先画（打底），折线/曲线类后画（压在上层）。
# 图例顺序不受影响，仍按 series.order_in_legend。
BAR_CHART_TYPES = {"bar", "barh"}

# 极值标注的基准偏移（点）：最高向上、最低向下；自动避让在此基准上按候选序列外推
EXTREME_LABEL_OFFSETS = {"最高": 16.0, "最低": -22.0}


def _draw_priority(scfg: dict[str, Any]) -> int:
    """绘图顺序优先级：柱状 = 0（先画），其余 = 1（后画）。"""
    return 0 if str(scfg.get("chart_type", "line")).lower() in BAR_CHART_TYPES else 1


class ChartError(RuntimeError):
    """绘图失败。"""


# ---- 工具 --------------------------------------------------------------

def _pick(value: Any, fallback: str) -> str:
    """支持 "auto" 占位：auto 时用 fallback。"""
    if value in (None, "", "auto"):
        return fallback
    return str(value)


def _blend(color: str, toward: str = "#ffffff", ratio: float = 0.55) -> str:
    """把颜色朝目标色混合，用于由画布底色派生绘图区底色。"""
    def to_rgb(c: str) -> tuple[float, float, float]:
        c = c.lstrip("#")
        if len(c) == 3:
            c = "".join(ch * 2 for ch in c)
        return tuple(int(c[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]

    try:
        a, b = to_rgb(color), to_rgb(toward)
    except (ValueError, IndexError):
        return toward
    mixed = tuple(a[i] + (b[i] - a[i]) * ratio for i in range(3))
    return "#" + "".join(f"{int(round(v * 255)):02x}" for v in mixed)


def _hermite_smooth(x: Sequence[float], y: Sequence[float], points: int = 240
                    ) -> tuple[np.ndarray, np.ndarray]:
    """三次 Hermite 插值（无需 scipy）。自动跳过缺失点。"""
    xs = np.asarray(x, dtype=float)
    ys = np.asarray(y, dtype=float)
    valid = ~np.isnan(ys)
    if valid.sum() < 2:
        return xs, ys
    xv, yv = xs[valid], ys[valid]
    dense = max(int(points), len(xv) * 2)
    xi = np.linspace(xv.min(), xv.max(), dense)

    slopes = np.empty_like(yv)
    slopes[1:-1] = (yv[2:] - yv[:-2]) / (xv[2:] - xv[:-2])
    slopes[0] = (yv[1] - yv[0]) / (xv[1] - xv[0])
    slopes[-1] = (yv[-1] - yv[-2]) / (xv[-1] - xv[-2])

    idx = np.clip(np.searchsorted(xv, xi) - 1, 0, len(xv) - 2)
    h = xv[idx + 1] - xv[idx]
    h = np.where(h == 0, 1e-9, h)
    t = (xi - xv[idx]) / h
    h00 = 2 * t ** 3 - 3 * t ** 2 + 1
    h10 = t ** 3 - 2 * t ** 2 + t
    h01 = -2 * t ** 3 + 3 * t ** 2
    h11 = t ** 3 - t ** 2
    yi = h00 * yv[idx] + h10 * h * slopes[idx] + h01 * yv[idx + 1] + h11 * h * slopes[idx + 1]
    return xi, yi


def _missing_policy(cfg: dict[str, Any]) -> str:
    return str(cfg["data"].get("fill_missing", "gap")).lower()


def _to_array(values: Sequence[Optional[float]]) -> np.ndarray:
    return np.array([np.nan if v is None else float(v) for v in values], dtype=float)


def _apply_missing(arr: np.ndarray, policy: str) -> np.ndarray:
    if policy == "zero":
        return np.nan_to_num(arr, nan=0.0)
    return arr


# ---- 全局样式 ----------------------------------------------------------

def setup_style(cfg: dict[str, Any]) -> None:
    """把字体、字号等写入 matplotlib rcParams。中文防方框的关键。"""
    # 部分中文字体（如 SimSun）没有 bold 字重，matplotlib 会打印回落提示；
    # 这是预期行为，降噪以免污染日志。
    import logging

    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)

    fig_cfg = cfg["figure"]
    families = list(fig_cfg.get("font_family") or [])
    for fallback in ("Microsoft YaHei", "SimHei", "SimSun", "DejaVu Sans"):
        if fallback not in families:
            families.append(fallback)
    plt.rcParams["font.sans-serif"] = families
    plt.rcParams["font.family"] = "sans-serif"
    plt.rcParams["font.size"] = float(fig_cfg.get("font_size", 11))
    plt.rcParams["font.weight"] = str(fig_cfg.get("font_weight", "normal"))
    plt.rcParams["axes.unicode_minus"] = bool(fig_cfg.get("axes_unicode_minus", True))
    # 矢量文本：svg / pdf 里的文字保持为「可编辑的文本元素」，而非轮廓路径
    #   svg_fonttype: none = 输出 <text>（可用 AI / Inkscape 直接改字）；path = 转轮廓（外观绝对一致但不可编辑）
    #   pdf_fonttype: 42  = TrueType 内嵌，文字可选中 / 可检索 / 可编辑；3 = Type 3（部分工具不可编辑）
    plt.rcParams["svg.fonttype"] = str(fig_cfg.get("svg_fonttype", "none")).lower()
    plt.rcParams["pdf.fonttype"] = int(fig_cfg.get("pdf_fonttype", 42))
    plt.rcParams["axes.facecolor"] = _resolve_axes_facecolor(cfg)
    plt.rcParams["savefig.facecolor"] = str(fig_cfg.get("facecolor", "#ffffff"))
    plt.rcParams["figure.autolayout"] = False


def _resolve_axes_facecolor(cfg: dict[str, Any]) -> str:
    value = cfg["figure"].get("axes_facecolor", "auto")
    if value in (None, "", "auto"):
        return _blend(str(cfg["figure"].get("facecolor", "#ffffff")), "#ffffff", 0.55)
    return str(value)


# ---- 元素解析 ----------------------------------------------------------

def collect_series(city: CityClimate, cfg: dict[str, Any], logger=None
                   ) -> list[tuple[str, dict[str, Any], np.ndarray]]:
    """筛出实际要绘制的元素：(规范键, 元素配置, 数值数组)。"""
    temp_unit = (cfg["data"].get("temp_unit") or "C").upper()
    rain_unit = (cfg["data"].get("rain_unit") or "mm").lower()
    auto_disable = bool(cfg["data"].get("auto_disable_empty_series", True))
    axis_ok = {a: bool((cfg.get("axes_" + a) or {}).get("show", True)) for a in AXIS_KEYS}

    result: list[tuple[str, dict[str, Any], np.ndarray]] = []
    for key, scfg in (cfg.get("series") or {}).items():
        if not isinstance(scfg, dict) or not scfg.get("enabled", True):
            continue
        axis = str(scfg.get("axis", "primary")).lower()
        if axis not in AXIS_KEYS:
            if logger:
                logger.warning(f"元素 {key} 的 axis 取值非法（{axis}），已按 primary 处理")
            axis = "primary"
        raw = city.values(key, temp_unit, rain_unit)
        all_missing = all(v is None for v in raw)
        if all_missing and auto_disable:
            if logger:
                logger.info(f"元素 {key} 在 {city.city_name} 无任何数据，已自动跳过")
            continue
        if axis != "primary" and not axis_ok.get(axis, True):
            if logger:
                logger.warning(f"元素 {key} 使用 {axis} 轴，但该轴已关闭，已跳过")
            continue
        arr = _apply_missing(_to_array(raw), _missing_policy(cfg))
        scale = float(scfg.get("scale_factor", 1.0) or 1.0)
        if scale != 1.0:
            arr = arr * scale
        result.append((normalize_series_key(key), scfg, arr))
    result.sort(key=lambda item: float(item[1].get("order_in_legend", 10)))
    return result


def _series_label(city: CityClimate, key: str, scfg: dict[str, Any]) -> str:
    label = scfg.get("label", "auto")
    if label in (None, "", "auto"):
        key = normalize_series_key(key)
        if key == "rainfall":
            return city.rain_label(sentence=True)
        return {"minTemp": "日均最低气温", "maxTemp": "日均最高气温",
                "meanTemp": "日均气温", "raindays": "平均降水日数"}.get(key, key)
    return str(label)


# ---- 坐标轴 ------------------------------------------------------------

def _y_label_position(ax, ax_cfg: dict[str, Any], default_x: float) -> None:
    """纵轴标题位置：沿轴方向 bottom/center/top，或横向 left/right，或显式坐标。"""
    x = ax_cfg.get("label_x")
    y = ax_cfg.get("label_y")
    pos = str(ax_cfg.get("label_position", "auto")).lower()
    along = {"bottom": 0.14, "center": 0.5, "top": 0.86}
    if pos in along:
        y = along[pos] if y is None else y
    elif pos == "left":
        x = default_x - 0.055 if x is None else x
    elif pos == "right":
        x = default_x + 0.055 if x is None else x
    if x is not None or y is not None:
        ax.yaxis.set_label_coords(
            float(x) if x is not None else default_x,
            float(y) if y is not None else 0.5,
        )


def _configure_y_axis(ax, ax_cfg: dict[str, Any], values: Sequence[np.ndarray],
                      default_x: float, has_bar: bool, cfg: dict[str, Any],
                      logger=None, only_side: bool = False) -> None:
    side = str(ax_cfg.get("side", "left")).lower()
    ax.yaxis.set_ticks_position(side)
    if side == "right":
        ax.yaxis.set_label_position("right")
    else:
        ax.yaxis.set_label_position("left")

    label = str(ax_cfg.get("label_text", "") or "")
    if label:
        label_kwargs: dict[str, Any] = {
            "fontsize": float(ax_cfg.get("label_fontsize", 12)),
            "color": str(ax_cfg.get("label_color", "#2c3e50")),
            "fontweight": str(ax_cfg.get("label_fontweight", "bold")),
            "rotation": ax_cfg.get("label_rotation", 90),
            "labelpad": float(ax_cfg.get("label_pad", 10)),
        }
        # 标题对齐：横排时用 label_align 实现「与刻度标签左/右对齐」，
        # 用 label_valign 固定垂直基准，保证左右两轴标题处于同一行。
        align = str(ax_cfg.get("label_align", "auto")).lower()
        if align in ("left", "center", "right"):
            label_kwargs["ha"] = align
        valign = str(ax_cfg.get("label_valign", "auto")).lower()
        if valign in ("top", "center", "bottom", "baseline", "center_baseline"):
            label_kwargs["va"] = valign
        ax.set_ylabel(label, **label_kwargs)
        _y_label_position(ax, ax_cfg, default_x)

    # 量程
    finite = [arr[np.isfinite(arr)] for arr in values]
    finite = [a for a in finite if a.size]
    limit = list(ax_cfg.get("limit") or [])
    if len(limit) == 2 and limit[0] is not None and limit[1] is not None:
        lo, hi = float(limit[0]), float(limit[1])
    else:
        if finite:
            lo = float(min(a.min() for a in finite))
            hi = float(max(a.max() for a in finite))
        else:
            lo, hi = 0.0, 1.0
        if hi < lo:
            lo, hi = hi, lo
        span = hi - lo
        if span <= 0:
            span = max(abs(hi), 1.0)
        pad = float(ax_cfg.get("auto_pad_ratio", 0.14) or 0.0)
        if has_bar:
            # 柱状图基线固定在 0：非负数据不向下留白，负值数据保留负向空间
            if lo >= 0:
                lo = 0.0
                hi = hi + span * pad
            else:
                lo = lo - span * pad
                hi = max(hi, 0.0) + span * pad
        else:
            lo -= span * pad
            hi += span * pad
        round_to = ax_cfg.get("tick_round_to")
        if round_to:
            step = float(round_to)
            lo = np.floor(lo / step) * step
            hi = np.ceil(hi / step) * step

    if ax_cfg.get("scale") == "log":
        ax.set_yscale("log")
    ax.set_ylim(lo, hi)

    # 刻度
    step = ax_cfg.get("tick_step")
    start = ax_cfg.get("tick_start")
    if step:
        step = float(step)
        base = lo if start is None else float(start)
        # 让刻度覆盖整个量程
        if base > lo:
            base = base - step * np.ceil((base - lo) / step)
        ticks = np.arange(base, hi + step * 0.5, step)
        if ticks.size > 80:
            if logger:
                logger.warning("tick_step 过小，刻度数量超过 80，已自动忽略该设置")
        else:
            ax.set_yticks(ticks)
    elif ax_cfg.get("tick_count"):
        ax.yaxis.set_major_locator(MaxNLocator(nbins=int(ax_cfg["tick_count"])))

    fmt = str(ax_cfg.get("tick_format", "") or "")
    if fmt:
        def _fmt(value, _pos, _fmt=fmt):
            try:
                return _fmt.format(value)
            except (ValueError, IndexError, KeyError):
                return f"{value:g}"
        ax.yaxis.set_major_formatter(FuncFormatter(_fmt))

    ax.tick_params(
        axis="y",
        which="major",
        labelsize=float(ax_cfg.get("tick_fontsize", 10)),
        labelcolor=str(ax_cfg.get("tick_color", "#5f666e")),
        colors=str(ax_cfg.get("tick_color", "#5f666e")),
        length=float(ax_cfg.get("tick_length", 4)),
        width=float(ax_cfg.get("tick_width", 0.8)),
        direction=str(ax_cfg.get("tick_direction", "out")),
    )

    # 轴脊
    spinner = ax.spines
    show_spine = bool(ax_cfg.get("spine_show", True))
    for name in ("left", "right", "bottom", "top"):
        spinner[name].set_visible(show_spine)
        spinner[name].set_color(str(ax_cfg.get("spine_color", "#c9d3dd")))
        spinner[name].set_linewidth(float(ax_cfg.get("spine_linewidth", 1.0)))
    if show_spine:
        spinner[side].set_visible(True)
        if only_side:
            for name in ("left", "bottom", "top"):
                spinner[name].set_visible(False)
        elif ax_cfg.get("hide_top_spine", True):
            spinner["top"].set_visible(False)


def _configure_x_axis(ax, city: CityClimate, cfg: dict[str, Any]) -> np.ndarray:
    xcfg = cfg["axes_x"]
    x = np.arange(len(city.months), dtype=float)
    labels = city.month_labels(str(cfg["data"].get("month_label_style", "1月")))

    interval = max(1, int(xcfg.get("tick_interval", 1) or 1))
    shown = [label if (i % interval == 0) else "" for i, label in enumerate(labels)]

    ax.set_xticks(x)
    ax.set_xticklabels(
        shown,
        rotation=float(xcfg.get("tick_rotation", 0)),
        fontsize=float(xcfg.get("tick_fontsize", 10)),
        color=str(xcfg.get("tick_color", "#5f666e")),
    )
    ax.tick_params(
        axis="x",
        length=float(xcfg.get("tick_length", 4)),
        width=float(xcfg.get("tick_width", 0.8)),
        direction=str(xcfg.get("tick_direction", "out")),
        colors=str(xcfg.get("tick_color", "#5f666e")),
    )

    if xcfg.get("show", True) and xcfg.get("label_text"):
        ax.set_xlabel(
            str(xcfg["label_text"]),
            fontsize=float(xcfg.get("label_fontsize", 12)),
            color=str(xcfg.get("label_color", "#2c3e50")),
            fontweight=str(xcfg.get("label_fontweight", "bold")),
            labelpad=float(xcfg.get("label_pad", 8)),
        )

    pad = float(xcfg.get("limit_pad", 0.5) or 0.0)
    ax.set_xlim(-pad, len(city.months) - 1 + pad)

    if not xcfg.get("show_spine", True):
        ax.spines["bottom"].set_visible(False)
    else:
        ax.spines["bottom"].set_visible(True)
        ax.spines["bottom"].set_color(str(xcfg.get("spine_color", "#c9d3dd")))
        ax.spines["bottom"].set_linewidth(float(xcfg.get("spine_linewidth", 1.0)))

    if xcfg.get("show_month_gridline", False) and x.size > 1:
        for pos in x[1:-1]:
            ax.axvline(pos, color=str(xcfg.get("grid_color", "#c9d3dd")),
                       alpha=float(xcfg.get("grid_alpha", 0.6)), linewidth=0.6, zorder=0)
    return x


def _season_flip(city: CityClimate, bands: dict[str, Any]) -> bool:
    """季节色带是否需要按南半球把月份平移半年。

    配置里的 ``seasons`` 按**北半球惯例**声明（冬 = 12/1/2 月）。南半球的冬夏、春秋相反，
    因此 ``hemisphere=auto`` 且城市纬度为负时，把季节月份整体平移 6 个月，让每个季节的
    颜色落到当地真实季节上（默认四季即冬夏、春秋互换）。
    纬度缺失或恰为 0 时按北半球处理。
    """
    hemisphere = str(bands.get("hemisphere", "auto")).lower()
    if hemisphere == "south":
        return True
    if hemisphere == "north":
        return False
    latitude = city.latitude
    return latitude is not None and latitude < 0


def _shift_half_year(months: set[int]) -> set[int]:
    """把 1-12 月整体平移半年（南半球反季）；非法月份保持原样，因而不会命中。"""
    return {((m - 1 + 6) % 12) + 1 if 1 <= m <= 12 else m for m in months}


def _draw_background(ax, city: CityClimate, cfg: dict[str, Any]) -> None:
    """绘制月份背景色带。

    ``ax`` 必须是**最底层坐标轴**（由 ``_stack_axes`` 给出），与网格同理：柱状图所在的轴
    可能被压在主/三轴之下，若色带画在含折线（后绘制）的主轴上，半透明色带会整体罩在
    柱子上。各轴与主轴共用同一条 x 轴（``twinx``），因此换轴不影响色带对位。
    """
    bands = (cfg["figure"].get("background") or {}).get("bands") or {}
    if not bands.get("show"):
        return
    mode = str(bands.get("mode", "season"))
    alpha = float(bands.get("alpha", 0.07))
    if mode == "alternate":
        for i in range(len(city.months)):
            if i % 2 == 0:
                ax.axvspan(i - 0.5, i + 0.5,
                           color=str(bands.get("alternate_color", "#8fa8c0")),
                           alpha=float(bands.get("alternate_alpha", 0.06)), zorder=0)
        return
    flip = _season_flip(city, bands)
    for season in bands.get("seasons") or []:
        months = {int(m) for m in (season.get("months") or [])}
        if flip:
            months = _shift_half_year(months)
        for i, mo in enumerate(city.months):
            if mo.month in months:
                ax.axvspan(i - 0.5, i + 0.5, color=str(season.get("color", "#cccccc")),
                           alpha=alpha, zorder=0)


def _draw_zeroline(ax, cfg: dict[str, Any]) -> None:
    zcfg = cfg["figure"].get("zeroline") or {}
    if not zcfg.get("show"):
        return
    ax.axhline(0.0, color=str(zcfg.get("color", "#8899aa")),
               linestyle=str(zcfg.get("linestyle", "-")),
               linewidth=float(zcfg.get("linewidth", 0.8)),
               alpha=float(zcfg.get("alpha", 0.8)), zorder=1)


def _rain_unit_text(city: CityClimate, cfg: dict[str, Any]) -> str:
    """降水单位文案：配置切到英寸时用英寸，否则沿用数据源自带单位（与表格口径一致）。"""
    if (cfg["data"].get("rain_unit") or "mm").lower() == "inch":
        return "英寸"
    return city.rain_unit_label()


def _mean_rainfall(city: CityClimate, cfg: dict[str, Any]) -> Optional[float]:
    """12 个月降水量的平均值（当前降水单位；缺测月不计入；全缺返回 None）。

    与柱状图保持同一刻度空间：套用 ``series.rainfall.scale_factor``。
    """
    rain_unit = (cfg["data"].get("rain_unit") or "mm").lower()
    values = [v for v in city.values("rainfall", rain_unit=rain_unit) if v is not None]
    if not values:
        return None
    scale = float(((cfg.get("series") or {}).get("rainfall") or {}).get("scale_factor", 1.0) or 1.0)
    return sum(values) / len(values) * scale


def _tick_label_column(ax) -> tuple[float, str, float]:
    """量出该轴**刻度标签所在列**：返回 (轴比例锚点, 文字水平对齐, 相对轴边缘的点偏移)。

    刻度标签边缘距轴边缘 = 向外的刻度长度 + ``tick_pad``（默认 4 + 3.5 = 7.5pt），
    与画布尺寸、字号无关；这里用实测值而非固定常量，好让 ``tick_length`` /
    ``tick_direction`` / 刻度左右侧切换后自动跟上。
    """
    on_left = str(ax.yaxis.get_ticks_position()).lower() == "left"
    labels = [t for t in ax.yaxis.get_majorticklabels() if t.get_text().strip()]
    if labels:
        try:
            fig = ax.get_figure()
            renderer = fig.canvas.get_renderer()
            boxes = [t.get_window_extent(renderer) for t in labels]
            axes_box = ax.get_window_extent()
            scale = 72.0 / float(fig.dpi)          # 像素 → 点，与最终输出 dpi 无关
            if on_left:
                return 0.0, "right", (max(b.x1 for b in boxes) - axes_box.x0) * scale
            return 1.0, "left", (min(b.x0 for b in boxes) - axes_box.x1) * scale
        except (AttributeError, IndexError, TypeError, ValueError):
            pass                                    # 取不到渲染器时退回 tick_pad 估算
    pad = 3.5
    ticks = list(getattr(ax.yaxis, "majorTicks", []))
    if ticks:
        try:
            pad = float(ticks[0].get_pad())
        except (AttributeError, TypeError, ValueError):
            pad = 3.5
    return (0.0, "right", -pad) if on_left else (1.0, "left", pad)


def _nudge_off_tick_labels(ax, anno, gap_pt: float = 2.0, passes: int = 3) -> float:
    """让标注避开设在同一侧的纵轴刻度标签，返回实际施加的点偏移（正=向上、负=向下）。

    只有当标注与刻度标签**横向也重叠**时才避让（标注留在绘图区内时天然安全）；
    判定时已预留 ``gap_pt`` 间隙，因此"贴得很近但没压上"也会被让开；
    每次取"刚好离开、且尽量不越出绘图区"的方向，位移量为离开所需最小值 + 该间隙。
    """
    try:
        fig = ax.get_figure()
        renderer = fig.canvas.get_renderer()
        axes_box = ax.get_window_extent()
        scale = 72.0 / float(fig.dpi)          # 像素 → 点，与最终输出 dpi 无关
        boxes = [t.get_window_extent(renderer)
                 for t in ax.yaxis.get_majorticklabels() if t.get_text().strip()]
    except (AttributeError, IndexError, TypeError, ValueError):
        return 0.0
    if not boxes:
        return 0.0

    gap_px = gap_pt * float(fig.dpi) / 72.0
    total = 0.0
    for _ in range(passes):
        try:
            anno.update_positions(renderer)     # 与极值标注同理：先刷新文字变换再量
            box = anno.get_window_extent(renderer)
        except (AttributeError, IndexError, TypeError, ValueError):
            break
        # 纵向按"间隙"放宽判定：贴得比 gap 更近也算撞上，避免出现 0.x px 的贴合观感
        hit = [b for b in boxes
               if b.y1 + gap_px > box.y0 and b.y0 - gap_px < box.y1
               and b.x1 > box.x0 and b.x0 < box.x1]
        if not hit:
            break
        up = max(b.y1 for b in hit) - box.y0 + gap_px
        down = box.y1 - min(b.y0 for b in hit) + gap_px

        def crosses(shift: float) -> bool:
            """位移后是否越出绘图区上下边界。"""
            return box.y0 + shift < axes_box.y0 or box.y1 + shift > axes_box.y1

        shift = min((up, -down), key=lambda s: (crosses(s), abs(s)))
        anno.set_position((anno.xyann[0], anno.xyann[1] + shift * scale))
        total += shift * scale
    return total


def _draw_mean_rain_line(axis_map: dict[str, Any], city: CityClimate,
                         cfg: dict[str, Any]) -> tuple[Optional[Any], Optional[Any]]:
    """平均降水线：把 12 个月降水量取平均，画一条水平参考线。

    默认不绘制（``figure.mean_rain_line.show = false``），需在配置中显式开启。
    画在降水柱所在的轴（``axis=auto`` 跟随 ``series.rainfall.axis``）且层级高于柱子，
    因此不会被柱子盖住；该城市降水全缺或所在轴未启用时静默跳过。

    返回 ``(线, 标注或 None)``：标注在这里只创建、不定最终位置——它的横纵落点都要量取
    坐标区与刻度标签的实际几何，须由 ``_place_mean_rain_annotation`` 在**布局定型后**
    放置（原因见该函数说明）。
    """
    mcfg = cfg["figure"].get("mean_rain_line") or {}
    if not mcfg.get("show"):
        return None, None
    mean = _mean_rainfall(city, cfg)
    if mean is None:
        return None, None

    axis_name = str(mcfg.get("axis", "auto") or "auto").lower()
    if axis_name == "auto":
        axis_name = str(((cfg.get("series") or {}).get("rainfall") or {})
                        .get("axis", "secondary")).lower()
    target = axis_map.get(axis_name)
    if target is None:
        return None, None

    rain_cfg = (cfg.get("series") or {}).get("rainfall") or {}
    color = _pick(mcfg.get("color"), _pick(rain_cfg.get("color"), "#46cbd4"))
    zorder = float(mcfg.get("zorder", 3))
    label = str(mcfg.get("label", "平均降水") or "")
    line = target.axhline(
        mean, color=color,
        linestyle=str(mcfg.get("linestyle", "--")),
        linewidth=float(mcfg.get("linewidth", 1.4)),
        alpha=float(mcfg.get("alpha", 1.0)),
        zorder=zorder, label=label or None,
    )

    anno = None
    if mcfg.get("annotate", True):
        text = str(mcfg.get("annotate_template", "{label} {value:.1f} {unit}")).format(
            label=label, value=mean, unit=_rain_unit_text(city, cfg),
            city=city.city_name, station=city.station_name or city.city_name,
        )
        if text.strip():
            anno = target.annotate(
                text,
                xy=(1.0, mean), xycoords=("axes fraction", "data"),
                xytext=(0.0, 0.0), textcoords="offset points",
                ha="right", va="bottom",
                fontsize=float(mcfg.get("annotate_fontsize", 9.0)),
                color=_pick(mcfg.get("annotate_color"), color),
                zorder=zorder + 1, clip_on=False,
            )
    return line, anno


def _place_mean_rain_annotation(target, anno, cfg: dict[str, Any], mean: float) -> None:
    """按配置把平均降水线标注放到最终位置，并在与同侧刻度标签相撞时自动上下让开。

    **必须在布局定型之后调用**（``_apply_layout`` 之后），与极值标注同理：横向的
    ``ticks`` 落点要量取刻度标签所在列，纵向避让要量取刻度标签与文字盒，二者都随
    坐标区的**最终**大小变化。而 ``tight_layout`` 会显著改变坐标区高度，自动刻度的
    档位也随之改变（实测伦敦 + 横300：布局前 4 档 0/100/200/300，布局后 7 档
    0/50/…/300）。若在布局前量旧几何，会得出"没有碰撞"的结论而不做避让，标注随后
    就压在新出现的刻度标签（如 ``50``）上。
    """
    mcfg = cfg["figure"].get("mean_rain_line") or {}
    # 落点：横向锚到绘图区左/中/右（轴宽比例），或 ``ticks`` 贴到该轴刻度标签那一列；
    # 纵向锚到均值线：``above``/``below`` 把文字底/顶边离开线 dy 点，``center`` 则让
    # 文字垂直中心正落在线上（此时 offset 的纵向分量不生效）。
    position = str(mcfg.get("annotate_position", "right") or "right").lower()
    if position not in ("left", "center", "right", "ticks"):
        position = "right"
    side = str(mcfg.get("annotate_side", "above") or "above").lower()
    below, centered = side == "below", side == "center"
    offset = mcfg.get("annotate_offset") or [0.0, 4.0]
    try:
        dx, dy = float(offset[0]), abs(float(offset[1]))
    except (TypeError, ValueError, IndexError):
        dx, dy = 0.0, 4.0
    if position == "ticks":
        x_frac, ha, dx_column = _tick_label_column(target)
        dx += dx_column              # 横向落到刻度标签列；dx 仍可继续微调
    else:
        x_frac, ha = {"left": (0.0, "left"), "center": (0.5, "center"),
                      "right": (1.0, "right")}[position]
    anno.xy = (x_frac, mean)
    anno.set_ha(ha)
    anno.set_va("center" if centered else ("top" if below else "bottom"))
    anno.set_position((dx, 0.0 if centered else (-dy if below else dy)))
    _nudge_off_tick_labels(target, anno)    # 与刻度标签撞上时自动上下让开


def _stack_axes(cfg: dict[str, Any], axis_map: dict[str, Any],
                by_axis: dict[str, list[tuple[str, dict[str, Any], np.ndarray]]]) -> str:
    """调整各坐标轴的叠放层次，保证**折线始终绘制在柱状图之上**。

    matplotlib 把同一图中的每个坐标轴当作整体按 zorder 依次绘制，``twinx`` 轴按加入
    顺序后绘制，因此「降水柱在副轴」时会整体盖住「主/三轴上的折线」。这里按「该轴是否
    含折线」分层：含折线的轴置于上层；并把底色交给最底层、其余轴透明，避免上层轴遮住
    下层的柱状图。
    """
    present = [a for a in AXIS_KEYS if axis_map.get(a) is not None]
    if not present:
        return "primary"

    def _has_line(axis_key: str) -> bool:
        return any(str(s.get("chart_type", "line")).lower() not in BAR_CHART_TYPES
                   for _key, s, _arr in by_axis.get(axis_key) or [])

    for axis_key in present:
        axis_map[axis_key].set_zorder(1 if _has_line(axis_key) else 0)

    # 最先绘制的轴负责绘制底色，其余轴透明（否则上层轴的底色会遮住下层内容）
    bottom = min(present, key=lambda a: (axis_map[a].get_zorder(), AXIS_KEYS.index(a)))
    for axis_key in present:
        axis_map[axis_key].patch.set_visible(axis_key == bottom)
    axis_map[bottom].set_facecolor(_resolve_axes_facecolor(cfg))
    return bottom


def _draw_grid(grid_owner, axis_map: dict[str, Any], bottom_key: str,
               grid_cfg: dict[str, Any]) -> None:
    """绘制网格。

    柱状图位于下层时，网格改画在**最底层坐标轴**上（位置仍与 `grid_owner` 的刻度对齐），
    这样网格既不会压住柱状图，也不会压住折线。
    """
    bottom = axis_map[bottom_key]
    axis = str(grid_cfg.get("axis", "y"))
    which = str(grid_cfg.get("which", "major"))
    style = {
        "color": str(grid_cfg.get("color", "#c9d3dd")),
        "linestyle": str(grid_cfg.get("linestyle", "--")),
        "linewidth": float(grid_cfg.get("linewidth", 0.7)),
        "alpha": float(grid_cfg.get("alpha", 0.75)),
        "zorder": float(grid_cfg.get("zorder", 0)),
    }

    if bottom is grid_owner:
        bottom.grid(True, axis=axis, which=which, **style)
        return

    if axis in ("y", "both"):
        lo, hi = grid_owner.get_ylim()
        blo, bhi = bottom.get_ylim()
        span = (hi - lo) or 1.0
        ticks: list[float] = []
        if which in ("major", "both"):
            ticks += list(grid_owner.get_yticks())
        if which in ("minor", "both"):
            ticks += list(grid_owner.get_yticks(minor=True))
        for value in ticks:
            if not np.isfinite(value):
                continue
            frac = (value - lo) / span
            bottom.axhline(blo + frac * (bhi - blo), **style)

    if axis in ("x", "both"):
        bottom.grid(True, axis="x", which=which, **style)


# ---- 单个元素绘制 ------------------------------------------------------

def _plot_one(ax, key: str, scfg: dict[str, Any], x: np.ndarray, y: np.ndarray,
              color: str) -> Optional[Line2D]:
    chart_type = str(scfg.get("chart_type", "line")).lower()
    alpha = float(scfg.get("alpha", 1.0))
    zorder = float(scfg.get("zorder", 3))
    lw = float(scfg.get("linewidth", 2.0))
    ls = str(scfg.get("linestyle", "-"))
    marker = str(scfg.get("marker", "") or "")
    ms = float(scfg.get("markersize", 4.5))
    mfc = _pick(scfg.get("markerfacecolor"), color)
    mec = _pick(scfg.get("markeredgecolor"), color)
    mew = float(scfg.get("markeredgewidth", 1.0))
    every = max(1, int(scfg.get("markevery", 1) or 1))
    label = str(scfg.get("_label", key))

    if chart_type == "bar":
        width = float(scfg.get("bar_width", 0.55))
        bars = ax.bar(
            x, np.nan_to_num(y, nan=0.0), width=width, color=color, alpha=alpha,
            edgecolor=str(scfg.get("bar_edgecolor", "none")),
            linewidth=float(scfg.get("bar_linewidth", 0.0)),
            align=str(scfg.get("bar_align", "center")),
            zorder=zorder, label=label,
        )
        return bars

    if chart_type in ("smooth", "spline"):
        xs, ys = _hermite_smooth(x, y, int(scfg.get("smooth_points", 240)))
        line, = ax.plot(xs, ys, color=color, alpha=alpha, linewidth=lw, linestyle=ls,
                        zorder=zorder, label=label)
        if marker:
            ax.plot(x, y, linestyle="none", marker=marker, markersize=ms,
                    markerfacecolor=mfc, markeredgecolor=mec, markeredgewidth=mew,
                    markevery=every, zorder=zorder + 0.1)
        return line

    if chart_type == "area":
        line, = ax.plot(x, y, color=color, alpha=alpha, linewidth=lw, linestyle=ls,
                        zorder=zorder, label=label)
        ax.fill_between(x, 0, np.nan_to_num(y, nan=0.0), color=color,
                        alpha=float(scfg.get("fill_alpha", 0.18)), zorder=zorder - 0.1)
        return line

    if chart_type == "step":
        line, = ax.step(x, y, where="mid", color=color, alpha=alpha,
                        linewidth=lw, linestyle=ls, zorder=zorder, label=label)
        return line

    if chart_type == "scatter":
        return ax.scatter(x, y, s=float(scfg.get("markersize", 30)) ** 2 / 6,
                          color=color, alpha=alpha, marker=_pick(scfg.get("marker"), "o"),
                          zorder=zorder, label=label, edgecolors=mec)

    if chart_type == "fill_between":
        base_key = normalize_series_key(str(scfg.get("fill_between_key", "minTemp")))
        base = scfg.get("_base_values")
        if base is None:
            base = np.zeros_like(y)
        line, = ax.plot(x, y, color=color, alpha=alpha, linewidth=lw, linestyle=ls,
                        zorder=zorder, label=label)
        ax.fill_between(x, base, np.nan_to_num(y, nan=np.nan), color=color,
                        alpha=float(scfg.get("fill_alpha", 0.18)), zorder=zorder - 0.1)
        return line

    return ax.plot(x, y, color=color, alpha=alpha, linewidth=lw, linestyle=ls,
                   marker=marker, markersize=ms, markerfacecolor=mfc,
                   markeredgecolor=mec, markeredgewidth=mew, markevery=every,
                   zorder=zorder, label=label)[0]


def _draw_data_labels(ax, scfg: dict[str, Any], x: np.ndarray, y: np.ndarray,
                      color: str, axis_scale: float = 1.0) -> None:
    dl = scfg.get("data_labels") or {}
    if not dl.get("show"):
        return
    target = ax
    fmt = str(dl.get("format", "{:.1f}"))
    lc = _pick(dl.get("color"), color)
    offset = float(dl.get("offset", 4.0))
    position = str(dl.get("position", "top")).lower()
    for xi, yi in zip(x, y):
        if not np.isfinite(yi):
            continue
        try:
            text = fmt.format(yi)
        except (ValueError, IndexError, KeyError):
            text = f"{yi:g}"
        va = "bottom" if position != "bottom" else "top"
        dy = offset if position != "bottom" else -offset
        target.annotate(
            text, xy=(xi, yi), xytext=(0, dy), textcoords="offset points",
            ha="center", va=va, fontsize=float(dl.get("fontsize", 8.5)),
            color=lc, rotation=float(dl.get("rotation", 0)), zorder=8,
        )


# ---- 标题/图例/署名 ----------------------------------------------------

def _add_titles(ax, city: CityClimate, cfg: dict[str, Any], context: dict[str, str]) -> None:
    fig_cfg = cfg["figure"]
    title_cfg = fig_cfg.get("title") or {}
    sub_cfg = fig_cfg.get("subtitle") or {}
    title_pad = float(title_cfg.get("pad", 14))

    if title_cfg.get("show", True):
        text = str(title_cfg.get("text", "{city}")).format(**context)
        if sub_cfg.get("show"):
            title_pad += float(sub_cfg.get("fontsize", 10)) * 1.5
        ax.set_title(
            text,
            fontsize=float(title_cfg.get("fontsize", 16)),
            color=str(title_cfg.get("color", "#2c3e50")),
            fontweight=str(title_cfg.get("fontweight", "bold")),
            pad=title_pad,
            loc=str(title_cfg.get("loc", "center")),
        )
    if sub_cfg.get("show"):
        text = str(sub_cfg.get("text", "")).format(**context)
        if text.strip():
            align = {"left": "left", "right": "right"}.get(str(sub_cfg.get("loc", "center")), "center")
            xpos = {"left": 0.0, "right": 1.0}.get(align, 0.5)
            ax.text(
                xpos, 1.0, text, transform=ax.transAxes, ha=align, va="bottom",
                fontsize=float(sub_cfg.get("fontsize", 10.5)),
                color=str(sub_cfg.get("color", "#596679")),
                fontweight=str(sub_cfg.get("fontweight", "normal")),
                clip_on=False,
            )


def _add_legend(ax, handles: list[Any], cfg: dict[str, Any]) -> None:
    lcfg = cfg["figure"].get("legend") or {}
    if not lcfg.get("show", True) or not handles:
        return
    ncol = int(lcfg.get("ncol", 0) or 0)
    if ncol <= 0:
        ncol = min(len(handles), 5)
    anchor = lcfg.get("bbox_to_anchor")
    kwargs: dict[str, Any] = {
        "loc": str(lcfg.get("loc", "upper center")),
        "ncol": ncol,
        "fontsize": float(lcfg.get("fontsize", 10)),
        "frameon": bool(lcfg.get("frameon", True)),
        "framealpha": float(lcfg.get("framealpha", 0.9)),
        "markerscale": float(lcfg.get("markerscale", 1.0)),
        "columnspacing": float(lcfg.get("columnspacing", 1.6)),
        "handlelength": float(lcfg.get("handlelength", 2.0)),
        "handletextpad": float(lcfg.get("handletextpad", 0.6)),
    }
    if isinstance(anchor, (list, tuple)) and len(anchor) == 2:
        kwargs["bbox_to_anchor"] = (float(anchor[0]), float(anchor[1]))
    if lcfg.get("title"):
        kwargs["title"] = str(lcfg["title"])
    legend = ax.legend(handles=handles, **kwargs)
    if lcfg.get("facecolor"):
        legend.get_frame().set_facecolor(str(lcfg["facecolor"]))
    if lcfg.get("edgecolor"):
        legend.get_frame().set_edgecolor(str(lcfg["edgecolor"]))


def _add_credit(fig, cfg: dict[str, Any], context: dict[str, str]) -> None:
    ccfg = cfg["figure"].get("credit") or {}
    if not ccfg.get("show"):
        return
    text = str(ccfg.get("text", "")).format(**context)
    if not text.strip():
        return
    loc = str(ccfg.get("loc", "right"))
    x = {"left": 0.01, "center": 0.5, "right": 0.99}.get(loc, 0.99)
    fig.text(x, -0.02, text, ha=loc if loc in ("left", "center", "right") else "right",
             va="top", fontsize=float(ccfg.get("fontsize", 8.5)),
             color=str(ccfg.get("color", "#7a8899")))


def _apply_layout(fig, cfg: dict[str, Any]) -> None:
    layout = str(cfg["figure"].get("layout", "tight"))
    if layout == "tight":
        fig.tight_layout()
    elif layout == "constrained":
        fig.set_layout_engine("constrained")
    adjust = cfg["figure"].get("subplots_adjust") or {}
    manual = {k: float(v) for k, v in adjust.items() if v is not None}
    if manual:
        fig.subplots_adjust(**manual)


def _save(fig, out_paths: list[Path], cfg: dict[str, Any]) -> list[Path]:
    dpi = float(cfg["output"].get("chart_dpi", cfg["figure"].get("dpi", 144)))
    constrained = str(cfg["figure"].get("layout")) == "constrained"
    # 轴标题可能被 label_x / label_y 移到坐标轴之外；matplotlib 的自动 bbox 不总能覆盖这类
    # 手动定位的标签，会被 tight 裁剪掉。这里在**默认艺术家集合**基础上补入轴标签
    # （注意 bbox_extra_artists 是替换而非追加，直接用会导致图例等被排除）。
    extra_artists: Optional[list[Any]] = None
    if not constrained:
        extra_artists = list(fig.get_default_bbox_extra_artists())
        extra_artists += [a.yaxis.label for a in fig.axes]
        extra_artists += [a.xaxis.label for a in fig.axes]
    saved: list[Path] = []
    for path in out_paths:
        fmt = path.suffix.lstrip(".").lower() or "png"
        fig.savefig(path, dpi=dpi, format=fmt,
                    facecolor=cfg["figure"].get("facecolor", "#ffffff"),
                    edgecolor=cfg["figure"].get("edgecolor", "none"),
                    bbox_inches=None if constrained else "tight",
                    bbox_extra_artists=extra_artists)
        saved.append(path)
    plt.close(fig)
    return saved


def _context(city: CityClimate, cfg: dict[str, Any]) -> dict[str, str]:
    return {
        "city": city.city_name,
        "city_id": str(city.city_id),
        "station": city.station_name or city.city_name,
        "member": city.member.mem_name,
        "org": city.member.org_name,
        "period": city.period_note(),
        # 经纬度：模型里存的是数值，显示文案按 data.coord 现算（方向符号/单位/小数位可配）
        "lat": format_coord(city.latitude, "lat", cfg),
        "lon": format_coord(city.longitude, "lon", cfg),
        "profile": str(cfg.get("profile_name", "")),
        "temp_unit": "°F" if (cfg["data"].get("temp_unit") or "C").upper() == "F" else "°C",
        "rain_unit": city.rain_unit_label(),
    }


# ---- 主入口：单城市 ----------------------------------------------------

def _grow_box(box: Any, pad: float) -> Any:
    """把 Bbox 四边各外扩 pad 像素。

    注意：``Bbox.expanded(sw, sh)`` 在本项目的 matplotlib 版本里按**倍数**解释参数，
    直接传点数会把盒子放大数倍，因此统一用这个显式版本。
    """
    return Bbox.from_extents(box.x0 - pad, box.y0 - pad, box.x1 + pad, box.y1 + pad)


def _densify(points: np.ndarray, samples: int = 32) -> np.ndarray:
    """把折线按段加密成点云，便于用"点在矩形内"做碰撞判定。"""
    if len(points) < 2:
        return points
    starts, ends = points[:-1], points[1:]
    ts = np.linspace(0.0, 1.0, samples)[:, None, None]
    return (starts[None] + (ends - starts)[None] * ts).reshape(-1, 2)


def _cloud_hits_box(points: np.ndarray, box: Any) -> bool:
    """点云（显示坐标）里是否有落在矩形内的点。"""
    if len(points) == 0:
        return False
    return bool(np.any((points[:, 0] >= box.x0) & (points[:, 0] <= box.x1)
                       & (points[:, 1] >= box.y0) & (points[:, 1] <= box.y1)))


def _place_extremes_label(ax, text: str, xy: tuple[float, float], base_dy: float,
                          line_obstacles: list[Any], placed_boxes: list[Any],
                          acfg: dict[str, Any]) -> tuple[Any, Optional[Any]]:
    """放置最高/最低气温标注，必要时自动避让，返回 (标注对象, 最终文字盒)。

    候选集合：沿偏好侧外推（基准 / 1.6 / 2.2 / 3.0 倍）× 横向错位，以及翻到数据点另一侧
    （``allow_flip``）的同样几档。横向档位按**标注自身半宽**取（长文案固定挪 ±30pt 根本
    挪不出界），另加固定的 ±30 / ±60pt。

    每个候选再派生一个**最小幅度推回坐标区内**的版本：首月/末月或数值贴近上下边界时，
    标注常常整块落到绘图区之外（实测乌兰巴托 + 简图系配置：最低月标注掉到坐标区底边以下、
    压住月份刻度，只因"降低 1.6 倍"是唯一不压平均降水线的解）。推回版本让算法在
    "压线"与"出界"之外多出"区内且不压线"的第三种选择。

    全部候选按 "冲突最少 → 越界最少（各边越界量按点累加，而非只数越界边数）→ 位移最小
    （翻边另加权重）" 评分，因此既避开曲线，也尽量少动、尽量不出界。
    偏移以**点**为单位，与画布尺寸、布局无关；``max_distance`` 仍是最终偏移各分量的硬上限。
    """
    color = str(acfg.get("color", "#a32d2d"))
    anno = ax.annotate(
        text, xy=xy, xytext=(0.0, float(base_dy)), textcoords="offset points",
        ha="center", fontsize=float(acfg.get("fontsize", 9)), color=color, zorder=9,
        arrowprops={"arrowstyle": "-", "color": color, "linewidth": 0.8},
    )
    if not acfg.get("avoid_overlap", True):
        return anno, None
    try:
        fig = ax.get_figure()
        renderer = fig.canvas.get_renderer()
        axes_box = ax.get_window_extent()
        # 带箭头的标注在 draw 之前文字变换尚未更新，先手动刷新一次；之后
        # Text.get_window_extent 取到的**纯文字盒**与真正绘制出来的完全一致
        # （不能用 Annotation.get_window_extent：它会把箭头并进来，必然压住数据点自身）。
        anno.update_positions(renderer)
        box0 = Text.get_window_extent(anno, renderer)
    except (AttributeError, IndexError, TypeError, ValueError):
        return anno, None

    scale = float(fig.dpi) / 72.0                      # 点 → 像素
    gap_px = float(acfg.get("gap", 2.0) or 0.0) * scale
    limit = abs(float(acfg.get("max_distance", 52.0) or 52.0))

    def cap(value: float) -> float:
        """限制搜索半径：各分量都不超过 max_distance。"""
        return (1.0 if value >= 0 else -1.0) * min(abs(value), limit)

    base = float(base_dy)
    preferred_up = base >= 0

    # 横向档位：0 → 半个标注宽 → 一个标注宽，再补固定档；长文案必须按自身宽度挪
    half_w = max(box0.width / scale / 2.0, 1.0)
    dx_steps: list[float] = [0.0]
    for mult in (1.0, 1.4, 2.0):
        dx_steps.extend((half_w * mult, -half_w * mult))
    dx_steps.extend((30.0, -30.0, 60.0, -60.0))

    candidates: list[tuple[float, float]] = []
    for mult in (1.0, 1.6, 2.2, 3.0):
        for dx in dx_steps:
            candidates.append((cap(dx), cap(base * mult)))
    if acfg.get("allow_flip", True):
        for mult in (1.0, 1.6, 2.2):
            for dx in dx_steps:
                candidates.append((cap(dx), cap(-base * mult)))

    def push_inside(dx: float, dy: float) -> tuple[float, float]:
        """把候选框以最小幅度推回坐标区内（留出 gap）；已在区内则原样返回。"""
        box = box0.translated(dx * scale, (dy - base) * scale)
        shift_x = shift_y = 0.0
        if box.x0 < axes_box.x0 + gap_px:
            shift_x = axes_box.x0 + gap_px - box.x0
        elif box.x1 > axes_box.x1 - gap_px:
            shift_x = axes_box.x1 - gap_px - box.x1
        if box.y0 < axes_box.y0 + gap_px:
            shift_y = axes_box.y0 + gap_px - box.y0
        elif box.y1 > axes_box.y1 - gap_px:
            shift_y = axes_box.y1 - gap_px - box.y1
        if shift_x == 0.0 and shift_y == 0.0:
            return dx, dy
        return cap(dx + shift_x / scale), cap(dy + shift_y / scale)

    for cand in list(candidates):
        pushed = push_inside(*cand)
        if pushed != cand:
            candidates.append(pushed)

    clouds = []
    for ln in line_obstacles:
        pts = np.asarray(ln.get_xydata(), dtype=float)
        if pts.size:
            # axhline 用的是混合变换，必须走艺术家自己的 transform
            clouds.append(_densify(ln.get_transform().transform(pts)))

    best: Optional[tuple[tuple[float, float, float], float, float]] = None
    for dx, dy in candidates:
        box = box0.translated(dx * scale, (dy - base) * scale)
        test = _grow_box(box, gap_px)
        hits = sum(1 for pts in clouds if _cloud_hits_box(pts, test))
        hits += sum(1 for ob in placed_boxes
                    if test.x0 < ob.x1 and ob.x0 < test.x1
                    and test.y0 < ob.y1 and ob.y0 < test.y1)
        # 越界量（点）：四条边超出坐标区的部分累加，比"越界边数"更能分辨越界深浅
        spill = (max(0.0, axes_box.x0 - box.x0) + max(0.0, box.x1 - axes_box.x1)
                 + max(0.0, axes_box.y0 - box.y0) + max(0.0, box.y1 - axes_box.y1)) / scale
        flipped = (dy > 0) != preferred_up
        cost = abs(dx) + abs(dy - base) + (30.0 if flipped else 0.0)
        score = (float(hits), float(spill), cost)
        if best is None or score < best[0]:
            best = (score, dx, dy)
    assert best is not None
    _, dx, dy = best
    anno.set_position((dx, dy))
    return anno, box0.translated(dx * scale, (dy - base) * scale)


def _draw_extremes_annotations(ax, city: CityClimate, cfg: dict[str, Any], x: np.ndarray,
                               series_list: list[Any], data_lines: list[Any],
                               mean_line: Any) -> None:
    """最高/最低月标注。

    **必须在布局定型之后调用**（``_apply_layout`` 之后）：避让判定要用到坐标区的实际
    矩形，放布局之前拿到的是初始 subplot 位置，会把"其实没越界"的标注也挪走。
    """
    acfg = cfg["figure"].get("annotation") or {}
    if not acfg.get("show_extremes"):
        return
    target_key = normalize_series_key(str(acfg.get("series", "meanTemp")))
    arr = None
    for k, _scfg, values in series_list:
        if k == target_key:
            arr = values
            break
    if arr is None or not np.isfinite(arr).any():
        return

    finite = np.where(np.isfinite(arr), arr, np.nan)
    hi_i = int(np.nanargmax(finite))
    lo_i = int(np.nanargmin(finite))
    labels = city.month_labels(str(cfg["data"].get("month_label_style", "1月")))
    obstacles = list(data_lines)
    if mean_line is not None:
        obstacles.append(mean_line)             # 平均降水线也是要避开的目标
    placed_boxes: list[Any] = []
    for idx, tag in ((hi_i, "最高"), (lo_i, "最低")):
        text = f"{labels[idx]} {tag}"
        if acfg.get("show_value", True):
            text += f" {float(arr[idx]):.1f}"
        _anno, box = _place_extremes_label(
            ax, text, (float(x[idx]), float(arr[idx])),
            EXTREME_LABEL_OFFSETS.get(tag, 16.0), obstacles, placed_boxes, acfg,
        )
        if box is not None:
            placed_boxes.append(box)            # 后一个标注要避开前一个


def render_city_chart(city: CityClimate, cfg: dict[str, Any],
                      out_paths: list[Path], logger=None,
                      report: Optional[dict[str, Any]] = None) -> list[Path]:
    if not city.has_climate:
        raise ChartError(f"{city.city_name}（cityId {city.city_id}）没有气候数据，无法绘图")

    setup_style(cfg)
    fig_cfg = cfg["figure"]
    figsize = fig_cfg.get("figsize") or [12.0, 6.0]

    fig = plt.figure(
        figsize=(float(figsize[0]), float(figsize[1])),
        dpi=float(fig_cfg.get("dpi", 144)),
        facecolor=str(fig_cfg.get("facecolor", "#ffffff")),
        edgecolor=str(fig_cfg.get("edgecolor", "none")),
    )
    ax = fig.add_subplot(111)
    ax.set_facecolor(_resolve_axes_facecolor(cfg))

    series_list = collect_series(city, cfg, logger)
    by_axis: dict[str, list[tuple[str, dict[str, Any], np.ndarray]]] = {a: [] for a in AXIS_KEYS}
    for key, scfg, arr in series_list:
        by_axis[str(scfg.get("axis", "primary"))].append((key, scfg, arr))

    ax2 = ax.twinx() if by_axis["secondary"] else None
    ax3 = None
    if by_axis["tertiary"]:
        ax3 = ax.twinx()
        offset = float(cfg["axes_tertiary"].get("offset_points", 62) or 0.0)
        ax3.spines["right"].set_position(("outward", offset))
    for extra in (ax2, ax3):
        if extra is not None:
            extra.set_facecolor("none")
            extra.grid(False)

    axis_map = {"primary": ax, "secondary": ax2, "tertiary": ax3}
    bottom_key = _stack_axes(cfg, axis_map, by_axis)

    x = _configure_x_axis(ax, city, cfg)
    # 色带画在最底层坐标轴上：柱状图若在副轴（被压在主轴之下），色带画在主轴上会罩住柱子
    _draw_background(axis_map[bottom_key], city, cfg)

    # ---- 绘制元素 ----
    # 绘图顺序固定为「先柱状、后折线」（同组内按 order_in_legend）；
    # 图例顺序仍按 order_in_legend，不受绘制顺序影响。
    handles_by_key: dict[str, Any] = {}
    # 记录绘制元素前已有的 Line2D，画完后"新增的那些"就是各数据曲线（含仅标记点的那条），
    # 供极值标注避让使用；网格线/零线/平均降水线都不在这个集合里。
    lines_before = {id(ln) for a in axis_map.values() if a is not None for ln in a.lines}
    draw_order = sorted(series_list, key=lambda item: _draw_priority(item[1]))
    for key, scfg, arr in draw_order:
        target = axis_map.get(str(scfg.get("axis", "primary")))
        if target is None:
            continue
        color = _pick(scfg.get("color"), "#333333")
        scfg = dict(scfg)
        scfg["_label"] = _series_label(city, key, scfg)
        if scfg.get("chart_type") == "fill_between":
            base_key = normalize_series_key(str(scfg.get("fill_between_key", "minTemp")))
            base_vals = None
            for k2, s2, v2 in series_list:
                if k2 == base_key:
                    base_vals = v2
                    break
            scfg["_base_values"] = base_vals
        handle = _plot_one(target, key, scfg, x, arr, color)
        if handle is not None:
            handles_by_key[key] = handle
        _draw_data_labels(target, scfg, x, arr, color)

    data_lines = [ln for a in axis_map.values() if a is not None
                  for ln in a.lines if id(ln) not in lines_before]
    handles = [handles_by_key[k] for k, _scfg, _arr in series_list if k in handles_by_key]
    if not handles:
        plt.close(fig)
        raise ChartError(f"{city.city_name} 没有任何可绘制的元素，请检查配置中 series 的 enabled")

    # 回填实际绘出的要素（供上层日志/汇总展示）
    if report is not None:
        report["series"] = [
            {
                "key": key,
                "label": _series_label(city, key, scfg),
                "chart_type": str(scfg.get("chart_type", "line")),
                "axis": str(scfg.get("axis", "primary")),
            }
            for key, scfg, _arr in series_list
        ]

    # ---- 轴样式 ----
    axis_specs = [
        (ax, "axes_primary", by_axis["primary"], -0.075, False),
        (ax2, "axes_secondary", by_axis["secondary"], 1.075, True),
        (ax3, "axes_tertiary", by_axis["tertiary"], 1.155, True),
    ]
    for target, key_name, items, default_x, only_side in axis_specs:
        if target is None:
            continue
        _configure_y_axis(
            target,
            cfg[key_name],
            [v for _, _, v in items] or [np.array([0.0])],
            default_x=default_x,
            has_bar=any(s.get("chart_type") == "bar" for _, s, _ in items),
            cfg=cfg,
            logger=logger,
            only_side=only_side,
        )
        if only_side:
            target.tick_params(axis="x", length=0, labelsize=0)
    if ax3 is not None:
        # 偏移后的轴脊需要重新应用位置（configure 里会重置可见性）
        ax3.spines["right"].set_position(
            ("outward", float(cfg["axes_tertiary"].get("offset_points", 62) or 0.0))
        )

    grid_cfg = fig_cfg.get("grid") or {}
    if grid_cfg.get("show", True):
        _draw_grid(ax, axis_map, bottom_key, grid_cfg)
    ax.set_axisbelow(True)
    _draw_zeroline(ax, cfg)

    # ---- 平均降水线（默认关闭，需 figure.mean_rain_line.show = true）----
    mean_line, mean_anno = _draw_mean_rain_line(axis_map, city, cfg)
    if mean_line is not None and str(mean_line.get_label() or "").strip():
        handles.append(mean_line)

    context = _context(city, cfg)
    _add_titles(ax, city, cfg, context)
    _add_legend(ax, handles, cfg)
    _add_credit(fig, cfg, context)
    _apply_layout(fig, cfg)
    # 平均降水线标注与极值标注同理，放在布局定型之后：横纵落点/避让都要用坐标区的最终几何
    if mean_anno is not None:
        _place_mean_rain_annotation(mean_anno.axes, mean_anno, cfg,
                                    float(mean_line.get_ydata()[0]))
    # 极值标注放在布局定型之后：避让判定要用坐标区的最终矩形
    _draw_extremes_annotations(ax, city, cfg, x, series_list, data_lines, mean_line)
    return _save(fig, out_paths, cfg)


# ---- 主入口：多城市对比 ------------------------------------------------

def render_comparison_chart(cities: list[CityClimate], cfg: dict[str, Any],
                            out_paths: list[Path], logger=None,
                            report: Optional[dict[str, Any]] = None) -> list[Path]:
    usable = [c for c in cities if c.has_climate]
    if not usable:
        raise ChartError("参与对比的城市都没有气候数据，无法绘图")

    cmpc = cfg.get("compare") or {}
    metric = normalize_series_key(str(cmpc.get("metric", "meanTemp")))
    temp_unit = (cfg["data"].get("temp_unit") or "C").upper()
    rain_unit = (cfg["data"].get("rain_unit") or "mm").lower()

    def sort_key(city: CityClimate):
        mode = str(cmpc.get("sort_by", "value_desc"))
        if mode == "name":
            return (0, city.city_name)
        if mode == "city_id":
            return (0, city.city_id)
        value = city.annual(metric, temp_unit, rain_unit)
        value = float("-inf") if value is None else value
        return (0, -value if mode != "value_asc" else value)

    if str(cmpc.get("sort_by", "value_desc")) != "none":
        usable = sorted(usable, key=sort_key)

    setup_style(cfg)
    figsize = cmpc.get("figsize") or cfg["figure"].get("figsize") or [12.0, 6.0]
    fig = plt.figure(
        figsize=(float(figsize[0]), float(figsize[1])),
        dpi=float(cfg["output"].get("chart_dpi", cfg["figure"].get("dpi", 144))),
        facecolor=str(cfg["figure"].get("facecolor", "#ffffff")),
    )
    ax = fig.add_subplot(111)
    ax.set_facecolor(_resolve_axes_facecolor(cfg))

    template_city = usable[0]
    x = _configure_x_axis(ax, template_city, cfg)

    colors = list(cmpc.get("colors") or ["#d85a30", "#185fa5"])
    color_map = cmpc.get("color_map") or {}
    linestyles = list(cmpc.get("linestyle_cycle") or ["-"])
    markers = list(cmpc.get("marker_cycle") or ["o"])
    chart_type = str(cmpc.get("chart_type", "smooth")).lower()
    array_module = np

    all_values: list[np.ndarray] = []
    handles: list[Any] = []
    for i, city in enumerate(usable):
        values = array_module.array(
            [np.nan if v is None else float(v) for v in city.values(metric, temp_unit, rain_unit)],
            dtype=float,
        )
        policy = _missing_policy(cfg)
        values = _apply_missing(values, policy)
        all_values.append(values)

        if str(cmpc.get("color_by", "order")) == "city_id":
            color = colors[city.city_id % len(colors)]
        else:
            color = color_map.get(city.city_name) or colors[i % len(colors)]
        ls = linestyles[i % len(linestyles)]
        marker = markers[i % len(markers)]
        label = str(cmpc.get("label_template", "{city}")).format(city=city.city_name)
        if cmpc.get("show_value_range", False):
            valid = values[np.isfinite(values)]
            if valid.size:
                label += f"（{valid.min():.1f}~{valid.max():.1f}）"

        if chart_type == "bar":
            total = len(usable)
            width = float(cmpc.get("bar_width", 0.8)) / max(total, 1)
            offset = (i - (total - 1) / 2.0) * width
            handle = ax.bar(x + offset, np.nan_to_num(values, nan=0.0), width=width,
                            color=color, alpha=float(cmpc.get("alpha", 1.0)),
                            label=label, zorder=3)
        elif chart_type in ("smooth", "spline"):
            xs, ys = _hermite_smooth(x, values, 240)
            handle, = ax.plot(xs, ys, color=color, alpha=float(cmpc.get("alpha", 1.0)),
                              linewidth=float(cmpc.get("linewidth", 2.2)), linestyle=ls,
                              label=label, zorder=3)
            ax.plot(x, values, linestyle="none", marker=marker,
                    markersize=float(cmpc.get("markersize", 5.0)), color=color, zorder=3.1)
        else:
            handle, = ax.plot(x, values, color=color, alpha=float(cmpc.get("alpha", 1.0)),
                              linewidth=float(cmpc.get("linewidth", 2.2)), linestyle=ls,
                              marker=marker, markersize=float(cmpc.get("markersize", 5.0)),
                              label=label, zorder=3)
        handles.append(handle)

        dl = cmpc.get("data_labels") or {}
        if dl.get("show"):
            for xi, yi in zip(x, values):
                if not np.isfinite(yi):
                    continue
                try:
                    text = str(dl.get("format", "{:.1f}")).format(yi)
                except (ValueError, IndexError, KeyError):
                    text = f"{yi:g}"
                ax.annotate(text, xy=(xi, yi), xytext=(0, float(dl.get("offset", 4.0))),
                            textcoords="offset points", ha="center", va="bottom",
                            fontsize=float(dl.get("fontsize", 8.0)), color=color, zorder=8)

    # 轴：对比图只用单轴，沿用主轴或副轴配置
    axis_key = "secondary" if metric in RAIN_KEYS else "primary"
    axis_cfg = dict(cfg[("axes_secondary" if axis_key == "secondary" else "axes_primary")])
    axis_cfg["side"] = "left"
    if not axis_cfg.get("label_text"):
        axis_cfg["label_text"] = metric
    _configure_y_axis(ax, axis_cfg, all_values, default_x=-0.075,
                      has_bar=chart_type == "bar", cfg=cfg, logger=logger)

    grid_cfg = cfg["figure"].get("grid") or {}
    if grid_cfg.get("show", True):
        ax.grid(True, axis=str(grid_cfg.get("axis", "y")),
                color=str(grid_cfg.get("color", "#c9d3dd")),
                linestyle=str(grid_cfg.get("linestyle", "--")),
                linewidth=float(grid_cfg.get("linewidth", 0.7)),
                alpha=float(grid_cfg.get("alpha", 0.75)), zorder=0)
    ax.set_axisbelow(True)
    _draw_zeroline(ax, cfg)

    # 标题上下文用"多城市"合成
    metric_label = {"minTemp": "日均最低气温", "maxTemp": "日均最高气温",
                    "meanTemp": "日均气温", "rainfall": "平均总降水",
                    "raindays": "平均降水日数"}.get(metric, metric)
    context = {
        "city": f"{len(usable)} 个城市",
        "city_id": "",
        "station": "",
        "member": "、".join({c.member.mem_name for c in usable if c.member.mem_name}),
        "org": "",
        "period": "、".join(sorted({c.period_note() for c in usable if c.period_note()})),
        "lat": "", "lon": "",
        "profile": str(cfg.get("profile_name", "")),
        "temp_unit": "°F" if temp_unit == "F" else "°C",
        "rain_unit": usable[0].rain_unit_label(),
        "metric": metric_label,
    }
    fig_cfg = cfg["figure"]
    title_cfg = dict(fig_cfg.get("title") or {})
    title_cfg["text"] = str(cmpc.get("title_text") or "{metric}对比 · {city}")
    ax.set_title(
        str(title_cfg["text"]).format(**context),
        fontsize=float(title_cfg.get("fontsize", 16)),
        color=str(title_cfg.get("color", "#2c3e50")),
        fontweight=str(title_cfg.get("fontweight", "bold")),
        pad=float(title_cfg.get("pad", 14)),
        loc=str(title_cfg.get("loc", "center")),
    )
    sub_cfg = fig_cfg.get("subtitle") or {}
    if sub_cfg.get("show"):
        text = str(sub_cfg.get("text", "")).format(**context)
        if text.strip():
            ax.text(0.5, 1.0, text, transform=ax.transAxes, ha="center", va="bottom",
                    fontsize=float(sub_cfg.get("fontsize", 10.5)),
                    color=str(sub_cfg.get("color", "#596679")), clip_on=False)

    legend_cfg = dict(fig_cfg.get("legend") or {})
    ncol = int(cmpc.get("legend_ncol", 0) or 0)
    if ncol <= 0:
        ncol = min(len(handles), 5)
    legend_cfg["ncol"] = ncol
    merged = dict(cfg)
    merged["figure"] = dict(fig_cfg)
    merged["figure"]["legend"] = legend_cfg
    _add_legend(ax, handles, merged)
    _add_credit(fig, cfg, context)
    _apply_layout(fig, cfg)
    if report is not None:
        report["series"] = [metric_label]
        report["metric"] = metric_label
        report["city_count"] = len(usable)
    return _save(fig, out_paths, cfg)
