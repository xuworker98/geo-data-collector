# archive —— 原始包与发布包归档

本目录存放**原始技能包原件**与**GitHub Release 发布包副本**，用于追溯、版本对比与离线分发。
两者均被 `.gitignore` 排除（`archive/*.zip`），**不进入 Git 历史**。

| 文件 | 说明 |
|---|---|
| `geo-data-collector_V1.1.1_20261030.zip` | WorkBuddy 已发布版本 V1.1.1 的**原始压缩包**（14 个文件，137,451 字节） |
| `geo-data-collector_v1.1.1_skill.zip` | GitHub Release `v1.1.1` 的**发布包副本**（27 个文件，185,879 字节） |

## 校验值

| 文件 | SHA-256 |
|---|---|
| 原始包 | `59301A161F72E288570F048CBF4196BDC0542B0753F4C571D2674F3CDFC1974E` |
| 发布包 | `8C651450DBEBF36325261483DE3E263BF3AFEBBAFF19E5226568758D547EA21A` |

校验命令：

```powershell
Get-FileHash .\geo-data-collector_v1.1.1_skill.zip -Algorithm SHA256
```

```bash
sha256sum geo-data-collector_v1.1.1_skill.zip
```

## 发布包如何生成（可重放）

不要手工压缩，统一用打包脚本从仓库根生成（白名单 + 统一时间戳，同一提交两次打包字节一致）：

```powershell
python D:\DeepSeek\02_脚本工具\脚本_技能发布打包_20261102.py `
  --repo D:\DeepSeek\05_技能提示词\技能\geo-data-collector `
  --out  D:\DeepSeek\05_技能提示词\技能\geo-data-collector\archive\geo-data-collector_v1.1.1_skill.zip
```

发布包内容：`SKILL.md`、`README.md`、`LICENSE`、`VERSION`、`CHANGELOG.md`、`SECURITY.md`、
`DATA-SOURCES.md`、`THIRD-PARTY-NOTICES.md`、`config.example.json`、`requirements.txt`、
`scripts/`、`references/`、`docs/`、`assets/`。
（`archive/`、`.github/`、`examples/` 不进包。）

## 为什么不入库

1. 技能包本体已在仓库根目录（`SKILL.md` + `scripts/` + `references/` + `assets/`），zip 只是冗余副本；
2. 二进制包会让仓库体积随版本膨胀；
3. 需要发布包的人可直接从 GitHub Release 附件下载，或按上面的命令自行重放生成。

> 归档时间：2026-11-02；发布包与 Release 附件 SHA-256 一致（已核对）。

## 与仓库当前内容的对应关系

| 原始包内容 | 仓库位置 | 处理方式 |
|---|---|---|
| `SKILL.md`（762 行） | `SKILL.md` + `references/` + `docs/` | 拆分为技能定义 + 规则全文 + 操作手册（内容未删减） |
| `README.md` | `README.md` | 按 GitHub 发布规范重写 |
| `scripts/*.py`（8 个） | `scripts/` | 原样保留（判据与算法未改；后续仅做安全与可用性修正） |
| `references/*.md`（2 份） | `references/` | 保留并补充交叉引用 |
| `assets/模板_采集任务清单.xlsx` | `assets/` | 原样保留 |
| `config.example.json` | 根目录 | 原样保留 |
| （缺失的 `CHANGELOG.md`） | `CHANGELOG.md` | 本次补齐 |
