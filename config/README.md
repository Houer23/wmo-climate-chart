# 配置项完整字典

本文件是 `config/profiles.yaml`、`config/custom.yaml` 与 `--set` 的**完整可配置项清单**。
配置一律使用 **YAML**（`.yaml` / `.yml`）。内置默认值定义在 `src/config_loader.py` 的 `DEFAULTS`，
任何时候都可用 `python wmo_climate.py --show-config` 打印**合并后的最终配置**（YAML）对照阅读。

**配置来源与优先级**（后者覆盖前者）：

1. 内置默认值 `DEFAULTS`（= 下方所有默认值）
2. 内置 `config/profiles.yaml` 选定的配置；`--profile <名称>`；未指定用 `default_profile`
3. 自定义配置（多文件）：`config/custom.yaml` + `config/custom/*.yaml` 按"上下顺序"加载并合并
   （**同名时自定义优先**；可 `extends` 跨文件继承）；`--profiles-file` 可替换为外部整套配置
4. 命令行 `--set 键=值`（可重复，点路径，值按 JSON/数字/布尔自动识别）

**自定义配置**：写在 `config/custom.yaml` 或 `config/custom/*.yaml` 的 `profiles` 下（一配置一项），
多文件按文件名升序加载（忽略隐藏文件与子目录）、后加载者覆盖先加载者，且可 `extends` 更早文件中的同名配置。
其中 `config/custom/*` 为本地配置目录，默认不入库（见 `.gitignore`，仅保留 `.gitkeep`）。示例：

```yaml
profiles:
  简图:
    description: 长画布（2:3），仅气温 + 降水，无标题，图例置底
    figure:
      figsize: [8, 12]
      title: {show: false}
    series:
      minTemp: {enabled: false}
      maxTemp: {enabled: false}
      meanTemp: {enabled: true, label: 气温, color: "#d62728"}
      rainfall: {label: 降水, color: "#1f77b4"}
    axes_primary: {label_text: 气温}
    axes_secondary: {label_text: 降水}
```

**合并规则**：对象递归深合并；数组整体替换（不是拼接）；因此局部配置只需写想改的项。

### YAML 语法要点

| 事项 | 说明 |
|---|---|
| 缩进 | 只能用**空格**（建议 2 个），不能用 Tab；同级键左对齐 |
| 层级 | 用缩进表示嵌套，替代 JSON 的大括号；可以任意深度混用行内写法 `{a: 1, b: 2}` 与 `[1, 2]` |
| 字符串 | 一般无需引号；含 `:` `#` `{` `}` `[` `]` `,` 或以数字/布尔开头时请加引号 |
| 类型陷阱 | `01`、`1.0`、`yes`/`no`/`on`/`off` 会被解析为数字/布尔，需按字面字符串用时请加引号（如 `month_label_style: "01"`） |
| 注释 | 以 `#` 开头（行内 `值  # 说明` 亦可），可随配置一起保留，便于写备注 |
| 多文档 | 不支持；一个文件一份 `profiles` / `styles` 结构 |
| 兼容 | JSON 是 YAML 的子集，旧 `.json` 配置仍可读（`--profiles-file x.json`、`config/custom.json`、`config/custom/*.json`），但优先级低于 YAML |

**配置内可用的继承与复用**：

| 键 | 说明 |
|---|---|
| `extends` | 继承另一套配置（可链式，循环会被检测并报错） |
| `apply_styles` | 引用 `styles` 中定义的命名样式（可复用的一组设置） |
| `styles`（顶层） | 命名样式集合，如 `official_colors`、`big_font`、`clean_paper` |

**占位符模板**（用于 `title.text`、`subtitle.text`、`credit.text`、文件名等）：

`{city}` 城市名 · `{city_id}` 编号 · `{member}` 国家/地区 · `{org}` 气象机构 ·
`{station}` 测站名 · `{period}` 统计时段 · `{lat}` `{lon}` 经纬度 ·
`{temp_unit}` 温度单位 · `{rain_unit}` 降水单位 · `{profile}` 配置名 ·
对比图另支持 `{metric}`（对比元素名）· `{city_count}`（城市数）

