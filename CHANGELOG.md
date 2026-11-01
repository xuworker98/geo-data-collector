# 更新日志 / Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)（Semantic Versioning）。

## [1.1.1] - 2026-10-30

首次开源发布（GitHub）。能力层与采集链路与 WorkBuddy 已发布版本一致，
本次只做**发行工程化与文档重构**，不改判据、不改坐标算法。

### 新增

- 补齐 GitHub 发行文件：`LICENSE`（MIT）、`CHANGELOG.md`、`CONTRIBUTING.md`、
  `SECURITY.md`、`DATA-SOURCES.md`、`THIRD-PARTY-NOTICES.md`、`VERSION`、
  `.gitignore`、`.gitattributes`、`requirements.txt`、GitHub Actions 离线自检
- `README.md` 重写为项目主页：功能总表、流程示意、支持格式、快速开始、
  准确率分级承诺、合规与使用限制、已知限制、数据来源、开发者说明、联系方式
- 新增 `docs/`：`使用手册.md`、`架构说明.md`、`排障与常见坑.md`
- 新增 `references/`：`自然村采集规则.md`（R1–R23）、`准确率与质检口径.md`、
  `实测案例与归因.md`
- 新增 `examples/` 示例输入表、`archive/` 原始技能包归档说明
- 新增 `export.to_out_fields()`：采集记录 → 明细行适配（补 `所属乡镇`、派生 GCJ-02、归一质检状态）

### 变更

- `SKILL.md` 由 762 行精简为技能定义（触发条件 / 流程 / 硬约束 / 参考索引），
  长规则与实测案例改为 `references/` 渐进披露，按需读取
  （原文内容未删减，仅重新归类）
- `OUT_FIELDS` 增加 `所属乡镇`（20 → 21 列），修复乡镇表恒为「(未填写)」的问题
- 统一仓库文本编码与换行（UTF-8 无 BOM / LF）
- 修正旧 README 目录表中列出的不存在目录（`tests/`、`data/`、`temp/`），
  改为"运行后生成"的准确说明

### 修复（开源前安全与可用性审计）

- **HTTPS 证书校验恢复默认开启**：此前全局 `CERT_NONE` 会让 Key 与地址暴露给中间人；
  新增 `GDC_INSECURE_TLS=1` 显式降级开关（带警告）
- **依赖缺失不再静默**：缺 `pyproj` 时打印 `RuntimeWarning` 并按恒等处理
- **网络失败不再静默**：`_get_retry` 失败时发 `RuntimeWarning` 并记录 `_LAST_NET_ERROR`
- **GBK 控制台崩溃修复**：采集计划里 `⚠` 字符在 cp936 控制台会 `UnicodeEncodeError`，改为 `[!]`
- **`export.py --template` 默认路径改为仓库 `assets/`**，不再依赖当前工作目录（此前从 `scripts/` 运行会静默生成第二份 assets）
- 删除源码中的内部项目名与第一人称复盘表述；`adminchain._bbox_of` 标明为未实现桩
- 修正文档数字：GCJ-02 偏移改为实测 510~560 m；`qc.py` 自检 12 项；检索词阶梯最多 9 条（含村委会变体）；请求超时 15 s；补充 300 m（质检分级）与 100 m（交付达标线）的区别

### 已知限制

见 [`README.md`](README.md#️-已知限制--known-limitationsv111)：
POI 两模式仅高德、网络失败归「未命中」、6 类任务与 4 个 `Job.kind` 的差异、300 m 与 100 m 双阈值等。

## [1.0.0] - 2026-10-20

首个可交付版本（M1–M7 全部完成并实测）：

- M1 能力层：四源适配 + 坐标引擎（GCJ-02 六次迭代逆解，误差 <1 m）
- M2 区划模块：民政部五级 + 统计局村级 + 双套代码错位三级对齐
- M3 采集模块：政府驻地 / POI 三模式 + 配额断点
- M4 质检模块：四层漏斗 + 坐标系侦测 + 报告
- M5 输出模块：六表 Excel/CSV，openpyxl → 纯标准库 OOXML → CSV 三级降级
- M6 Skill 封装：SKILL.md + 输入模板 + references
- M7 端到端穿透：5/5 必填满、5/5 ≤100 m

> 说明：1.0.0 → 1.1.1 之间的内部迭代（判据修正 R1–R23、133 条政务网点盲测、
> 自然村盲测等）已在 `references/` 与 `SKILL.md` 中留痕，未逐版单列。
