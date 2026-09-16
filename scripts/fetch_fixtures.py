"""抓取并保存测试夹具（真实的 WMO 响应样本）。

用途（可复用）：
    python scripts/fetch_fixtures.py
输出：
    tests/fixtures/<cityId>_<lang>.json   —— 原始响应（UTF-8 无 BOM）

选取的样本覆盖了实测发现的各类边界情况：
    237  北京      正常数据，raintype=PPT
    1    香港      正常数据，raintype=Rainfall（小数位降水）
    156  澳门      正常数据，raintype=Rainfall
    1007 新西伯利亚 PPT，且降水数值为 0 的月份
    2184 希洪      raintype 与 rainunit 均为空串
    2685 奥伦      完全没有气候数据（应优雅降级）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.http_client import HttpClient, city_data_url  # noqa: E402
from src.config_loader import load_config  # noqa: E402

SAMPLES = {
    237: "正常数据·PPT",
    1: "正常数据·Rainfall·小数降水",
    156: "正常数据·Rainfall",
    1007: "PPT·含 0 值月份",
    2184: "raintype/rainunit 为空串",
    2685: "无气候数据",
    500: "塔里：降水量与降水日数全缺（纯气温城市）",
    2034: "圣保罗：降水日数全缺",
    1729: "卢森格林：气温全缺（纯降水城市）",
}


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    fixtures = root / "tests" / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)

    cfg = load_config(None)
    client = HttpClient(cfg["fetch"], logger=None,
                        cache_dir=root / "cache")
    client.save_raw = False

    failed = []
    for city_id, note in SAMPLES.items():
        url = city_data_url(cfg["fetch"], city_id, "zh")
        try:
            result = client.get(url)
        except Exception as exc:  # noqa: BLE001
            failed.append((city_id, note, str(exc)))
            print(f"[失败] {city_id} {note}：{exc}")
            continue
        path = fixtures / f"{city_id}_zh.json"
        path.write_text(result.text, encoding="utf-8")
        print(f"[成功] {city_id} {note} → {path.name}（{len(result.text)} 字符）")

    if failed:
        print("\n以下样本抓取失败：")
        for city_id, note, err in failed:
            print(f"  {city_id} {note}: {err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