> `{lat}` / `{lon}` 的**显示文案由 `data.coord` 决定**（方向符号、单位、小数位），
> 数据本身始终是数值：例如默认输出 `39.93°N` / `116.28°E`，`style: signed` 时为
> `39.93°` / `116.28°`，南纬/西经为负。经纬度的先后顺序由模板自己决定（如 `{lon}/{lat}`）。

---

## A. `data` — 数据与字段口径

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `lang` | str | `"zh"` | 语言版本，决定 URL 路径（`zh`/`en`/`fr`…） |
| `derive_mean_temp` | bool | `true` | 数据源 `meanTemp` 恒为空，是否用 `(最高+最低)/2` 派生 |
| `temp_unit` | str | `"C"` | 温度单位：`C` / `F` |
| `rain_unit` | str | `"mm"` | 降水单位：`mm` / `inch` |
| `fill_missing` | str | `"gap"` | 缺失值策略：`gap` 断线 / `zero` 补 0 / `skip` 跳过 |
| `month_label_style` | str | `"1月"` | 月份标签：`1月` / `一月` / `Jan` / `01` |
| `include_annual` | bool | `false` | 表格与图表是否附加年值（温度取月均，降水取累加） |
| `auto_disable_empty_series` | bool | `true` | 某元素在该城市全无数据时自动不绘制（如纯气温城市自动去掉降水柱） |
| `coord.style` | str | `"direction"` | **经纬度显示方式**：`direction` 带方向符号（南纬/西经靠 S/W 区分） / `signed` 纯数字（西经、南纬为**负数**）；也接受中文 `带方向` / `纯数字`。作用于 `{lat}` `{lon}` 占位符与 `--list-cities` 坐标列 |
| `coord.direction` | str | `"letter"` | 方向符号形式（`coord.style=direction` 时生效）：`letter` = `E`/`W`/`N`/`S`（**默认**） / `hanzi` = `东`/`西`/`南`/`北`；也接受中文 `字母` / `汉字` |
| `coord.unit` | bool | `true` | 是否带单位 |
| `coord.unit_text` | str | `"°"` | 单位文案：默认为符号 `°`，可设为 `度` |
| `coord.decimals` | int | `2` | 小数位（≥ 0） |

**`coord` 输出示例**（经度 116.2833，纬度 -33.87，方向符号一律在数字之后）：

| 配置 | 经度 | 纬度 |
|---|---|---|
| 默认（`direction` + `letter` + `°`） | `116.28°E` | `33.87°S` |
| `direction: hanzi` | `116.28°东` | `33.87°南` |
| `unit: false` | `116.28E` | `33.87S` |
| `unit_text: 度` | `116.28度E` | `33.87度南` |
| `style: signed` | `116.28°` | `-33.87°` |
| `decimals: 4` | `116.2833°E` | `33.8700°S` |

