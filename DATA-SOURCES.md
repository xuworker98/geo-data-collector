# 数据来源与使用条款 / Data Sources

本仓库**不附带任何地图数据**，只提供查询与坐标转换工具。运行时数据来自下列公开来源，
使用前请自行核对各来源的最新条款。

## 一、行政区划数据

| 来源 | 用途 | 获取方式 | 条款提示 |
|---|---|---|---|
| 民政部**国家地名信息库**（省/市/区县/乡镇） | 五级标准名称与区划代码 | `python scripts/regions.py --province <省> --check` 联网抓取后本地缓存；无网用 `--import-mca <json>` 导入 | 非公开 API，走站点前端接口；请遵守该网站使用条款，**优先使用离线导入**；抓取频率保持克制 |
| **国家统计局**统计用区划代码（2023） | 村/社区级名称与代码 | `--import-villages <csv>` 导入 | 公开统计数据，引用时注明来源与年份 |

> 抓取结果缓存在 `data/regions/`（`.gitignore` 排除），元数据记录表版本与抓取时间。
> **请勿将缓存随仓库分发**；需要时重新抓取或导入。

## 二、地图服务 API（需自备 Key）

| 平台 | 接口域名 | 申请入口 | 坐标系 |
|---|---|---|---|
| 天地图（主源） | `api.tianditu.gov.cn` | https://console.tianditu.gov.cn/ | CGCS2000 |
| 高德 | `restapi.amap.com` | https://console.amap.com/dev/key/app | GCJ-02 |
| 百度 | `api.map.baidu.com` | https://lbsyun.baidu.com/apiconsole/key | BD-09 |
| 腾讯 | `apis.map.qq.com` | https://lbs.qq.com/dev/console/key/manage | GCJ-02 |

**必须使用服务端 / Web 服务类型的 Key**（浏览器端 JS API Key 不可用）。

### 使用限制（务必自行核对官方最新条款）

- 各平台对**批量抓取、缓存、二次分发、商用**有不同限制，以各平台《服务条款》《商用授权协议》为准；
- **禁止**绕过配额（多 Key 轮换）、禁止转售平台数据、禁止把采集结果当作地图数据源对外提供服务；
- 商用（含对外有偿服务）需自行与平台签约，费用与条款以官网为准；
- 对外发布地图还须遵守《测绘法》《地图管理条例》及审图号 / 资质要求——**坐标系转换不等于合规**。

## 三、算法与库

| 项目 | 说明 | 许可 |
|---|---|---|
| GCJ-02 / BD-09 转换公式 | 依据公开文档实现（见 `scripts/gc_common.py`） | 公式为公开算法；本仓库实现以 MIT 发布 |
| `pyproj` | CGCS2000(EPSG:4490) → WGS84(EPSG:4326) 严格转换 | MIT |
| `openpyxl` | Excel 读写（缺失时自动降级） | MIT |
| Python 标准库 | `urllib` / `csv` / `json` / `zipfile` 等 | PSF |

完整第三方声明见 [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md)。

## 四、复现与引用

引用本项目产出的数据时，请同时注明底层数据来源（对应平台 / 部委）与本工具版本，
并保留查询时间。示例：

```
数据来源：<平台名称> API（查询日期 YYYY-MM-DD）
处理工具：geo-data-collector v1.1.1（https://github.com/xuworker98/geo-data-collector）
```

## 五、无隶属关系声明

本项目为独立开源工具，与天地图、高德、百度、腾讯、民政部、国家统计局
**无隶属或合作关系**；相关名称与商标归各自所有者。
