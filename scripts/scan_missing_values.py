"""扫描抽样城市数据，列出含缺失值/异常字段的城市。

用途（可复用）：
    python scripts/scan_missing_values.py

数据来源：
    tests/fixtures/samples/*.json —— 由 过程文件/侦察/recon_probe.py 抽样下载的原始响应
    （见 过程文件/侦察/说明.md）。补充新样本后重跑本脚本即可核查数据质量。
"""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "tests" / "fixtures" / "samples"


def main() -> int:
    rows: list[str] = []
    for path in sorted(glob.glob(str(SAMPLES / "*.json"))):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))["city"]
        except Exception:  # noqa: BLE001 - 样本损坏时跳过
            continue
        climate = data.get("climate") or {}
        months = climate.get("climateMonth") or []
        if not months:
            continue

        def missing(key: str) -> int:
            return sum(1 for m in months if m.get(key) in (None, "", "NULL"))

        rf, rd, mn, mx = (missing("rainfall"), missing("raindays"),
                          missing("minTemp"), missing("maxTemp"))
        if rf or rd or mn or mx:
            rows.append(
                f"{os.path.basename(path):<12} {data.get('cityName', '?'):<10} "
                f"raintype={climate.get('raintype')!r:<10} "
                f"rainfall缺失={rf} raindays缺失={rd} min缺失={mn} max缺失={mx}"
            )

    print("有缺失值的城市：")
    print("\n".join(rows) if rows else "（无）")
    print()
    print("全部抽样城市数：", len(glob.glob(str(SAMPLES / "*.json"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
