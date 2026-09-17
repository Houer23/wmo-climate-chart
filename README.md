# WMO 城市气候数据解析与统计图生成工具

按城市编号（cityId）解析**世界天气信息服务网**（World Weather Information Service, WMO）的城市气候资料，
生成**表格**（CSV / Markdown / XLSX / JSON）与**气温-降水统计图**（PNG / SVG / PDF）。
图表元素、配色、双轴标题与位置、字体字号、画布尺寸、标题与图例、网格、刻度、表格字段等**全部参数可配置**，
支持**多套配置并存**并按需选用，未指定时使用默认配置。

---

## 1. 环境与安装

```bash
# 依赖：matplotlib（含 numpy）+ openpyxl；网络请求仅用 Python 标准库
pip install -r requirements.txt
```

Python 3.10+（开发验证于 3.13）。中文字体依赖系统字体，Windows 默认的 `Microsoft YaHei` / `SimHei` 已足够。

---

## 2. 快速开始

```bash
# 北京（cityId 237）：出表格 + 统计图，使用默认配置
python wmo_climate.py --city-id 237

# 按城市名自动反查 cityId
python wmo_climate.py --city 北京 --city 香港

# 换一套配置
python wmo_climate.py --city-id 237 --profile presentation

# 临时改参数（点路径覆盖）
python wmo_climate.py --city-id 237 --set series.rainfall.color=#ff7f0e --set figure.title.show=false

# 多城市对比
python wmo_climate.py --compare 北京 --compare 1 --compare 156 --profile compare

# 查城市编号
python wmo_climate.py --search 北京
python wmo_climate.py --list-cities --country 中国
```

---

## 3. 命令行参数

| 参数 | 说明 |
|---|---|
| `--city-id ID` | 城市编号，可重复传入实现批量 |
| `--city 名称` | 城市名称，自动反查 cityId，可重复 |
| `--compare ID或名称` | 多城市对比，可重复；同时传多个 cityId 或城市名 |
| `--compare-metric 元素` | 对比元素：`minTemp`/`maxTemp`/`meanTemp`/`rainfall`/`raindays` |
| `--profile 名称` | 选用配置；**未指定则用默认配置** |
| `--profiles-file 路径` | 使用外部 profiles 文件，不污染项目内置配置 |
| `--set 键=值` | 点路径覆盖配置，如 `series.rainfall.color=#ff0000`，可重复 |
| `--list-profiles` | 列出全部可用配置及说明 |
| `--show-config` | 打印解析后的最终配置（便于确认合并结果） |
| `--init-profile 名称` | 导出全量配置模板到 `config/<名称>.json` |
| `--temp-unit C\|F` | 温度单位（等价于 `--set data.temp_unit=`） |
| `--rain-unit mm\|inch` | 降水单位 |
| `--out-dir 目录` | 输出目录 |
| `--no-chart` / `--no-table` | 只出表格 / 只出图 |
| `--refresh-cache` | 忽略本地缓存强制重新请求 |
| `--refresh-index` | 强制重新拉取城市索引 |
| `--log-level LEVEL` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `--search 关键词` / `--list-cities` / `--country 国家` / `--limit N` | 城市编号查询 |

退出码：`0` 全部成功；`1` 部分城市失败（其余仍正常产出）；`2` 配置错误；`3` 城市查询/网络失败。

---

## 4. 数据源与实测约束

数据源有两个地址，工具两者都会用：

| 用途 | 地址 |
|---|---|
| 城市页面（存在性校验、页面标题） | `https://worldweather.wmo.int/{lang}/city.html?cityId={cityId}` |
| 气候数据（真正取数） | `https://worldweather.wmo.int/{lang}/json/{cityId}_{lang}.xml` |
| 城市索引（名称反查） | `https://worldweather.wmo.int/{lang}/json/Country_{lang}.xml` |

> **重要**：城市页面本身**不含气候数值**（页面 HTML 仅含前端渲染脚本），
> 气候数据由页面脚本异步加载 `{cityId}_{lang}.xml` 得到。该文件内容**实为 JSON**，
> 响应头标 `application/xml`，且带 UTF-8 BOM。

实测发现并已在代码中处理的约束：