## B. `fetch` — 请求层

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `base_url` | str | `"https://worldweather.wmo.int"` | 站点根地址 |
| `city_page_path` | str | `"{lang}/city.html?cityId={city_id}"` | 城市页面路径模板 |
| `data_path` | str | `"{lang}/json/{city_id}_{lang}.xml"` | 气候数据路径模板（内容实为 JSON） |
| `city_index_path` | str | `"{lang}/json/Country_{lang}.xml"` | 城市索引路径模板 |
| `fetch_page_first` | bool | `true` | 是否先请求 HTML 页做存在性校验（页面本身不含数据） |
| `timeout` | 秒 | `30` | 单次请求超时 |
| `retries` | int | `4` | 失败重试次数（应对实测的间歇性 TLS 断连） |
| `backoff` | 秒 | `1.2` | 退避基数，第 n 次重试等待 `backoff × 2^(n-1)` |
| `backoff_max` | 秒 | `15` | 单次退避上限 |
| `min_interval` | 秒 | `1.0` | **相邻网络请求的最小间隔**（进程内排队，含重试）；默认每秒最多 1 次请求，批量成图时对数据源保持礼貌；设 `0` 关闭限速 |
| `verify_ssl` | bool | `true` | 是否校验证书（不建议关闭） |
| `proxy` | str | `""` | 代理地址，如 `http://127.0.0.1:7890` |
| `save_raw` | bool | `false` | 是否把原始响应落到 `raw_dir` |
| `raw_dir` | path | `"cache/raw"` | 原始响应目录 |
| `headers` | obj | 见下 | **主请求头**，逐项可配 |
| `headers.User-Agent` | str | Chrome 125 UA | 浏览器 UA |
| `headers.Accept` | str | 见默认 | 声明接受 html/xml/json |
| `headers.Accept-Language` | str | `"zh-CN,zh;q=0.9,en;q=0.8"` | 语言偏好 |
| `headers.Accept-Encoding` | str | `"identity"` | 不做压缩，避免解压问题 |
| `headers.Connection` | str | `"keep-alive"` | 连接复用 |
| `headers.Cache-Control` / `Pragma` | str | `"no-cache"` | 禁用中间缓存 |
| `headers.Upgrade-Insecure-Requests` | str | `"1"` | 与浏览器一致 |
| `cache.enabled` | bool | `true` | 启用本地响应缓存 |
| `cache.ttl` | 秒 | `86400` | 缓存有效期 |
| `cache.dir` | path | `"cache"` | 缓存目录（含城市索引缓存） |

> 可往 `headers` 里追加任意自定义头（如 `Referer`、`Cookie`），会与默认头合并。

## C. `output` — 输出与文件

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `out_dir` | path | `"output"` | 输出目录（相对路径基于项目根；`--out-dir` 可覆盖） |
| `name_template` | str | `"{city}_{city_id}_climate_{profile}"` | **图片**文件名模板（不含扩展名）；`{profile}` 使图片**以配置名作后缀** |
| `table_name_template` | str | `"{city}_{city_id}_climate"` | **表格**文件名模板；默认**不带**配置名后缀 |
| `compare_name_template` | str | `"{city_count}城对比_{metric}_{profile}"` | 对比图文件名模板 |
| `table_formats` | array | `["csv","md","xlsx"]` | 表格格式：`csv` / `md` / `xlsx` / `json`（空数组=不出表格） |
| `chart_formats` | array | `["png"]` | 图片格式：`png` / `svg` / `pdf` / `jpg` / `webp`（空数组=不出图） |
| `chart_dpi` | int | `144` | 位图 DPI（`svg`/`pdf` 为矢量不受影响） |
| `overwrite` | str | `"overwrite"` | `overwrite` 覆盖 / `skip` 跳过已存在 / `timestamp` 加时间戳 |
| `csv_encoding` | str | `"utf-8-sig"` | CSV 编码（带 BOM 便于 Excel 直接打开） |
| `log_level` | str | `"INFO"` | 日志级别 |

