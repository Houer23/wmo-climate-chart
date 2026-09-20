"""城市索引：按城市名反查 cityId。

索引来源：``{lang}/json/Country_{lang}.xml``（实测约 1.1 MB，210 个成员国 / 3629 个城市）。
结构为 ``{"member": {"0": {...}, "1": {...}, "lang": "zh"}}`` —— 注意 ``member`` 是**字典**
且含非城市项（``lang``），需按 ``memId`` 过滤。

首次拉取后归一化为扁平列表缓存到本地，后续秒级加载。
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .http_client import HttpClient, city_index_url
from .parser import parse_int


class CityLookupError(RuntimeError):
    """按名称找不到城市。"""


@dataclass
class CityEntry:
    city_id: int
    city_name: str
    en_name: str
    station_name: str
    mem_id: Optional[int]
    mem_name: str
    latitude: Optional[float]
    longitude: Optional[float]
    time_zone: str
    is_capital: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "cityId": self.city_id, "cityName": self.city_name, "enName": self.en_name,
            "stationName": self.station_name, "memId": self.mem_id, "memName": self.mem_name,
            "latitude": self.latitude, "longitude": self.longitude,
            "timeZone": self.time_zone, "isCapital": self.is_capital,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CityEntry":
        return cls(
            city_id=int(payload.get("cityId") or 0),
            city_name=str(payload.get("cityName") or ""),
            en_name=str(payload.get("enName") or ""),
            station_name=str(payload.get("stationName") or ""),
            mem_id=payload.get("memId"),
            mem_name=str(payload.get("memName") or ""),
            latitude=payload.get("latitude"),
            longitude=payload.get("longitude"),
            time_zone=str(payload.get("timeZone") or ""),
            is_capital=bool(payload.get("isCapital")),
        )


class CityIndex:
    """城市索引（含本地缓存与模糊查找）。"""

    def __init__(self, entries: list[CityEntry], logger=None) -> None:
        self.entries = entries
        self.logger = logger
        self._by_id = {e.city_id: e for e in entries}

    def __len__(self) -> int:
        return len(self.entries)

    def by_id(self, city_id: int) -> Optional[CityEntry]:
        return self._by_id.get(city_id)

    def countries(self) -> list[str]:
        return sorted({e.mem_name for e in self.entries if e.mem_name})

    def list_by_country(self, name: str) -> list[CityEntry]:
        key = name.strip()
        return [e for e in self.entries if e.mem_name == key or key in e.mem_name]

    def search(self, query: str, limit: int = 10) -> list[CityEntry]:
        """按名称检索，按匹配强度排序：完全相同 > 前缀 > 包含 > 英文匹配 > 近似。"""
        q = query.strip()
        if not q:
            return []
        lowered = q.lower()
        scored: dict[int, tuple[int, CityEntry]] = {}

        for entry in self.entries:
            score: Optional[int] = None
            name = entry.city_name
            station = entry.station_name
            en = entry.en_name.lower()

            if name == q or station == q:
                score = 100
            elif name.startswith(q) or station.startswith(q):
                score = 80
            elif q in name or q in station:
                score = 60
            elif lowered and (lowered == en):
                score = 55
            elif lowered and (en.startswith(lowered) or lowered in en):
                score = 40
            if score is None:
                continue
            # 首都优先、有坐标优先
            if entry.is_capital:
                score += 3
            if entry.latitude is not None:
                score += 1
            current = scored.get(entry.city_id)
            if current is None or score > current[0]:
                scored[entry.city_id] = (score, entry)

        if not scored:
            names = [e.city_name for e in self.entries]
            close = difflib.get_close_matches(q, names, n=limit, cutoff=0.6)
            return [e for e in self.entries if e.city_name in close][:limit]

        ordered = sorted(scored.values(), key=lambda item: (-item[0], item[1].city_name))
        return [entry for _, entry in ordered[:limit]]

    def resolve_one(self, query: str) -> CityEntry:
        """解析唯一城市；歧义时抛出带候选清单的错误。"""
        matches = self.search(query, limit=12)
        if not matches:
            raise CityLookupError(f"找不到城市「{query}」。可用 --list-cities 查看，或改用 --city-id")
        exact = [m for m in matches if m.city_name == query.strip()]
        if len(exact) == 1:
            return exact[0]
        best = matches[0]
        if len(matches) > 1 and matches[1].city_name != best.city_name:
            alternatives = "、".join(f"{m.city_name}({m.city_id}, {m.mem_name})" for m in matches[:5])
            # 名字完全相同只是不同国家时才报歧义
            same_name = [m for m in matches if m.city_name == best.city_name]
            if len(same_name) == 1 and best.city_name.startswith(query.strip()):
                return best
            if len(same_name) > 1:
                raise CityLookupError(
                    f"「{query}」匹配到多个同名城市，请改用 --city-id：{alternatives}"
                )
        return best


def _flatten(payload: Any) -> list[CityEntry]:
    """把 Country_zh.xml 的嵌套结构压平为城市清单。"""
    if not isinstance(payload, dict):
        return []
    raw_members = payload.get("member", payload)
    if isinstance(raw_members, dict):
        members = [v for v in raw_members.values() if isinstance(v, dict) and "memId" in v]
    elif isinstance(raw_members, list):
        members = [v for v in raw_members if isinstance(v, dict) and "memId" in v]
    else:
        members = []

    entries: list[CityEntry] = []
    for member in members:
        cities = member.get("city") or []
        if isinstance(cities, dict):
            cities = [v for v in cities.values() if isinstance(v, dict)]
        for city in cities:
            if not isinstance(city, dict):
                continue
            city_id = parse_int(city.get("cityId"))
            if not city_id:
                continue
            entries.append(CityEntry(
                city_id=city_id,
                city_name=str(city.get("cityName") or "").strip(),
                en_name=str(city.get("enName") or "").strip(),
                station_name=str(city.get("stationName") or "").strip(),
                mem_id=parse_int(member.get("memId")),
                mem_name=str(member.get("memName") or "").strip(),
                latitude=parse_int(city.get("cityLatitude")) if False else _num(city.get("cityLatitude")),
                longitude=_num(city.get("cityLongitude")),
                time_zone=str(city.get("timeZone") or ""),
                is_capital=bool(city.get("isCapital")),
            ))
    entries.sort(key=lambda e: (e.mem_name, e.city_name))
    return entries


def _num(value: Any) -> Optional[float]:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def load_city_index(client: HttpClient, cfg: dict[str, Any], cache_file: Path,
                    logger=None, refresh: bool = False) -> CityIndex:
    """加载城市索引：优先读归一化缓存，其次读原始响应，最后联网拉取。"""
    if not refresh and cache_file.exists():
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            entries = [CityEntry.from_dict(item) for item in payload.get("cities") or []]
            if entries:
                if logger:
                    logger.debug(f"城市索引命中缓存：{cache_file}（{len(entries)} 个城市）")
                return CityIndex(entries, logger=logger)
        except (OSError, ValueError, KeyError):
            if logger:
                logger.warning("城市索引缓存损坏，将重新拉取")

    url = city_index_url(cfg["fetch"], cfg["data"].get("lang", "zh"))
    # 进度说明交给请求层统一打（「请求：城市索引」），这里不再重复一条 INFO
    result = client.get(url, accept="application/json,text/xml,*/*;q=0.8", note="城市索引")
    try:
        payload = json.loads(result.text.lstrip("\ufeff"))
    except ValueError as exc:
        raise CityLookupError(f"城市索引不是合法 JSON：{exc}") from exc

    entries = _flatten(payload)
    if not entries:
        raise CityLookupError("城市索引解析后为空，站点结构可能已变化")

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        json.dumps({"cities": [e.to_dict() for e in entries]}, ensure_ascii=False),
        encoding="utf-8",
    )
    if logger:
        logger.info(f"城市索引已缓存：{len(entries)} 个城市 → {cache_file}")
    return CityIndex(entries, logger=logger)