| 现象 | 处理方式 |
|---|---|
| `climateMonth[].meanTemp` 恒为 `null`（抽样 252/252） | 由 `(minTemp + maxTemp) / 2` 派生，可用 `data.derive_mean_temp` 关闭 |
| `raintype` 有 `Rainfall` / `PPT` / 空串 三种变体 | 单位与元素名按 raintype 映射，并有兜底 |
| `rainunit` 可能为空串 | 回退为「毫米」 |
| `rainfall`、`raindays` 存在空值 | 统一为缺失值，按 `data.fill_missing` 策略处理；表格用占位符 |
| 约 30% 城市完全没有气候数据（抽样 30 城中 9 城） | 抛出明确原因，批量模式下不中断其他城市 |
| 无效 cityId 返回 HTTP 404 | 识别为「城市不存在」，不重试 |
| cityId > 600000 为 ECMWF 模式城市 | 无气候数据，给出明确提示 |
| 服务端存在**间歇性 TLS 断连**（`UNEXPECTED_EOF_WHILE_READING`） | 超时 + 指数退避重试（`fetch.retries` / `fetch.backoff`） |
| 请求头非强制但构造完整可提升稳定性 | 参见 `fetch.headers`（UA / Accept / Accept-Language / Connection 等均可配） |

---

## 5. 目录结构

```
DrawClimateChart/
├── wmo_climate.py            # CLI 主入口
├── requirements.txt
├── config/
│   ├── profiles.json         # 内置配置（default + 14 套预设 + 可复用样式）
│   ├── custom.json           # 自定义配置（单文件，如「简图」）
│   ├── custom/               # 自定义配置（多文件目录，按文件名顺序加载，可互继承）
│   └── README.md             # ★ 配置项完整字典
├── src/
│   ├── http_client.py        # 请求层：主请求头 / 超时 / 退避重试 / BOM 解码 / 缓存
│   ├── parser.py             # 解析层：容错 + 平均气温派生
│   ├── models.py             # 数据模型与单位换算
│   ├── city_index.py         # 城市名 → cityId 反查（3629 城）
│   ├── config_loader.py      # 配置：默认值 / 深合并 / 继承 / 样式 / 校验
│   ├── table_writer.py       # 表格：CSV / Markdown / XLSX / JSON
│   ├── chart.py              # 绘图：matplotlib 双轴（+第三轴），全参数可配
│   └── pipeline.py           # 编排：单城 / 批量 / 对比
├── scripts/                  # 可复用工具（夹具抓取 / CLI 验收 / 缺失值扫描）
├── tests/
│   ├── test_regression.py    # 回归测试（离线可跑，130 项断言）
│   ├── fixtures/             # 真实响应样本（含城市索引与 samples/ 抽样数据）
│   └── _output/              # 测试产物：渲染核对图 / 表格中间输出（不入库）
├── output/                   # 交付物：表格与图（生成物，不入库）
├── cache/                    # 运行缓存：HTTP 响应 / 城市索引 / 原始响应（不入库）
├── logs/                     # 运行日志与验收报告
└── 过程文件/                  # 开发过程留证（侦察证据）
```

---

## 6. 配置系统

四层来源，后者覆盖前者：

1. **内置默认值**（`src/config_loader.py` 的 `DEFAULTS`，含全部可配置项）
2. **配置文件中的命名配置**：内置 `config/profiles.json`
3. **自定义配置（多文件）**：`config/custom.json` + `config/custom/*.json`，按"上下顺序"加载，
   与内置配置合并共存（**同名时自定义优先**；可 `extends` 跨文件继承、可 `apply_styles` 复用样式、可只写要改的项）
4. **命令行 `--set 键=值`** 点路径覆盖

未指定 `--profile` 时使用 `profiles.json` 的 `default_profile`。

> **自定义配置**：写在 `config/custom.json` 或 `config/custom/*.json` 的 `profiles` 下（一配置一项），
> 与内置配置合并共存，无需改动 `profiles.json`；多文件时**后加载者覆盖先加载者**，
> 且任意文件中的配置都能 `extends` 更早文件中出现的同名配置。仓库已内置一个示例 `简图`。

### 6.1 内置预设

