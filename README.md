# 地理数据采集器 · geo-data-collector

**geo-data-collector — Batch Geocoding, POI Collection & Coordinate Verification**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-1.1.1-14508C.svg)](CHANGELOG.md)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](requirements.txt)

批量获取**准确、可验证、可交付**的地理空间数据：行政区划政府驻地、POI 点位、地址转经纬度、坐标纠偏与坐标系侦测。

竞品解决「能不能爬下来」，本工具解决「**爬下来的数据能不能用**」——四源交叉验真、四层质检漏斗、必填满、不虚标准确率。

> ⚠️ **内容语言 / Language**：本仓库文档、提示词与输出表头为**简体中文**。English readers can learn about features and usage from this README and the bilingual SKILL front-matter.

---

## ✨ 主要功能及特点 / Features

### 任务类型 / Task Types

| 任务类型 | 用途 | 关键实现 |
|------|------|---------|
| 政府驻地 | 省 / 市 / 区县 / 乡镇政府驻地经纬度与详细地址 | 区划清单驱动（民政部五级），逐点查证 |
| POI 周边 | 以指定坐标为中心、半径内某类 POI | 同心圆补采，防半径边缘漏点 |
| POI 区内分类 | 指定行政区划内某类 POI | 网格切分 + 分页去重，超量自动下钻 |
| POI 关键词 | 按关键词检索 POI | 四源瀑布 + 名称/门牌互证 |
| 地址转坐标 | 按表格地址列批量补齐经纬度 | 瀑布式判定：有地址先走地址，地址优先比名称优先高 7.5 个百分点 |
| 坐标转换 | WGS84 / GCJ-02 / BD-09 / CGCS2000 互转 | 六次迭代逆解，干净环境实测误差 0.0 m |
| 坐标系侦测 | 判定一份表到底是哪个坐标系 | 双假设反向验证；**只侦测、不自动决断** |

### 重点增强能力 / Key Enhancements

- **四源降级 Four-source Fallback**：天地图 → 高德 → 百度 → 腾讯，单源失效不中断，轨迹全记录。
- **四坐标系归一 Four-CRS Normalization**：天地图 CGCS2000 严格转换；GCJ-02 六次迭代逆解；BD-09 两级转换。
- **四层质检漏斗 QC Funnel**：L0 硬杀筛（缺失/越界/重复）→ L1 离群筛 → L2/L3 交叉验真与逆向验真。
- **自适应抽检 Adaptive Sampling**：正常 5% → 连续合格放宽 2% → 出现不合格加严 15%。
- **断点续传 Checkpoint Resume**：每条结果即时落盘 `.jsonl`，中断重跑自动跳过已完成键，已花钱的调用不重复。
- **配额熔断 Quota Circuit-breaker**：按日配额预算停机，跨自然日不盲重试。
- **六表 Excel Six-table Output**：地市 / 区县 / 乡镇 / 明细 / 汇总 / 校验，附 CSV 与五段式质检报告。
- **必填满 Never-blank Delivery**：每条输入必有一行输出（坐标，或「未命中 + 原因 + 建议动作」），绝不留空。
- **零 Key 可用 Zero-key Mode**：坐标转换、纠偏、区划查询（本地缓存）、模板生成均不需要任何 Key。
- **代理自愈 Proxy Self-healing**：`http_opener()` 探测代理端口，失效代理自动跳过（本机曾因失效代理表现为「整网禁网」）。

## 🖼️ 工作流程示意 / Pipeline

本仓库是 **AI 技能包 + Python 库**（不是 GUI 软件），核心是一条八步穿透流水线：

```mermaid
flowchart LR
    A["任意格式表格<br/>或自然语言指令"] --> B["① 整理成标准模板"]
    B --> C["② 意图解析回显确认"]
    C --> D["③ 环境自检"]
    D --> E["④ 试跑 3 条<br/>人工验真"]
    E --> F{"用户确认"}
    F -->|"否"| B
    F -->|"是"| G["⑤ 全量采集<br/>四源降级 + 断点续传"]
    G --> H["⑥ 四层质检漏斗"]
    H --> I["⑦ 六表 Excel/CSV<br/>+ 五段式质检报告"]
    I --> J["⑧ 交付与回查"]
```

