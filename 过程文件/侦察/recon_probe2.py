"""侦察脚本 2：确认边界行为与挑选用例城市。

用途（可复用）：
  python recon_probe2.py <country.json路径>
输出：
  - 无效 cityId 的 HTTP 响应行为（用于设计错误处理）
  - 中国/中国香港/中国台湾等城市清单（用于人工核对与演示）
  - 带气候数据且 raintype 不同的代表城市
"""
import json
import sys
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

out = []

# 1) 无效 cityId 行为
for cid in (999999, 0, 123456):
    url = f"https://worldweather.wmo.int/zh/json/{cid}_zh.xml"
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
        out.append(f"cityId={cid} -> HTTP {r.status} len={len(body)} head={body[:120]!r}")
    except urllib.error.HTTPError as e:
        out.append(f"cityId={cid} -> HTTPError {e.code} {e.reason}")
    except Exception as e:  # noqa: BLE001
        out.append(f"cityId={cid} -> {type(e).__name__}: {e}")

# 2) 城市清单
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
raw = data["member"]
members = [v for v in (raw.values() if isinstance(raw, dict) else raw)
           if isinstance(v, dict) and "memId" in v]
out.append("")
out.append("== 与中国相关的成员 ==")
for m in members:
    if any(k in (m.get("memName") or "") for k in ("中国", "香港", "澳门", "台湾")):
        cities = [(c.get("cityId"), c.get("cityName")) for c in (m.get("city") or [])]
        out.append(f"memId={m['memId']} {m.get('memName')} 城市数={len(cities)}")
        out.append("   " + ", ".join(f"{i}:{n}" for i, n in cities[:14]))

out.append("")
out.append("== 前 6 个成员的城市样例 ==")
for m in members[:6]:
    cities = [(c.get("cityId"), c.get("cityName")) for c in (m.get("city") or [])]
    out.append(f"{m.get('memName')}: {cities[:6]}")

(Path(__file__).parent / "_recon_probe2_report.txt").write_text("\n".join(out), encoding="utf-8")