## D. `figure` — 画布、标题、图例、网格、背景

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `figsize` | [宽,高] 英寸 | `[12.0, 6.0]` | 画布尺寸 |
| `dpi` | int | `144` | 画布分辨率 |
| `facecolor` | color | `"#ebf1f5"` | 画布底色；支持 8 位 HEX（含透明度），如 `#ffffff80` = 白色 50% 透明 |
| `axes_facecolor` | color/`auto`/`none` | `"auto"` | 绘图区底色；`auto` = 由 `facecolor` 向白混合派生；`none` = 透明（直接露出画布底色，避免两层半透明叠加导致该区域更不透明） |
| `edgecolor` | color | `"none"` | 画布边框 |
| `layout` | str | `"tight"` | 布局：`tight` / `constrained` / `none` |
| `subplots_adjust.left/right/top/bottom/wspace/hspace` | float/null | `null` | 手动边距（非 null 才生效） |
| `font_family` | array | `["Microsoft YaHei","SimHei","DejaVu Sans"]` | 字体族（列表按序回退，防中文方框） |
| `font_size` | float | `11` | 全局基准字号 |
| `font_weight` | str | `"normal"` | 全局字重 |
| `axes_unicode_minus` | bool | `true` | 负号显示为真减号 |
| `svg_fonttype` | str | `"none"` | SVG 文字形态：`none` = 保留 `<text>` 文字元素（可在 AI / Inkscape 中直接改字，依赖查看端字体）；`path` = 转轮廓路径（外观绝对一致但不可编辑） |
| `pdf_fonttype` | int | `42` | PDF 文字形态：`42` = TrueType 内嵌（可选中 / 可检索 / 可编辑）；`3` = Type 3 |
| `title.show` | bool | `true` | **是否显示标题** |
| `title.text` | str 模板 | `"{city} 气候统计"` | 标题文本 |
| `title.fontsize` / `color` / `fontweight` / `pad` | — | `16` / `#2c3e50` / `bold` / `14` | 标题样式 |
| `title.loc` | str | `"center"` | 标题位置：`left` / `center` / `right` |
| `subtitle.show` | bool | `false` | 是否显示副标题 |
| `subtitle.text` | str 模板 | `"{member} · {period}"` | 副标题文本 |
| `subtitle.fontsize` / `color` / `fontweight` / `pad` / `loc` | — | `10.5` / `#596679` / `normal` / `6` / `center` | 副标题样式 |
| `legend.show` | bool | `true` | 是否显示图例 |
| `legend.loc` | str | `"upper center"` | 图例位置（matplotlib loc 字符串） |
| `legend.bbox_to_anchor` | [x,y] / null | `[0.5,-0.12]` | 图例锚点（画布外置需给负值） |
| `legend.ncol` | int | `0` | 列数；`0` = 按元素数量自动 |
| `legend.fontsize` / `frameon` / `framealpha` | — | `10` / `true` / `0.9` | 图例样式 |
| `legend.facecolor` / `edgecolor` / `title` | — | `#ffffff` / `#c9d3dd` / `""` | 图例外观与标题 |
| `legend.markerscale` / `columnspacing` / `handlelength` / `handletextpad` | — | `1.0` / `1.6` / `2.0` / `0.6` | 图例细节 |
| `grid.show` | bool | `true` | 是否显示网格 |
| `grid.axis` | str | `"y"` | `x` / `y` / `both` |
| `grid.which` | str | `"major"` | `major` / `minor` / `both` |
| `grid.color` / `linestyle` / `linewidth` / `alpha` / `zorder` | — | `#c9d3dd` / `--` / `0.7` / `0.75` / `0` | 网格样式 |
| `background.bands.show` | bool | `false` | 是否显示月份背景色带（画在最底层坐标轴上，始终位于柱状图之下，不会罩住降水柱） |
| `background.bands.mode` | str | `"season"` | `season` 季节色带 / `alternate` 隔月交替 |
| `background.bands.hemisphere` | str | `"auto"` | 季节色带的半球：`auto` 按城市纬度自动判定（纬度 < 0 视为南半球，冬夏/春秋自动互换）/ `north` / `south`；仅影响 `season` 模式 |
| `background.bands.alpha` | float | `0.07` | 季节色带透明度 |
| `background.bands.alternate_color` / `alternate_alpha` | — | `#8fa8c0` / `0.06` | 交替模式颜色与透明度 |
| `background.bands.seasons` | array | 冬春夏秋 | 每项 `{name, months:[...], color}`，月份可跨年（如冬 = [12,1,2]）；按**北半球惯例**声明，南半球城市由 `hemisphere` 自动平移半年 |
| `zeroline.show` | bool | `false` | 是否画 0 基准线 |
| `zeroline.color` / `linestyle` / `linewidth` / `alpha` | — | `#8899aa` / `-` / `0.8` / `0.8` | 基准线样式 |
| `mean_rain_line.show` | bool | `false` | **是否画平均降水线**（12 个月降水量的平均水平线）；默认关闭，需显式置 `true`。取值只看该城市有无降水数据，与 `series.rainfall` 是否绘制无关；缺测月不计入，全缺则不画 |
| `mean_rain_line.axis` | str | `"auto"` | 画在哪个纵轴：`auto` = 跟随 `series.rainfall.axis` / `primary` / `secondary` / `tertiary` |
| `mean_rain_line.color` | str | `"auto"` | 线色：`auto` = 跟随 `series.rainfall.color` |
| `mean_rain_line.linestyle` / `linewidth` / `alpha` / `zorder` | — | `--` / `1.4` / `1.0` / `3` | 线型、线宽、透明度、层级（默认高于降水柱，不会被柱子盖住） |
| `mean_rain_line.label` | str | `"平均降水"` | 图例文案；空字符串 = 不进图例 |
| `mean_rain_line.annotate` | bool | `true` | 是否在线上标注平均值 |
| `mean_rain_line.annotate_template` | str 模板 | `"{label} {value:.1f} {unit}"` | 标注文本模板，占位符 `{label}` / `{value}` / `{unit}` / `{city}` / `{station}` |
| `mean_rain_line.annotate_color` | str | `"auto"` | 标注文字颜色：`auto` = 沿用线色；也可给具体色值（只改标注，不动线） |
| `mean_rain_line.annotate_position` | str | `"right"` | 标注横向锚点：`left` / `center` / `right`（对应绘图区左边缘 / 中线 / 右边缘，文字随之左对齐 / 居中 / 右对齐），或 `ticks` = **贴到该纵轴刻度标签那一列**（自动量取"向外刻度长度 + tick_pad"，与画布尺寸、字号、文案长度无关；刻度在左侧时自动镜像为右对齐） |
| `mean_rain_line.annotate_side` | str | `"above"` | 标注与均值线的纵向关系：`above`（文字底边高出线 dy 点）/ `below`（文字顶边低于线 dy 点）/ `center`（文字**垂直中心正落在线上**，此时 offset 的纵向分量不生效）。**自动避让**：若文字与该纵轴刻度标签撞上（横向同列且纵向间隙不足 2pt），会自动向上或向下让开，取「位移较小、且尽量不越出绘图区」的方向，让开后保证 ≥2pt 间隙 |
| `mean_rain_line.annotate_offset` | [dx, dy] | `[0, 4]` | 点偏移微调：`dx` 向右为正；`dy` 取非负数，**方向由 `annotate_side` 决定**（默认 4 pt 离开线） |
| `mean_rain_line.annotate_fontsize` | float | `9.0` | 标注字号 |
| `annotation.show_extremes` | bool | `false` | 是否标注极值月 |
| `annotation.series` | str | `"meanTemp"` | 标注哪个元素的极值 |
| `annotation.fontsize` / `color` / `show_value` | — | `9` / `#a32d2d` / `true` | 标注样式 |
| `annotation.avoid_overlap` | bool | `true` | **自动避让**：在候选位置里挑"冲突最少 → 越界最少 → 位移最小"的位置（避开数据曲线、另一条极值标注与平均降水线）。候选 = 偏好侧外推 × 横向错位（档位含按标注自身宽度自适应的值，长文案才挪得动）+ 翻到数据点另一侧，每个候选还会派生一个**最小幅度推回绘图区内**的版本，因此"贴近上下边界/首末月 + 小画布"时也不会掉出坐标区；设 `false` 恢复固定偏移 |
| `annotation.gap` | float | `2.0` | 碰撞判定的安全间隙（点）；贴得比它更近也算撞上 |
| `annotation.max_distance` | float | `52.0` | 位置搜索的半径上限（点）：最终偏移的**横纵分量**都不超过它（含"推回绘图区内"产生的位移） |
| `annotation.allow_flip` | bool | `true` | 是否允许翻到数据点另一侧（`false` 则最高始终在上、最低始终在下） |
| `credit.show` | bool | `false` | 是否显示数据来源署名 |
| `credit.text` | str 模板 | `"数据来源：世界天气信息服务网（WMO）"` | 署名文本 |
| `credit.fontsize` / `color` / `loc` / `pad` | — | `8.5` / `#7a8899` / `right` / `6` | 署名样式 |

