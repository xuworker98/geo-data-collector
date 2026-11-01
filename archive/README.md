# archive —— 原始技能包归档

本目录存放**发布前的原始技能包原件**，用于追溯与版本对比，**不随仓库分发**。

| 文件 | 说明 |
|---|---|
| `geo-data-collector_V1.1.1_20261030.zip` | WorkBuddy 已发布版本 V1.1.1 的原始压缩包（14 个文件） |

- 原始包 SHA-256：`59301A161F72E288570F048CBF4196BDC0542B0753F4C571D2674F3CDFC1974E`
- 原始包大小：137,451 字节
- 归档时间：2026-11-02
- `.gitignore` 已排除 `archive/*.zip`，**zip 不会进入 Git 历史**

## 为什么不入库

1. 技能包本体已在仓库根目录（`SKILL.md` + `scripts/` + `references/` + `assets/`），zip 只是冗余副本；
2. 二进制包会让仓库体积随版本膨胀；
3. 需要原始包的人可以从 GitHub Release 附件获取打包版本。

## 与仓库当前内容的对应关系

| 原始包内容 | 仓库位置 | 处理方式 |
|---|---|---|
| `SKILL.md`（762 行） | `SKILL.md` + `references/` + `docs/` | 拆分为技能定义 + 规则全文 + 操作手册（内容未删减） |
| `README.md` | `README.md` | 按 GitHub 发布规范重写 |
| `scripts/*.py`（8 个） | `scripts/` | 原样保留（未改代码） |
| `references/*.md`（2 份） | `references/` | 保留并补充交叉引用 |
| `assets/模板_采集任务清单.xlsx` | `assets/` | 原样保留 |
| `config.example.json` | 根目录 | 原样保留 |
| （缺失的 `CHANGELOG.md`） | `CHANGELOG.md` | 本次补齐 |
