"""侦察脚本：探测 WMO 城市气候数据文件(cityId_zh.xml)的字段完整性与变体情况。

用途（可复用）：
  python recon_probe.py <country.json路径> [抽样数量]
输出：
  - 全球城市总数 / 国家数
  - 抽样城市的 climate 字段存在性矩阵（minTemp/maxTemp/meanTemp/rainfall/raindays/raintype）
  - 字段缺失或特殊值(如 'NULL')的样例清单

结论会写入 _recon_probe_report.txt，供设计解析器的容错逻辑。
"""
import json
import sys
import time
import random
import urllib.request
import urllib.error
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json,text/xml,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://worldweather.wmo.int/zh/index.html",
}


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8-sig", errors="replace")


def main():
    src = Path(sys.argv[1])
    sample_n = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    data = json.loads(src.read_text(encoding="utf-8-sig"))
    raw_members = data["member"]
    # 索引文件里 member 是 { "0": {...}, "1": {...}, "lang": "zh" } 形式的字典，兼容列表形式
    if isinstance(raw_members, dict):
        members = [v for v in raw_members.values() if isinstance(v, dict) and "memId" in v]
    else:
        members = [v for v in raw_members if isinstance(v, dict) and "memId" in v]
    out = []
    out.append(f"member 数量: {len(members)}")
    out.append(f"member keys: {sorted(members[0].keys())}")
    city_keys = set()
    for m in members:
        for c in (m.get("city") or []):
            city_keys.update(c.keys())
    out.append(f"city 条目字段: {sorted(city_keys)}")

    pairs = []  # (memName, cityId, cityName)
    for m in members:
        for c in m.get("city") or []:
            pairs.append((m.get("memName"), c.get("cityId"), c.get("cityName")))
    out.append(f"全球城市总数: {len(pairs)}")
    out.append(f"城市样例: {pairs[:5]}")

    random.seed(7)
    sample = random.sample(pairs, min(sample_n, len(pairs)))

    tmp = Path(__file__).parent / "_probe_tmp"
    tmp.mkdir(exist_ok=True)

    raintypes = {}
    stats = {"meanTemp_present": 0, "meanTemp_null": 0,
             "rainfall_null": 0, "raindays_null": 0, "raindays_NULL_str": 0,
             "climate_missing": 0, "months_count": {}, "ok": 0}
    anomalies = []

    for mem, cid, cname in sample:
        url = f"https://worldweather.wmo.int/zh/json/{cid}_zh.xml"
        try:
            raw = fetch(url)
        except Exception as e:  # noqa: BLE001
            anomalies.append(f"{cid} {cname} 下载失败: {e}")
            continue
        (tmp / f"{cid}.json").write_text(raw, encoding="utf-8")
        try:
            d = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            anomalies.append(f"{cid} {cname} JSON解析失败: {e}")
            continue
        city = d.get("city", d)
        cl = city.get("climate")
        if not cl or not cl.get("climateMonth"):
            stats["climate_missing"] += 1
            anomalies.append(f"{cid} {cname} 无气候数据")
            continue
        stats["ok"] += 1
        rt = cl.get("raintype")
        raintypes[rt] = raintypes.get(rt, 0) + 1
        months = cl["climateMonth"]
        stats["months_count"][len(months)] = stats["months_count"].get(len(months), 0) + 1
        for mo in months:
            if mo.get("meanTemp") in (None, ""):
                stats["meanTemp_null"] += 1
            else:
                stats["meanTemp_present"] += 1
            if mo.get("rainfall") in (None, ""):
                stats["rainfall_null"] += 1
            if mo.get("raindays") in (None, ""):
                stats["raindays_null"] += 1
            if str(mo.get("raindays")).upper() == "NULL":
                stats["raindays_NULL_str"] += 1
        out.append(f"  [{cid}] {cname} raintype={rt} rainunit={cl.get('rainunit')} "
                   f"months={len(months)} clino={cl.get('climatefromclino')} "
                   f"datab={cl.get('datab')} datae={cl.get('datae')} "
                   f"tempb={cl.get('tempb')} tempe={cl.get('tempe')}")

    out.append("")
    out.append(f"抽样结果: ok={stats['ok']} 无气候数据={stats['climate_missing']} raintype分布={raintypes}")
    out.append(f"meanTemp 有值/为空: {stats['meanTemp_present']}/{stats['meanTemp_null']}")
    out.append(f"rainfall 空值: {stats['rainfall_null']}  raindays 空值: {stats['raindays_null']} "
               f"raindays=='NULL'字符串: {stats['raindays_NULL_str']}")
    out.append(f"月份数分布: {stats['months_count']}")
    out.append("异常/注记:")
    out.extend("  " + a for a in anomalies)

    (Path(__file__).parent / "_recon_probe_report.txt").write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out))


if __name__ == "__main__":
    main()
