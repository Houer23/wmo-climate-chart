"""数据模型：气候月值、城市气候信息、统计量口径。

设计要点
--------
1. 数据源 ``climateMonth[].meanTemp`` 实测恒为 null，平均气温一律由
   ``(minTemp + maxTemp) / 2`` 派生，派生结果用 ``mean_temp_derived`` 标记。
2. 温度同时保留摄氏度与华氏度原始值，输出单位由配置 ``data.temp_unit`` 决定，
   不在解析阶段做单位换算，避免精度损失与口径混淆。
3. 所有数值在解析阶段统一为 ``float | None``，空串 / "NULL" / 缺失一律为 None，
   由上层按 ``data.fill_missing`` 策略决定如何呈现。
4. 经纬度同样以 **数值**（``float | None``）保存（数据源给的是 ``"39.933333000"`` 这类
   字符串，解析阶段即转成数）；显示文案一律由 :func:`format_coord` / :func:`coord_pair`
   按配置 ``data.coord`` 现算，不在模型里固化格式。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# 规范的绘图元素键（unit 无关）
SERIES_KEYS = ("minTemp", "maxTemp", "meanTemp", "rainfall", "raindays")

# 别名 -> 规范键，兼容 minTempC / maxTempF 等写法
SERIES_ALIASES = {
    "minTemp": "minTemp",
    "minTempC": "minTemp",
    "minTempF": "minTemp",
    "maxTemp": "maxTemp",
    "maxTempC": "maxTemp",
    "maxTempF": "maxTemp",
    "meanTemp": "meanTemp",
    "meanTempC": "meanTemp",
    "meanTempF": "meanTemp",
    "avgTemp": "meanTemp",
    "rainfall": "rainfall",
    "rain": "rainfall",
    "precipitation": "rainfall",
    "ppt": "rainfall",
    "raindays": "raindays",
    "rainDays": "raindays",
    "rdays": "raindays",
}

# 温度类元素（走主轴、按平均值统计年值）
TEMP_KEYS = ("minTemp", "maxTemp", "meanTemp")
# 降水类元素（走副轴、按累加统计年值）
RAIN_KEYS = ("rainfall", "raindays")


def normalize_series_key(key: str) -> str:
    """把用户写的元素键规范化为规范键；无法识别时抛 KeyError。"""
    if key in SERIES_ALIASES:
        return SERIES_ALIASES[key]
    lowered = key.strip()
    for alias, canon in SERIES_ALIASES.items():
        if alias.lower() == lowered.lower():
            return canon
    raise KeyError(key)


def c_to_f(celsius: Optional[float]) -> Optional[float]:
    if celsius is None:
        return None
    return celsius * 9.0 / 5.0 + 32.0


def mm_to_inch(mm: Optional[float]) -> Optional[float]:
    if mm is None:
        return None
    return mm / 25.4


@dataclass
class MonthClimate:
    """某个月的气候平均值。"""

    month: int
    min_temp: Optional[float] = None
    max_temp: Optional[float] = None
    mean_temp: Optional[float] = None
    min_temp_f: Optional[float] = None
    max_temp_f: Optional[float] = None
    mean_temp_f: Optional[float] = None
    rainfall: Optional[float] = None
    rain_days: Optional[float] = None
    source_date: str = ""
    mean_temp_derived: bool = False

    def fill_derived_mean(self) -> None:
        """数据源 meanTemp 为空时，用 (min+max)/2 补出平均气温。"""
        if self.mean_temp is not None:
            return
        if self.min_temp is None or self.max_temp is None:
            return
        self.mean_temp = round((self.min_temp + self.max_temp) / 2.0, 2)
        self.mean_temp_derived = True
        if self.mean_temp_f is None:
            self.mean_temp_f = c_to_f(self.mean_temp)

    def value(self, key: str, temp_unit: str = "C", rain_unit: str = "mm") -> Optional[float]:
        """按元素键取原始值（温度按 temp_unit、降水按 rain_unit）。"""
        key = normalize_series_key(key)
        if key == "minTemp":
            return self.min_temp if temp_unit == "C" else self.min_temp_f
        if key == "maxTemp":
            return self.max_temp if temp_unit == "C" else self.max_temp_f
        if key == "meanTemp":
            return self.mean_temp if temp_unit == "C" else self.mean_temp_f
        if key == "rainfall":
            return self.rainfall if rain_unit == "mm" else mm_to_inch(self.rainfall)
        if key == "raindays":
            return self.rain_days
        raise KeyError(key)


@dataclass
class Period:
    """统计时段信息（数据源里分散在若干字段）。"""

    temp_from: str = ""
    temp_to: str = ""
    data_from: str = ""
    data_to: str = ""
    rain_days_from: str = ""
    rain_days_to: str = ""
    rainfall_from: str = ""
    rainfall_to: str = ""
    clino: str = ""

    def note(self, fmt: str = "{start}\u2013{end}") -> str:
        """优先用气温时段，其次用降水时段，最后用通用 data 时段。"""
        start, end = self.temp_from, self.temp_to
        if not (start and end):
            start, end = self.rainfall_from, self.rainfall_to
        if not (start and end):
            start, end = self.data_from, self.data_to
        if not (start or end):
            return ""
        return fmt.format(start=start or "?", end=end or "?")


@dataclass
class Member:
    """所属国家/地区气象机构。"""

    mem_id: Optional[int] = None
    mem_name: str = ""
    org_name: str = ""
    url: str = ""
    logo: str = ""


@dataclass
class CityClimate:
    """一个城市的气候信息（解析结果）。"""

    city_id: int
    city_name: str = ""
    station_name: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    time_zone: str = ""
    is_capital: bool = False
    is_dep: bool = False
    lang: str = "zh"
    member: Member = field(default_factory=Member)
    raintype: str = ""
    rain_unit: str = ""
    period: Period = field(default_factory=Period)
    months: list[MonthClimate] = field(default_factory=list)
    mean_derived: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    # ---- 基础判定 -------------------------------------------------------
    @property
    def has_climate(self) -> bool:
        return bool(self.months)

    def has_any_value(self, keys: Iterable[str]) -> bool:
        for mo in self.months:
            for k in keys:
                if mo.value(k) is not None:
                    return True
        return False

    # ---- 取值 -----------------------------------------------------------
    def values(self, key: str, temp_unit: str = "C", rain_unit: str = "mm") -> list[Optional[float]]:
        return [mo.value(key, temp_unit, rain_unit) for mo in self.months]

    def month_labels(self, style: str = "1月") -> list[str]:
        return [month_label(mo.month, style) for mo in self.months]

    def annual(self, key: str, temp_unit: str = "C", rain_unit: str = "mm") -> Optional[float]:
        """年值：温度取有效月均值，降水/降水日数取有效月累加。"""
        key = normalize_series_key(key)
        vals = [v for v in self.values(key, temp_unit, rain_unit) if v is not None]
        if not vals:
            return None
        if key in RAIN_KEYS:
            return sum(vals)
        return sum(vals) / len(vals)

    # ---- 文案 -----------------------------------------------------------
    def rain_label(self, sentence: bool = False) -> str:
        """按 raintype 给出降水元素名称；实测有 Rainfall / PPT / 空串 三种。"""
        rt = (self.raintype or "").strip().lower()
        base = "降水"
        if sentence:
            return "平均总降水"
        if rt in ("ppt", "precipitation"):
            base = "降水"
        return base

    def rain_unit_label(self) -> str:
        """降水单位文案；数据源 rainunit 可能为空。"""
        unit = (self.rain_unit or "").strip().lower()
        if unit in ("inch", "in", "inches"):
            return "英寸"
        return "毫米"

    def temp_unit_label(self) -> str:
        return "°C"

    def period_note(self) -> str:
        return self.period.note()

    def source_note(self) -> str:
        org = self.member.org_name or self.member.mem_name
        return f"数据来源：世界天气信息服务网（WMO）{('· ' + org) if org else ''}"

    def location_label(self, cfg: Optional[dict[str, Any]] = None) -> str:
        """经纬度成对文案（纬度在前），按 ``data.coord`` 配置格式化。"""
        return coord_pair(self.latitude, self.longitude, cfg)

    # ---- 序列化 ---------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "cityId": self.city_id,
            "cityName": self.city_name,
            "stationName": self.station_name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timeZone": self.time_zone,
            "isCapital": self.is_capital,
            "member": {
                "memId": self.member.mem_id,
                "memName": self.member.mem_name,
                "orgName": self.member.org_name,
            },
            "raintype": self.raintype,
            "rainUnit": self.rain_unit,
            "periodNote": self.period_note(),
            "meanTempDerived": self.mean_derived,
            "months": [
                {
                    "month": mo.month,
                    "minTemp": mo.min_temp,
                    "maxTemp": mo.max_temp,
                    "meanTemp": mo.mean_temp,
                    "meanTempDerived": mo.mean_temp_derived,
                    "minTempF": mo.min_temp_f,
                    "maxTempF": mo.max_temp_f,
                    "meanTempF": mo.mean_temp_f,
                    "rainfall": mo.rainfall,
                    "raindays": mo.rain_days,
                    "sourceDate": mo.source_date,
                }
                for mo in self.months
            ],
        }


# ---- 经纬度显示 --------------------------------------------------------
#: 方向符号：(正方向, 负方向)——南纬/西经为负。``{lat}`` 取 N/S，``{lon}`` 取 E/W。
COORD_DIRECTION_LABELS: dict[str, tuple[dict[str, str], dict[str, str]]] = {
    "lat": ({"letter": "N", "hanzi": "北"}, {"letter": "S", "hanzi": "南"}),
    "lon": ({"letter": "E", "hanzi": "东"}, {"letter": "W", "hanzi": "西"}),
}

#: ``data.coord`` 的默认值（与 ``config_loader.DEFAULTS`` 保持一致）
COORD_DEFAULTS: dict[str, Any] = {
    "style": "direction",     # direction(带方向符号) | signed(纯数字，西经/南纬为负)
    "direction": "letter",    # letter(E/W/N/S) | hanzi(东/西/南/北)
    "unit": True,             # 是否带单位
    "unit_text": "°",         # 单位文案：° 或 度
    "decimals": 2,            # 小数位
}

#: 取值别名（配置里也允许写中文），供校验与格式化共用
COORD_STYLE_ALIASES = {"direction": "direction", "带方向": "direction",
                       "signed": "signed", "纯数字": "signed"}
COORD_DIRECTION_ALIASES = {"letter": "letter", "字母": "letter",
                           "hanzi": "hanzi", "汉字": "hanzi"}


def normalize_coord_option(value: Any, aliases: dict[str, str], default: str = "") -> str:
    """把 ``data.coord`` 里的取值（含中文别名、大小写）归一为规范键；无法识别返回 default。"""
    text = str(value).strip()
    if text in aliases:
        return aliases[text]
    return aliases.get(text.lower(), default)


def coord_options(cfg: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """取出 ``data.coord`` 配置并按默认值补齐（缺项 / 旧配置都能用）。"""
    options = dict(COORD_DEFAULTS)
    node = ((cfg or {}).get("data") or {}).get("coord")
    if isinstance(node, dict):
        options.update({k: v for k, v in node.items() if v is not None})
    return options


def format_coord(value: Optional[float], kind: str, cfg: Optional[dict[str, Any]] = None) -> str:
    """把**数值**经纬度按 ``data.coord`` 配置格式化为可显示文案。

    ``kind`` 取 ``"lat"``（北/南、N/S）或 ``"lon"``（东/西、E/W）；值为 ``None``
    （数据源缺经纬度）时返回空串。方向符号一律在数字之后：

    ====================  ==============================
    ``direction`` + 字母  ``106.87°E`` / ``33.87°S``
    ``direction`` + 汉字  ``106.87°东`` / ``33.87°南``
    ``direction`` 不带单位 ``106.87E``
    ``signed``（纯数字）  ``106.87°`` / ``-33.87°``（西经、南纬为负）
    单位为汉字            ``106.87度E`` / ``33.87度南``
    ====================  ==============================
    """
    if value is None:
        return ""
    options = coord_options(cfg)
    try:
        decimals = max(0, int(options.get("decimals", 2)))
    except (TypeError, ValueError):
        decimals = 2
    unit = ""
    if options.get("unit", True):
        unit = str(options.get("unit_text", "°") or "")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if normalize_coord_option(options.get("style"), COORD_STYLE_ALIASES, "direction") == "signed":
        return f"{number:.{decimals}f}{unit}"
    sides = COORD_DIRECTION_LABELS["lat" if str(kind).lower().startswith("lat") else "lon"]
    label = sides[0] if number >= 0 else sides[1]
    if normalize_coord_option(options.get("direction"), COORD_DIRECTION_ALIASES,
                              "letter") == "hanzi":
        return f"{abs(number):.{decimals}f}{unit}{label['hanzi']}"
    return f"{abs(number):.{decimals}f}{unit}{label['letter']}"


def coord_pair(latitude: Optional[float], longitude: Optional[float],
               cfg: Optional[dict[str, Any]] = None, separator: str = ", ") -> str:
    """经纬度成对文案（**纬度在前**）；任一缺失返回空串。"""
    lat = format_coord(latitude, "lat", cfg)
    lon = format_coord(longitude, "lon", cfg)
    if not lat or not lon:
        return ""
    return f"{lat}{separator}{lon}"


def city_display_name(city: Any, cfg: Optional[dict[str, Any]] = None) -> str:
    """城市展示名：``data.city_short_name`` 为真时取逗号（半角/全角均可）前第一段，否则完整名称。

    用于标题、文件名、对比图例等展示场景；名称不含逗号时短名与全名一致。返回空串兜底。
    """
    name = str(getattr(city, "city_name", "") or "")
    if cfg and ((cfg.get("data") or {}).get("city_short_name")):
        for sep in (",", "，"):
            if sep in name:
                return name.split(sep, 1)[0].strip()
    return name


# ---- 月份标签 ----------------------------------------------------------
_ZH_MONTHS_NUM = [f"{i}月" for i in range(1, 13)]
_ZH_MONTHS_CN = ["一月", "二月", "三月", "四月", "五月", "六月",
                 "七月", "八月", "九月", "十月", "十一月", "十二月"]
_EN_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def month_label(month: int, style: str = "1月") -> str:
    """月份标签样式：1月 / 一月 / Jan / 01。"""
    idx = max(1, min(12, int(month))) - 1
    if style in ("一月", "cn"):
        return _ZH_MONTHS_CN[idx]
    if style.lower() in ("jan", "en", "english"):
        return _EN_MONTHS[idx]
    if style == "01":
        return f"{idx + 1:02d}"
    return _ZH_MONTHS_NUM[idx]


def format_number(value: Optional[float], precision: int = 1, missing: str = "\u2014") -> str:
    """按精度格式化，None 用占位符。"""
    if value is None:
        return missing
    if precision <= 0:
        return f"{value:.0f}"
    return f"{value:.{precision}f}"
