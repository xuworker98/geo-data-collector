# 第三方声明 / Third-Party Notices

本项目以 [MIT License](LICENSE) 发布。下列第三方组件、算法与数据来源各自保留其权利。

## 一、运行时依赖

| 组件 | 用途 | 许可 | 项目主页 |
|---|---|---|---|
| [pyproj](https://pyproj4.github.io/pyproj/) | CGCS2000（EPSG:4490）→ WGS84（EPSG:4326）严格转换；缺失时降级为恒等 | MIT | https://github.com/pyproj4/pyproj |
| [openpyxl](https://openpyxl.readthedocs.io/) | Excel 读写；缺失时输出降级为纯标准库 OOXML / CSV | MIT | https://foss.heptapod.net/openpyxl/openpyxl |
| Python 标准库 | `urllib` / `csv` / `json` / `zipfile` / `ssl` 等 | PSF License | https://www.python.org/ |

> 以上依赖不作为本仓库源码分发，由使用者通过 `pip install -r requirements.txt` 安装。

## 二、坐标转换算法

`scripts/gc_common.py` 中的 WGS84 / GCJ-02 / BD-09 互转实现依据**公开文档化的转换公式**
（椭球参数与多项式偏移量属公开资料），并自行实现 6 次迭代逆解与黄金向量回归。

- 本项目未复制任何第三方源码文件；
- 若你发现某段实现与某开源项目高度一致且需要署名，请提交 Issue，我们会补上对应版权声明；
- 逆解迭代次数与容差为本项目实测调优结果（见 `references/实测案例与归因.md`）。

## 三、数据来源

| 数据 | 来源 | 说明 |
|---|---|---|
| 行政区划（省/市/区县/乡镇） | 民政部国家地名信息库 | 不随仓库分发，本地抓取/导入后缓存 |
| 村级区划代码 | 国家统计局统计用区划代码 | 公开统计数据 |
| 地图 POI / 地理编码 | 天地图 / 高德 / 百度 / 腾讯 Web 服务 API | 需使用者自备 Key，并遵守各平台条款 |

地图数据的版权归各平台所有；本项目只做查询与坐标转换，**不转售、不缓存对外分发平台数据**。
详见 [`DATA-SOURCES.md`](DATA-SOURCES.md)。

## 四、商标

天地图、高德、百度、腾讯及其 Logo 为各自所有者的商标。
本项目与上述公司无隶属或合作关系，仅通过其公开 Web 服务 API 访问数据。