## E. `axes_primary` / `axes_secondary` / `axes_tertiary` — 三个纵轴

三个轴配置项**结构完全一致**（`axes_tertiary` 多一个 `offset_points`）。
`primary` 放温度，`secondary` 放降水，`tertiary` 放量级差异大的元素（如降水日数）。

| 键 | 类型 | 默认（主轴/副轴/三轴） | 说明 |
|---|---|---|---|
| `show` | bool | `true` | 是否启用该轴（关闭后归属该轴的元素自动跳过） |
| `side` | str | `left` / `right` / `right` | 刻度位于哪一侧 |
| `offset_points` | int | — / — / `62` | **仅三轴**：相对右轴再向外偏移的点数 |
| `label_text` | str | `"温度 (°C)"` / `"降水 (毫米)"` / `"降水日数 (天)"` | **纵轴标题**（留空则不显示） |
| `label_position` | str | `"auto"` | **纵轴标题位置**：`auto` / `bottom` / `center` / `top`（沿轴方向）/ `left` / `right` |
| `label_x` / `label_y` | float/null | `null` | 显式指定标题坐标（轴坐标系比例，优先于 `label_position`） |
| `label_fontsize` / `label_color` / `label_fontweight` | — | `12` / `#2c3e50` / `bold` | 标题样式 |
| `label_rotation` | int/str | `90` / `270` | 标题旋转角度 |
| `label_align` | str | `"auto"` | 标题水平对齐：`auto` / `left` / `center` / `right`（横排时用于与刻度标签左/右对齐） |
| `label_valign` | str | `"auto"` | 标题垂直基准：`auto` / `top` / `center` / `bottom`（固定后可让左右两轴标题处于同一行） |
| `label_pad` | float | `10` / `12` | 标题与轴的间距 |
| `limit` | [min,max] | `[]` | 量程；空数组 = 按数据自动 |
| `limit_shift` | number | `0` | **量程整体平移**：显式/自动量程确定后，把上下限同向平移该值（正数向上），刻度随之重算；`--mark h` / `c` 即写此项（每档 ±10） |
| `auto_pad_ratio` | float | `0.14` / `0.18` | 自动量程时上下留白比例；柱状轴基线固定为 0 |
| `tick_start` | number/null | `null`（副/三轴为 `0`） | 刻度起始值 |
| `tick_step` | number/null | `null` | 刻度间隔（设定后按固定步长生成刻度） |
| `tick_count` | int/null | `null` | 刻度数量（与 `tick_step` 二选一，步长优先） |
| `tick_round_to` | number/null | `null` | 把量程取整到该倍数 |
| `tick_format` | str | `"{:.0f}"` | 刻度格式串（如 `"{:.1f}"`、`"{:,.0f}"`） |
| `tick_fontsize` / `tick_color` | — | `10` / `#5f666e` | 刻度文字样式 |
| `tick_length` / `tick_width` / `tick_direction` | — | `4` / `0.8` / `out` | 刻度线（方向 `in`/`out`/`inout`） |
| `scale` | str | `"linear"` | `linear` / `log` |
| `spine_show` / `spine_color` / `spine_linewidth` | — | `true` / `#c9d3dd` / `1.0` | 轴脊 |
| `hide_top_spine` | bool | `true` | 是否隐藏顶边轴脊 |

