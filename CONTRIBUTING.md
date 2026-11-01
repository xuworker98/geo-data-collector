# 贡献指南 / Contributing

感谢你对**地理数据采集器（geo-data-collector）**的关注！欢迎提交 Issue 与 Pull Request。

## 一、报告问题 / Reporting Bugs

提交 Issue 时请提供：

1. 运行环境（操作系统、Python 版本、是否安装 `pyproj` / `openpyxl`）
2. 复现步骤与**脱敏后**的输入样例（去掉真实客户名、地址、Key）
3. 完整报错信息或日志片段
4. 若有坐标偏差，请附：目标名称、期望坐标、实际坐标、偏差米数

> ⚠️ **禁止**在 Issue 中粘贴 API Key、身份证号、真实客户数据或未脱敏的采集结果。

## 二、提交代码 / Submitting Code

1. Fork 本仓库
2. 创建特性分支：`git checkout -b feature/xxx`
3. 本地跑通离线自检（不联网、不需要 Key）：

```bash
python -m compileall -q scripts
python -X utf8 scripts/gc_common.py          # 坐标算法自检
python -X utf8 scripts/qc.py                 # 质检模块自检 12/12
python -X utf8 scripts/collect.py --selftest # 采集模块自检
python -X utf8 scripts/export.py --demo      # 输出链路（六表 + 报告）冒烟
```

> Windows 控制台建议加 `-X utf8`，避免 GBK 代码页下的中文乱码。

4. 复核本次改动是否触及「不可改」的硬约束（见下文）
5. 提交 PR，并在描述里写明：改了什么、依据是什么、跑过哪些自检

## 三、代码规范 / Code Style

- 遵循 PEP 8；**中文注释与提示信息一律 UTF-8**
- 不引入重型依赖：坐标与采集用标准库 + `pyproj`（可降级）；Excel 输出保留纯标准库降级路径
- 新增数据源需同时补：`references/四源接口对照表.md` 的能力/坑位、降级链位置、限速与配额
- 新增判据需附**实测证据**（多少条样本、达标率变化多少），只写"我觉得更合理"的 PR 不予合并
- 网络层遵循：4xx 是永久错不重试；默认超时 15 s（`runtime.timeout`）；日配额爆掉当日熔断
- 涉及凭证/数据安全请先读 [`SECURITY.md`](SECURITY.md)；涉及数据来源与条款读 [`DATA-SOURCES.md`](DATA-SOURCES.md)

## 四、不可改动的硬约束 / Non-negotiable Rules

以下几条是踩坑换来的，修改会被直接拒绝：

| # | 约束 | 原因 |
|---|---|---|
| 1 | **不内置任何 API Key**，不得写入源码/示例/日志 | 合规红线，且公开仓库会泄露 |
| 2 | **不做 Key 池轮换** | 属违规突破配额 |
| 3 | `gcj02_to_wgs84` 保持 6 次迭代逆解，不得改单次近似 | 单次近似不可接受（GCJ-02 与 WGS84 总偏移约 0.5 km） |
| 4 | 自然村匹配必须用（区县, 乡镇, 行政村）复合键 | 同名村会跨村串味 |
| 5 | 坐标系**可侦测不可决断**，必须交用户拍板 | 误判代价 = 整表偏移 500 m |
| 6 | 票数不能压过内容判据（R23 已证伪三种改法） | 同源抄写会击穿票数制 |
| 7 | 行政区划名称只认民政部/统计局官方源 | 图商数据不作为行政区划依据 |

## 五、沟通 / Communication

- Issue 与 PR 优先使用中文
- 讨论判据时请贴数据，不要只贴结论
- 保持友善与尊重
