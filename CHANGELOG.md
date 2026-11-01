# 更新日志 / Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)（Semantic Versioning）。

## [1.1.1] - 2026-10-30

首次开源发布（GitHub）。能力层与采集链路与 WorkBuddy 已发布版本一致，
本次只做**发行工程化与文档重构**，不改判据、不改坐标算法。

### 新增

- 补齐 GitHub 发行文件：`LICENSE`（MIT）、`CHANGELOG.md`、`CONTRIBUTING.md`、
  `.gitignore`、`.gitattributes`、`requirements.txt`、GitHub Actions 离线自检
- `README.md` 重写为项目主页：功能总表、流程示意、支持格式、快速开始、
  准确率分级承诺、合规红线、开发者说明、联系方式
- 新增 `docs/`：`使用手册.md`、`架构说明.md`、`排障与常见坑.md`
- 新增 `references/`：`自然村采集规则.md`（R1–R23）、`准确率与质检口径.md`、
  `实测案例与归因.md`
- 新增 `examples/` 示例输入表、`archive/` 原始技能包归档说明

### 变更

- `SKILL.md` 由 762 行精简为技能定义（触发条件 / 流程 / 硬约束 / 参考索引），
  长规则与实测案例改为 `references/` 渐进披露，按需读取
  （原文内容未删减，仅重新归类）
- 统一仓库文本编码与换行（UTF-8 无 BOM / LF）
- 修正旧 README 目录表中列出的不存在目录（`tests/`、`data/`、`temp/`），
  改为"运行后生成"的准确说明

### 修复

- `SKILL.md` 末尾进度描述停留在"M6 进行中 / M7 待做"，与 V1.1.1 实际状态冲突，已改正
- `SKILL.md` 引用的 `CHANGELOG.md` 此前不存在，本次补齐

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