## F. `axes_x` — 横轴（月份轴）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `show` | bool | `true` | 是否显示横轴标题 |
| `label_text` | str | `"月份"` | 横轴标题（留空则不显示） |
| `label_fontsize` / `label_color` / `label_fontweight` / `label_pad` | — | `12` / `#2c3e50` / `bold` / `8` | 标题样式 |
| `tick_rotation` | float | `0` | 月份标签旋转角度 |
| `tick_fontsize` / `tick_color` | — | `10` / `#5f666e` | 标签样式 |
| `tick_interval` | int | `1` | 隔 N 个月显示一个标签（拥挤时用 2 或 3） |
| `tick_length` / `tick_width` / `tick_direction` | — | `4` / `0.8` / `out` | 刻度线 |
| `show_month_gridline` | bool | `false` | 是否在月份之间画竖分隔线 |
| `grid_color` / `grid_alpha` | — | `#c9d3dd` / `0.6` | 竖线样式 |
| `show_spine` / `spine_color` / `spine_linewidth` | — | `true` / `#c9d3dd` / `1.0` | 底边轴脊 |
| `limit_pad` | float | `0.5` | 左右留白（单位：月） |

## G. `series` — 绘图元素（每个元素独立配置）

可用元素键：`minTemp`（日均最低气温）、`maxTemp`（日均最高气温）、`meanTemp`（日均气温）、
`rainfall`（降水量）、`raindays`（降水日数）。
别名同样可用：`minTempC`/`maxTempF`/`avgTemp`/`rain`/`precipitation`/`ppt`/`rainDays` 等。