> **「试跑 3 条」不可跳过**：参数写错（如同名地点未加城市限定）会导致整批数据作废，
> 10 秒的代价可以避免数小时返工。

流水线各步的输入输出契约见 [`docs/使用手册.md`](docs/使用手册.md)。

## 📁 支持格式 / Supported Formats

### 输入 / Input

| 载体 | 说明 |
|------|------|
| `.xlsx` / `.xls` | 标准模板，或任意格式表格（由 AI 整理成标准模板） |
| `.csv` / `.tsv` | 自动编码侦测：`utf-8-sig → gbk → gb18030 → utf-8`，Excel 另存为 GBK 也不会读崩 |
| 自然语言 | 由 Agent 按 SKILL.md 的意图解析规则转成结构化任务 |

标准输入模板 16 列（`scripts/export.py: TPL_FIELDS`，唯一真源）：

```
任务ID | 任务类型 | 省 | 市 | 区县 | 乡镇 | 关键词 | 中心点经度 | 中心点纬度
 | 半径米 | 名称 | 详细地址 | 参考经度 | 参考纬度 | 待查列 | 备注
```

### 输出 / Output

| 产物 | 内容 |
|------|------|
| `.xlsx` 六表 | 地市表 / 区县表 / 乡镇表 / 明细清单 / 汇总表 / 校验表 |
| `.csv` | 无 `openpyxl` 时自动降级为同前缀多文件，**列名与内容完全一致** |
| 质检报告 `.md` | 五段固定结构：任务概览 / 计划对账 / 质量结论 / 异常清单 / 遗留与建议 |

明细 20 列**必带双坐标**：`经度_WGS84 / 纬度_WGS84` 供工程交付，`经度_GCJ02 / 纬度_GCJ02` 供用户在高德地图直接取点核对。

### 坐标系 / CRS

| 坐标系 | 常见来源 | 本工具如何处理 |
|------|------|------|
| WGS84 | GPS、工程交付 | 默认输出坐标系 |
| GCJ-02 | 高德、腾讯、对外发布地图 | 六次迭代逆解回 WGS84（单次近似实测偏差 450 m 量级，禁止使用） |
| BD-09 | 百度 | BD-09 → GCJ-02 → WGS84 两级转换（只做一级残留约 100 m） |
| CGCS2000 | 天地图、国家大地坐标 | `pyproj` EPSG:4490 → 4326 严格转换（缺 `pyproj` 时降级为恒等，差异 <1 m） |

## 🚀 快速开始 / Quick Start

### 环境要求 / Requirements

| 依赖 | 版本 | 说明 |
|------|------|------|
| Python | ≥ 3.10 | 用到了 `X \| None` 运行时注解 |
| pyproj | ≥ 3.4 | 严格坐标转换；缺失时自动降级为恒等转换 |
| openpyxl | ≥ 3.0 | 读写 Excel；缺失时输出降级为纯标准库 xlsx / CSV |
| 地图 Key | 至少 1 个 | 天地图 / 高德 / 百度 / 腾讯，**本仓库不内置任何 Key** |

### 三步跑起来

```bash
# 1. 装依赖
pip install -r requirements.txt

# 2. 配 Key（复制模板后填入；config.local.json 已被 .gitignore 排除）
copy config.example.json config.local.json     # Windows
cp config.example.json config.local.json       # macOS / Linux

# 3. 网络体检（连不通时第一步跑这个，别先怀疑 Key）
python scripts/config.py
```

### 常用自检与准备命令

```bash
# 能力层自检：坐标算法往返 + 四源配置
python scripts/gc_common.py

# 区划数据准备：首次联网抓取后永久缓存到 data/regions/（广西实测约 60 s）
python scripts/regions.py --province 广西 --check

# 生成标准输入模板
python scripts/export.py --template assets/模板_采集任务清单.xlsx

# 模块离线自检（不花配额、不联网）
python scripts/qc.py
python scripts/collect.py --selftest
```

### 一条采集任务（Python API）