| 配置 | 说明 |
|---|---|
| `default` | 官方页面风格：日均最低/最高气温 + 降水柱，双轴 |
| `full` | 全元素：最低/最高/平均气温 + 降水柱 + 降水日数（**第三轴**） |
| `temp_only` | 纯气温图（含最高最低之间的区间带） |
| `rain_only` | 纯降水图（降水量柱 + 降水日数折线，单轴） |
| `presentation` | 投屏演示：大画布、大字号、粗线条、数据标签 |
| `paper` | 学术论文：白底、无图例边框、左对齐标题、附数据来源，输出 PDF/SVG |
| `compact` | 紧凑小图 |
| `minimal` | 极简：无标题/图例/网格/轴标题 |
| `seasonal` | 季节底色带 + 气温极值月标注 |
| `alt_bands` | 隔月底色 |
| `yellow_black` | 高对比配色 |
| `annual_table` | 表格增强：年列、转置、含 JSON 输出 |
| `fahrenheit` | 英制单位（°F + 英寸） |
| `compare` / `compare_rain` | 多城市对比（气温 / 降水柱） |

```bash
python wmo_climate.py --list-profiles          # 查看全部配置
python wmo_climate.py --init-profile my_style  # 导出全量模板后再改
```

### 6.2 自定义配置示例

> 自定义配置写在 **`config/custom.json`** 或 **`config/custom/*.json`** 的 `profiles` 下（可放任意多个），
> 无需复制内置文件；同名配置会覆盖内置同名项。多文件按文件名升序加载，后续文件可 `extends` 前文出现的配置：

```jsonc
{
  "profiles": {
    "my_style": {
      "extends": "default",
      "apply_styles": ["official_colors"],
      "figure": { "figsize": [14, 7], "title": { "text": "{city}气候 ({period})" } },
      "series": {
        "meanTemp": { "enabled": true, "chart_type": "smooth", "color": "#333333" },
        "rainfall": { "color": "#4a9fd8", "data_labels": { "show": true } }
      }
    }
  }
}
```

> 只写想改的项即可，其余自动继承。完整配置项见 **[config/README.md](config/README.md)**。

---

## 7. 输出

默认文件名模板：**图片**用 `{city}_{city_id}_climate_{profile}`（**以配置名作后缀**，如 `北京_237_climate_简图.png`），
**表格**用 `{city}_{city_id}_climate`（不带后缀，如 `北京_237_climate.xlsx`）。
分别由 `output.name_template` / `output.table_name_template` 调整
（占位符：`{city}` `{city_id}` `{member}` `{station}` `{profile}`；对比图另有 `{city_count}` `{metric}`）。

- **表格**：CSV（UTF-8 BOM，Excel 直接打开不乱码）、Markdown、XLSX、JSON
- **图**：PNG（默认）/ SVG / PDF；SVG 与 PDF 中的文字保持为**可编辑文本对象**，可直接在 Illustrator / Inkscape 中改字（`figure.svg_fonttype` / `figure.pdf_fonttype` 可切换为轮廓路径）
- 覆盖策略 `output.overwrite`：`overwrite` / `skip` / `timestamp`
- **运行信息**：日志与结果汇总会列出每个城市**本次实际绘出的要素**（如「绘出要素：日均最高气温、日均最低气温、平均总降水」），已扣除配置禁用、所属轴关闭与无数据自动降级的元素；对比模式另给出「对比要素」。

已完成示例见 `output/` 目录。

---

## 8. 测试

```bash
python tests/test_regression.py            # 离线：用 tests/fixtures 中的真实响应样本
python tests/test_regression.py --network  # 追加联网用例
```

覆盖：数值容错、平均气温派生、raintype 三种变体、缺失值、无气候数据城市、
配置深合并与继承、非法配置报错、四种表格格式与转置/年列/单位变体、
**全部配置逐一渲染**、三轴渲染、多城市对比、城市名反查。

---

## 9. 已知限制

- 数据源只提供**月气候平均值**（气候态），不含逐年历史序列；`meanTemp` 需派生。
- 城市页面不含数据，因此「页面解析」实为**存在性校验 + 数据接口定位**，数值一律取自数据文件。
- 第三轴（降水日数）仅有右轴刻度，不参与主轴对齐，适合并置观感展示。
- `--compare` 只支持单一元素对比；不同城市统计时段可能不同，对比图中会在副标题列出各自时段。