默认启用：`minTemp`、`maxTemp`、`rainfall`；默认关闭：`meanTemp`、`raindays`。

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | 见上 | **是否绘制该元素** |
| `label` | str | 各元素默认名 | 图例名；`auto` = 按元素自动命名（降水名随 raintype 变化） |
| `chart_type` | str | 各元素不同 | `line` / `smooth` 平滑 / `bar` 柱 / `area` 面积 / `step` 阶梯 / `scatter` 散点 / `fill_between` 区间带 |
| `axis` | str | `primary`/`secondary`/`tertiary` | 归属轴 |
| `scale_factor` | float | `1.0` | 数值缩放（把量级不同的元素压到同一轴，如降水日数 ×12） |
| `color` | color | 各元素不同 | **元素配色**（HEX / 颜色名） |
| `alpha` | float | `1.0` | 透明度 |
| `linewidth` | float | `2.0` | 线宽 |
| `linestyle` | str | `"-"` | 线型 `-` `--` `-.` `:` |
| `zorder` | float | `3` | 绘制层级 |
| `marker` | str | `"o"` | 数据点标记（`""` 不显示） |
| `markersize` | float | `4.5` | 标记大小 |
| `markerfacecolor` / `markeredgecolor` | color/`auto` | `"auto"` | 标记填充色与边色；`auto` = 与 `color` 一致 |
| `markeredgewidth` | float | `1.0` | 标记边宽 |
| `markevery` | int | `1` | 每隔 N 个点显示一个标记 |
| `bar_width` | float | `0.55` | 柱宽（`bar` 专有） |
| `bar_edgecolor` / `bar_linewidth` | — | `"none"` / `0.0` | 柱边线 |
| `bar_align` | str | `"center"` | 柱对齐 `center`/`edge` |
| `smooth_points` | int | `240` | 平滑曲线插值点数（`smooth` 专有，自实现三次 Hermite 插值） |
| `fill_alpha` | float | `0.18` | 面积图/区间带透明度 |
| `fill_between_key` | str | `"minTemp"` | 区间带的另一边界元素（`fill_between` 专有，如 `maxTemp` 以 `minTemp` 为底） |
| `data_labels.show` | bool | `false` | 是否显示数值标签 |
| `data_labels.fontsize` / `color` / `format` | — | `8.5` / `auto` / `"{:.1f}"` | 标签样式与格式 |
| `data_labels.offset` / `rotation` / `position` | — | `4.0` / `0` / `top` | 标签偏移/旋转/位置（`top`/`bottom`/`center`） |
| `order_in_legend` | float | 各元素不同 | 图例顺序（小者在前）；**绘制顺序固定为「先柱状、后折线」**，不受此项影响 |

**各元素默认值（官方页面配色）**

| 元素 | 配色 | 类型 | 轴 | 默认启用 |
|---|---|---|---|---|
| `maxTemp` | `#eb6877` | smooth | primary | ✅ |
| `minTemp` | `#0f91c4` | smooth | primary | ✅ |
| `meanTemp` | `#990000` | line -- | primary | ❌ |
| `rainfall` | `#46cbd4` | bar | secondary | ✅ |
| `raindays` | `#0f6e56` | line -. | tertiary | ❌ |