```python
import sys
sys.path.insert(0, "scripts")

from collect import Job, run
from export import build_six_tables, write_qc_report, write_table

# 采集青秀区全部乡镇政府驻地
job = Job(kind="gov_seat", province="广西壮族自治区", city="南宁市",
          area="青秀区", levels=(4,))
res = run(job)                       # 自动出计划 → 执行 → 断点落盘
rows = res["结果"]

# 六表 + 质检报告
tables = build_six_tables(rows)
for name, trows in tables.items():
    write_table(f"output/{name}.xlsx", trows)
write_qc_report("output/质检报告.md", meta, rows)
```

完整参数、任务类型枚举与输出列定义见 [`docs/使用手册.md`](docs/使用手册.md)；
Agent 触发与执行纪律见 [`SKILL.md`](SKILL.md)。

### 预置提示词范例 / Prompt Examples

| # | 范例（【】内可改） | 场景 |
|---|---|---|
| 1 | 采集【广西壮族自治区】所有【乡镇】政府驻地的经纬度和详细地址 | 政府驻地 |
| 2 | 采集【南宁市】【青秀区】所有【小学】的经纬度和地址 | 区内分类 POI |
| 3 | 以【108.3622, 22.8199】为中心，采集半径【3000】米内的【加油站】 | 周边 POI |
| 4 | 按表格【data.xlsx】的【地址】列，批量补齐经纬度并交叉验真 | 地址转坐标 |
| 5 | 把表格【data.xlsx】的【经度/纬度】列，从【WGS84】转换到【GCJ-02】 | 批量坐标转换 |
| 6 | 检测表格【data.xlsx】坐标系是否真的是【WGS84】，出自检报告 | 坐标系侦测 |
| 7 | 按模板【模板_采集任务清单.xlsx】采集，只补留空列，其他列不动 | 模板驱动回填 |

## 📊 准确率与能力边界 / Accuracy & Limits

**分场景分级承诺，不虚标 95%**（政务网点类实测上限 85.4%，因此按场景分别承诺）：

| 场景 | 承诺 ≤100 m 达标率 | 依据 |
|---|---|---|
| 坐标转换 / 纠偏 | **≥99%** | 纯算法，迭代逆解误差 <1 m，干净环境实测 0.0 m |
| 坐标系侦测 | 不承诺准确率，只承诺**不自动决断** | 双向验证 PASS，但区分度依赖地址质量，低置信必须交用户拍板 |
| 标准门址 → 经纬度（有门牌号） | **≥90%** | 地址路径 ① 级命中 86.1% |
| 政务服务中心 / 派出所 / 政务网点 | **如实标注实测值**（当前 64.9%～77.5%，三源 75.9%） | 最难场景：图商收录整栋楼、专区在楼内 |
| 自然村 / 村民小组 | 不承诺准确率，只承诺「**必填满 + 列候选交用户**」 | 官方无编码无坐标，禁止自动破平 |

**两条硬承诺**：① **不杜撰**——查不到就写查不到，绝不用邻近点或上级行政区中心冒充目标坐标；
② **不半截交付**——每条输入必有一行输出，绝不只交最确认的几条然后一片留空。

**唯一合法准确率口径**：`准确率 = 达标条数 ÷ 总条数`，留空 / 存疑 / 未命中一律计为不达标。

### 本工具不做什么 / Not Supported

| 不做 | 说明 |
|---|---|
| 行政区划边界多边形 | 已移除（原 F8）。不提供省/市/县/乡镇边界 GeoJSON，不做点在多边形判定；需要边界请用 DataV 等专门工具 |
| 地形 / 遥感影像 / 路网拓扑 | 不在能力范围内 |
| 室内定位、楼层级坐标 | 图商 API 不提供（政务专区在楼内某层只能标存疑） |
| 自动决断坐标系 | 可侦测，**不可自动转换**，必须交用户拍板 |
| 自然村名录 | 官方渠道没有自然村清单，必须先由用户提供或人工整理 |

## 🧪 开发者 / For Developers

### 仓库结构

