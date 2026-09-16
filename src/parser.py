"""解析层：把 WMO 城市数据文件（实为 JSON）转换为 ``CityClimate``。

容错清单（均有实测依据）
------------------------
* ``meanTemp`` 恒为 null → 用 (min+max)/2 派生。
* ``rainfall`` / ``raindays`` 可能为空串或 ``NULL`` → 统一为 None。
* ``raintype`` 有 ``Rainfall`` / ``PPT`` / 空串 三种变体。
* ``rainunit`` 可能为空串。
* 约 30% 的城市完全没有 ``climate`` 段，需要给出明确原因而不是崩掉。
* ``cityId > 600000`` 是 ECMWF 模式城市，数据文件路径与结构都不同。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .models import CityClimate, Member, MonthClimate, Period, normalize_series_key

MISSING_TOKENS = {"", "null", "none", "n/a", "na", "nan", "-", "--", "\u2014", "nil", "undefined"}


class ParseError(RuntimeError):
    """响应不是预期的数据结构。"""


class NoClimateDataError(ParseError):
    """城市存在但没有气候资料。"""


def parse_number(value: Any) -> Optional[float]:
    """尽力把任意标量转成 float；无法转换或表示缺失时返回 None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value != value:  # NaN
            return None
        return float(value)
    text = str(value).strip()
    if text.lower() in MISSING_TOKENS:
        return None
    text = text.replace(",", "").replace("\u00a0", " ")
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value: Any) -> Optional[int]:
    num = parse_number(value)
    return None if num is None else int(round(num))


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def unwrap_city(payload: Any) -> dict[str, Any]:
    """兼容两种外层结构：``{"city": {...}}`` 与直接 ``{...}``。"""
    if isinstance(payload, dict):
        city = payload.get("city")
        if isinstance(city, dict):
            return city
        if isinstance(city, list) and city and isinstance(city[0], dict):
            return city[0]
        return payload
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    raise ParseError("响应结构无法识别，缺少城市对象")


def parse_city(payload: Any, city_id: Optional[int] = None, lang: str = "zh") -> CityClimate:
    """解析为 CityClimate。缺少气候数据时抛 NoClimateDataError。"""
    city = unwrap_city(payload)

    member_raw = city.get("member") or {}
    if isinstance(member_raw, list):
        member_raw = member_raw[0] if member_raw else {}
    member = Member(
        mem_id=parse_int(member_raw.get("memId")),
        mem_name=_text(member_raw.get("memName")),
        org_name=_text(member_raw.get("orgName")),
        url=_text(member_raw.get("url")),
        logo=_text(member_raw.get("logo")),
    )

    resolved_id = parse_int(city.get("cityId")) or city_id or 0
    climate = city.get("climate")
    if not isinstance(climate, dict):
        raise NoClimateDataError(f"cityId={resolved_id} 的响应中没有 climate 字段")

    period = Period(
        temp_from=_text(climate.get("tempb")),
        temp_to=_text(climate.get("tempe")),
        data_from=_text(climate.get("datab")),
        data_to=_text(climate.get("datae")),
        rain_days_from=_text(climate.get("rdayb")),
        rain_days_to=_text(climate.get("rdaye")),
        rainfall_from=_text(climate.get("rainfallb")),
        rainfall_to=_text(climate.get("rainfalle")),
        clino=_text(climate.get("climatefromclino")),
    )

    months_raw = climate.get("climateMonth")
    if isinstance(months_raw, dict):  # 极少数情况下是字典
        months_raw = list(months_raw.values())
    if not isinstance(months_raw, list) or not months_raw:
        raise NoClimateDataError(
            f"cityId={resolved_id}（{_text(city.get('cityName'))}）没有气候月份数据"
        )

    months: list[MonthClimate] = []
    for idx, item in enumerate(months_raw, start=1):
        if not isinstance(item, dict):
            continue
        month = parse_int(item.get("month")) or idx
        mo = MonthClimate(
            month=month,
            min_temp=parse_number(item.get("minTemp")),
            max_temp=parse_number(item.get("maxTemp")),
            mean_temp=parse_number(item.get("meanTemp")),
            min_temp_f=parse_number(item.get("minTempF")),
            max_temp_f=parse_number(item.get("maxTempF")),
            mean_temp_f=parse_number(item.get("meanTempF")),
            rainfall=parse_number(item.get("rainfall")),
            rain_days=parse_number(item.get("raindays")),
            source_date=_text(item.get("climateFromMemDate")),
        )
        months.append(mo)

    if not months:
        raise NoClimateDataError(f"cityId={resolved_id} 的气候月份数据为空")

    # 数据源 meanTemp 实测恒为 null → 统一派生
    derived = False
    for mo in months:
        before = mo.mean_temp
        mo.fill_derived_mean()
        if before is None and mo.mean_temp is not None:
            derived = True

    months.sort(key=lambda m: m.month)

    return CityClimate(
        city_id=resolved_id,
        city_name=_text(city.get("cityName")),
        station_name=_text(city.get("stationName")),
        latitude=parse_number(city.get("cityLatitude")),
        longitude=parse_number(city.get("cityLongitude")),
        time_zone=_text(city.get("timeZone")),
        is_capital=bool(city.get("isCapital")),
        is_dep=bool(city.get("isDep")),
        lang=lang,
        member=member,
        raintype=_text(climate.get("raintype")),
        rain_unit=_text(climate.get("rainunit")),
        period=period,
        months=months,
        mean_derived=derived,
        raw=city,
    )


def parse_city_text(text: str, city_id: Optional[int] = None, lang: str = "zh") -> CityClimate:
    """从原始响应文本解析。"""
    text = text.lstrip("\ufeff").strip()
    if not text:
        raise ParseError("响应内容为空")
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise ParseError(f"响应不是合法 JSON：{exc}") from exc
    return parse_city(payload, city_id=city_id, lang=lang)


def available_series(city: CityClimate) -> dict[str, bool]:
    """判断哪些元素在该城市有可用数据（供配置自动降级使用）。"""
    result: dict[str, bool] = {}
    for key in ("minTemp", "maxTemp", "meanTemp", "rainfall", "raindays"):
        canon = normalize_series_key(key)
        result[canon] = city.has_any_value([canon])
    return result