## H. `table` — 表格

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `rows` | array | 5 个元素 | 行顺序与显示项（可增删排序） |
| `row_labels` | obj | 见默认 | 行名文案覆盖；`rainfall` 用 `"auto"` 表示随 raintype 自动命名 |
| `transpose` | bool | `false` | `false` 行=元素、列=月份；`true` 则转置 |
| `precision.temp` / `.rainfall` / `.raindays` / `.annual` | int | `1` | 各类数值小数位 |
| `missing_placeholder` | str | `"—"` | 缺失值占位符 |
| `unit_in_label` | bool | `true` | 标签是否带单位（如「日均最低气温 (°C)」） |
| `include_annual` | bool | `false` | 是否加「年」列/行（温度取月均、降水取累加） |
| `annual_label` | str | `"年"` | 年值标签文案 |
| `show_period_note` | bool | `true` | 是否附统计时段脚注（取自数据源 `datab/datae` 等） |
| `show_source_note` | bool | `true` | 是否附数据来源脚注 |
| `show_title_row` | bool | `true` | 顶部是否写标题行（CSV/XLSX/Markdown） |
| `title_template` | str 模板 | `"{city} 气候统计（cityId {city_id}）"` | 表格标题 |
| `note_template` | str 模板 | 见默认 | 时段脚注模板（可用 `{period}` `{temp_unit}` `{rain_unit}`） |
| `xlsx.sheet_name` | str | `"气候数据"` | 工作表名 |
| `xlsx.freeze_header` | bool | `true` | 冻结表头与首列 |
| `xlsx.header_fill` / `header_font_color` | ARGB | `FF0EBFA2` / `FFFFFFFF` | 表头填充与字色 |
| `xlsx.zebra_fill` | ARGB | `FFF3F7FA` | 隔行底色 |
| `xlsx.column_width` / `first_column_width` | float | `13` / `20` | 列宽 |
| `xlsx.border_color` / `number_format` | ARGB/str | `FFBCC9D4` / `0.0` | 边框色与数字格式（`number_format` 仅作用于数值单元格） |

## I. `compare` — 多城市对比图

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `metric` | str | `"meanTemp"` | 对比元素 |
| `chart_type` | str | `"smooth"` | 对比图形类型（`rainfall` 时常配 `bar`） |
| `figsize` | [宽,高] | `[12.0, 6.0]` | 对比图画布 |
| `title_text` | str 模板 | `"{metric}对比 · {city}"` | 对比图标题 |
| `colors` | array | 8 色 | 按顺序分配给城市 |
| `color_map` | obj | `{}` | 显式指定「城市名 → 颜色」，优先于 `colors` |
| `color_by` | str | `"order"` | `order` 按传入顺序 / `city_id` 按编号取色 |
| `linestyle_cycle` / `marker_cycle` | array | 4 种 / 8 种 | 线型与标记循环 |
| `linewidth` / `markersize` / `alpha` | — | `2.2` / `5.0` / `1.0` | 线条样式 |
| `bar_width` | float | `0.8` | 柱状模式总柱宽（内部按城市数均分） |
| `sort_by` | str | `"value_desc"` | `none` / `value_desc` / `value_asc` / `name` / `city_id` |
| `show_value_range` | bool | `true` | 图例中附加该城市的数值范围 |
| `label_template` | str 模板 | `"{city}"` | 图例名模板 |
| `legend_ncol` | int | `0` | 图例列数；`0` 自动 |
| `data_labels.show` / `.fontsize` / `.format` / `.offset` | — | `false` / `8.0` / `"{:.1f}"` / `4.0` | 对比图数值标签 |

---

## 附：相对路径解析规则

`out_dir`、`cache.dir`、`raw_dir` 若为相对路径，一律基于**项目根目录**解析（不受当前工作目录影响）；
`--out-dir` 传入的路径按当前工作目录解析。绝对路径原样使用。
