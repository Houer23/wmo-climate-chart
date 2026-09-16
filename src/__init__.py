"""WMO 城市气候数据解析与图表生成工具。

包结构
------
* ``http_client`` 请求层（主请求头 / 重试 / 缓存）
* ``parser``      解析层（容错 + 平均气温派生）
* ``city_index``  城市名 → cityId 反查
* ``config_loader`` 多套配置（默认值 / profiles / 继承 / --set 覆盖）
* ``table_writer`` 表格输出（CSV / Markdown / XLSX / JSON）
* ``chart``       绘图（matplotlib 双轴，全参数可配）
* ``pipeline``    编排（单城 / 批量 / 多城市对比）
"""

__version__ = "1.0.0"
