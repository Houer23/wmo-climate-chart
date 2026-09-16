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
from matplotlib.ticker import FuncFormatter, MaxNLocator  # noqa: E402

from .models import CityClimate, normalize_series_key  # noqa: E402

TEMP_KEYS = ("minTemp", "maxTemp", "meanTemp")
RAIN_KEYS = ("rainfall", "raindays")
AXIS_KEYS = ("primary", "secondary", "tertiary")


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
        ax.set_ylabel(
            label,
            fontsize=float(ax_cfg.get("label_fontsize", 12)),
            color=str(ax_cfg.get("label_color", "#2c3e50")),
            fontweight=str(ax_cfg.get("label_fontweight", "bold")),
            rotation=ax_cfg.get("label_rotation", 90),
            labelpad=float(ax_cfg.get("label_pad", 10)),
        )
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


def _draw_background(ax, city: CityClimate, cfg: dict[str, Any]) -> None:
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
    for season in bands.get("seasons") or []:
        months = {int(m) for m in (season.get("months") or [])}
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
    saved: list[Path] = []
    for path in out_paths:
        fmt = path.suffix.lstrip(".").lower() or "png"
        fig.savefig(path, dpi=dpi, format=fmt,
                    facecolor=cfg["figure"].get("facecolor", "#ffffff"),
                    edgecolor=cfg["figure"].get("edgecolor", "none"),
                    bbox_inches="tight" if str(cfg["figure"].get("layout")) != "constrained" else None)
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
        "lat": f"{city.latitude:.2f}" if city.latitude is not None else "",
        "lon": f"{city.longitude:.2f}" if city.longitude is not None else "",
        "profile": str(cfg.get("profile_name", "")),
        "temp_unit": "°F" if (cfg["data"].get("temp_unit") or "C").upper() == "F" else "°C",
        "rain_unit": city.rain_unit_label(),
    }


# ---- 主入口：单城市 ----------------------------------------------------

def render_city_chart(city: CityClimate, cfg: dict[str, Any],
                      out_paths: list[Path], logger=None) -> list[Path]:
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

    x = _configure_x_axis(ax, city, cfg)
    _draw_background(ax, city, cfg)

    # ---- 绘制元素 ----
    handles: list[Any] = []
    for key, scfg, arr in series_list:
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
            handles.append(handle)
        _draw_data_labels(target, scfg, x, arr, color)

    if not handles:
        plt.close(fig)
        raise ChartError(f"{city.city_name} 没有任何可绘制的元素，请检查配置中 series 的 enabled")

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
        ax.grid(
            True,
            axis=str(grid_cfg.get("axis", "y")),
            which=str(grid_cfg.get("which", "major")),
            color=str(grid_cfg.get("color", "#c9d3dd")),
            linestyle=str(grid_cfg.get("linestyle", "--")),
            linewidth=float(grid_cfg.get("linewidth", 0.7)),
            alpha=float(grid_cfg.get("alpha", 0.75)),
            zorder=float(grid_cfg.get("zorder", 0)),
        )
    ax.set_axisbelow(True)
    _draw_zeroline(ax, cfg)

    # ---- 极值标注 ----
    acfg = fig_cfg.get("annotation") or {}
    if acfg.get("show_extremes"):
        target_key = normalize_series_key(str(acfg.get("series", "meanTemp")))
        arr = None
        for k, s, v in series_list:
            if k == target_key:
                arr = v
                break
        if arr is not None and np.isfinite(arr).any():
            finite = np.where(np.isfinite(arr), arr, np.nan)
            hi_i = int(np.nanargmax(finite))
            lo_i = int(np.nanargmin(finite))
            labels = city.month_labels(str(cfg["data"].get("month_label_style", "1月")))
            for idx, tag in ((hi_i, "最高"), (lo_i, "最低")):
                text = f"{labels[idx]} {tag}"
                if acfg.get("show_value", True):
                    text += f" {float(arr[idx]):.1f}"
                ax.annotate(
                    text, xy=(x[idx], arr[idx]), xytext=(0, 16 if tag == "最高" else -22),
                    textcoords="offset points", ha="center", fontsize=float(acfg.get("fontsize", 9)),
                    color=str(acfg.get("color", "#a32d2d")), zorder=9,
                    arrowprops={"arrowstyle": "-", "color": str(acfg.get("color", "#a32d2d")),
                                "linewidth": 0.8},
                )

    context = _context(city, cfg)
    _add_titles(ax, city, cfg, context)
    _add_legend(ax, handles, cfg)
    _add_credit(fig, cfg, context)
    _apply_layout(fig, cfg)
    return _save(fig, out_paths, cfg)


# ---- 主入口：多城市对比 ------------------------------------------------

def render_comparison_chart(cities: list[CityClimate], cfg: dict[str, Any],
                            out_paths: list[Path], logger=None) -> list[Path]:
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
    return _save(fig, out_paths, cfg)
