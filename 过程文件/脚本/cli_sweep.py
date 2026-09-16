"""CLI 全子命令验收：把每个开关都真实跑一遍，结果写入 _cli_sweep.txt。

用途（可复用）：python 过程文件/脚本/cli_sweep.py
注意：包含联网请求（首个城市会用缓存），用于端到端验收。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
MAIN = ROOT / "wmo_climate.py"

CASES: list[tuple[str, list[str]]] = [
    ("列出全部配置", ["--list-profiles"]),
    ("查看默认配置", ["--show-config"]),
    ("查看 presentation 配置(节选)", ["--show-config", "--profile", "presentation"]),
    ("导出全量配置模板", ["--init-profile", "my_style"]),
    ("搜索城市名", ["--search", "北京", "--limit", "5"]),
    ("按国家列城市", ["--list-cities", "--country", "中国香港", "--limit", "5"]),
    ("城市名反查并出图", ["--city", "北京", "--out-dir", "output/cli_sweep/name_lookup"]),
    ("批量多城市(含非法ID)", ["--city-id", "237", "--city-id", "1", "--city-id", "999999",
                              "--out-dir", "output/cli_sweep/batch"]),
    ("多城市对比", ["--compare", "北京", "--compare", "1", "--compare", "156",
                    "--profile", "compare", "--out-dir", "output/cli_sweep/compare"]),
    ("对比-降水柱", ["--compare", "237", "--compare", "1", "--compare", "156",
                     "--profile", "compare_rain", "--out-dir", "output/cli_sweep/compare_rain"]),
    ("临时覆盖参数", ["--city-id", "237", "--profile", "full",
                      "--set", "series.rainfall.color=#ff7f0e",
                      "--set", "figure.title.show=false",
                      "--set", "figure.legend.loc=lower center",
                      "--out-dir", "output/cli_sweep/set_override"]),
    ("英制单位", ["--city-id", "1", "--profile", "fahrenheit",
                  "--out-dir", "output/cli_sweep/fahrenheit"]),
    ("仅表格不出图", ["--city-id", "237", "--no-chart",
                      "--out-dir", "output/cli_sweep/no_chart"]),
    ("仅出图不出表", ["--city-id", "237", "--no-table",
                      "--out-dir", "output/cli_sweep/no_table"]),
    ("无气候数据城市", ["--city-id", "2685", "--out-dir", "output/cli_sweep/no_climate"]),
    ("非法配置名", ["--city-id", "237", "--profile", "不存在的配置"]),
    ("非法单位", ["--city-id", "237", "--temp-unit", "K"]),
    ("DEBUG 日志", ["--city-id", "156", "--log-level", "DEBUG",
                    "--out-dir", "output/cli_sweep/debug_log"]),
]


def main() -> int:
    out: list[str] = []
    failures = 0
    for title, args in CASES:
        cmd = [PY, str(MAIN), *args]
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        status = "OK" if proc.returncode in (0, 1) else "FAIL"
        # 退出码 1 = 部分城市失败（预期），2/3 = 配置或查询错误（预期于对应用例）
        expected_error = title.startswith("非法")
        if expected_error:
            status = "OK" if proc.returncode == 2 else "FAIL"
        if status == "FAIL":
            failures += 1
        out.append("=" * 78)
        out.append(f"[{status}] {title}")
        out.append(f"$ python wmo_climate.py {' '.join(args)}")
        out.append(f"退出码：{proc.returncode}")
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        if stdout:
            body = stdout if len(stdout) < 3500 else stdout[:3500] + "\n…（输出已截断）"
            out.append("--- stdout ---")
            out.append(body)
        if stderr:
            out.append("--- stderr ---")
            out.append(stderr[:1200])
        out.append("")

    out.append("=" * 78)
    out.append(f"用例总数 {len(CASES)}，异常用例 {failures} 个")

    report = ROOT / "过程文件" / "_cli_sweep.txt"
    report.write_text("\n".join(out), encoding="utf-8")
    print(f"报告已写入：{report}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