```text
geo-data-collector/
├── SKILL.md                 AI 技能定义（触发条件 / 流程 / 硬约束 / 参考索引）
├── README.md                项目主页（本文件）
├── scripts/                 核心代码
│   ├── config.py            配置加载 + 网络层（代理探测）
│   ├── gc_common.py         能力层：坐标引擎 + 四源适配 + 自然村查询
│   ├── regions.py           区划：民政部五级 + 统计局村级 + 双套代码错位对齐
│   ├── adminchain.py        行政编码链：四级编码解析 + 撤乡设镇 / 跨区纠偏
│   ├── collect.py           采集：四任务类型 + 抓前计划 + 配额断点
│   ├── poi_query.py         POI：瀑布式判定 + 区县字典 + 业务条线互斥
│   ├── qc.py                质检：四层漏斗 + 自适应抽检 + 坐标系侦测
│   └── export.py            输出：六表 + 模板 + 质检报告（三级降级）
├── references/              给 AI 的速查手册（接口坑位 / 规则 / 口径 / 案例）
├── docs/                    给人看的手册（使用 / 架构 / 排障）
├── assets/                  输入模板 xlsx
├── examples/                示例输入
├── archive/                 原始技能包归档（zip 不随仓库分发）
└── .github/workflows/ci.yml 离线自检 CI
```

### 本地自检（零 Key、可离线）

```bash
python -m compileall -q scripts
python scripts/gc_common.py            # 坐标算法往返校验
python scripts/qc.py                   # 质检 12/12
python scripts/collect.py --selftest   # 网格切分 / 断点 / 计划估算
```

### 扩展一个新数据源

需要同时改四处，缺一处就会「静默失效」：

1. `scripts/config.py`：Key 名、环境变量映射、日配额、QPS
2. `scripts/gc_common.py`：适配函数 + 降级链 `FALLBACK_ORDER` + 坐标归一
3. `references/四源接口对照表.md`：能力、坐标系、必踩坑、限速
4. `SKILL.md`：能力边界与前置条件

> 判据类改动必须附实测证据（样本量、达标率变化）。本仓库已有多条「看似合理但实测变差」的反例，
> 见 [`references/实测案例与归因.md`](references/实测案例与归因.md)。

## 🏗️ 技术栈 / Tech Stack

Python 3.10+ · 标准库（`urllib` / `csv` / `json` / `zipfile`）· pyproj（CGCS2000 严格转换）· openpyxl（可选）
· 天地图 / 高德 / 百度 / 腾讯 Web 服务 API · 民政部国家地名信息库 · 国家统计局统计用区划代码

**零重型依赖**：除 `pyproj` / `openpyxl` 外不依赖任何第三方库；Excel 输出保留纯标准库 OOXML 降级路径。

## 🔐 合规红线 / Compliance

1. **不内置 Key** —— Key 仅存本地 `config.local.json`（已 `.gitignore`）或环境变量，绝不入源码、示例与日志
2. **不做 Key 池轮换** —— 多 Key 轮换突破配额属违规，禁止实现
3. **输出坐标系可切换** —— 工程交付 WGS84；对外发布地图须切 GCJ-02（测绘法要求）
4. **行政区划只认官方源** —— 乡镇及以上取民政部，村级取统计局，不用图商数据反推区划
5. **商用授权提示** —— 四源商用授权需自行与平台签约（各源约 5 万元/年）；采集结果不得作为数据源对外销售

## 📄 许可证 / License

本项目采用 [MIT License](LICENSE) 许可协议。

- ✅ 可自由使用、修改、分发、商用
- ✅ 使用时须保留版权声明与许可声明
- ❌ 不提供任何担保（数据准确性由使用者自行核验）

This project is licensed under the [MIT License](LICENSE).

## 📞 联系方式 / Contact

- **作者 Author**：通信民工（许景渊）
- **哔哩哔哩 Bilibili**：@通信民工（UID：482597398）
- **微信公众号 WeChat**：@通信民工（ComDesigner）
- **QQ**：853665220（@qq.com）
- **网站 Website**：[http://www.txmg.com](http://www.txmg.com)
- **著作 Book**：《5G网络规划与工程实践》（清华大学出版社出版）

---

> **相关仓库**：[PlanLayerCreator](https://github.com/xuworker98/PlanLayerCreator)（通信规划图层生成工具）·
> [POICollector](https://github.com/xuworker98/POICollector)（POI 数据采集器）·
> [md-print-pdf](https://github.com/xuworker98/md-print-pdf)（Markdown 转 A4 打印级 PDF）
