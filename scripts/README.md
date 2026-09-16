# 项目脚本 · scripts/

可复用的处理与验收脚本（由 `过程文件/脚本/` 转正而来）。全部为独立可执行脚本，
内部自行定位项目根目录，可在任意工作目录下用 `python scripts/<脚本>` 调用。

| 脚本 | 作用 | 调用方式 | 是否可复用 |
|---|---|---|---|
| `fetch_fixtures.py` | 抓取真实 WMO 响应保存为测试夹具，覆盖各类边界（PPT/Rainfall/空 raintype、缺降水、缺气温、无气候数据） | `python scripts/fetch_fixtures.py` | ✅ **可复用**：站点数据更新或需补充样本时重跑 |
| `cli_sweep.py` | CLI 全子命令端到端验收（18 个用例：查询/批量/对比/覆盖参数/错误路径），报告写入 `过程文件/日志/_cli_sweep.txt` | `python scripts/cli_sweep.py` | ✅ **可复用**：改动 CLI 后回归 |
| `scan_missing_values.py` | 扫描 `tests/fixtures/samples/` 中的抽样数据，列出含缺失值/异常字段的城市 | `python scripts/scan_missing_values.py` | ✅ **可复用**：新增样本后核查数据质量 |

## 说明

- `fetch_fixtures.py` 会写 `tests/fixtures/`，这些夹具是回归测试的离线数据源；删除后需重跑该脚本才能离线测试。
- `cli_sweep.py` 依赖真实网络（首个城市会用缓存），退出码非 0 表示有用例异常；详细输出见 `过程文件/日志/_cli_sweep.txt`。
- `scan_missing_values.py` 的数据集为 `tests/fixtures/samples/*.json`（原 `过程文件/侦察/_probe_tmp/`），
  由 `过程文件/侦察/recon_probe.py` 抽样下载。
