# 安全策略 / Security Policy

## 支持的版本 / Supported Versions

| 版本 | 是否维护 |
|---|---|
| 1.1.x | ✅ 接受安全报告 |
| < 1.1 | ❌ 请先升级 |

## 报告安全问题 / Reporting a Vulnerability

- **不要**在公开 Issue 中粘贴 API Key、采集结果、身份证号或真实客户地址。
- 优先通过 GitHub 的 **Private vulnerability reporting**（仓库 Security 标签页）提交；
  若不可用，请发邮件至作者（见 README「联系方式」），主题注明 `[SECURITY] geo-data-collector`。
- 请提供：影响版本、复现步骤、影响范围（Key 泄露 / 数据泄露 / 代码执行等）。
- 我们会在 7 天内确认收到，并在修复后于 `CHANGELOG.md` 致谢（除非你要求匿名）。

## 凭证与数据边界 / Credential & Data Boundary

本项目的安全模型：

1. **不内置任何 Key**：Key 只存本地 `config.local.json`（`.gitignore` 排除）或环境变量
   `GDC_TD_KEY` / `GDC_AMAP_KEY` / `GDC_BAIDU_KEY` / `GDC_QQ_KEY`。
2. **日志脱敏**：`config.mask_key()` 输出 Key 片段；不要把完整终端输出贴到公开渠道。
3. **HTTPS 证书严格校验**：默认开启。仅当身处必须抓包的可信内网排障环境，才设
   `GDC_INSECURE_TLS=1` 降级——**开启后 Key 与地址可能被中间人窃取，用完请立即取消**。
4. **用户数据落在本地**：`data/`（断点、区划缓存、黄金样本）与 `output/`（六表、报告）
   均在 `.gitignore` 中排除；提交前请 `git status` 复核，避免把地址数据推上远端。
5. **不做 Key 池轮换**：多 Key 轮换突破配额属违规，代码不接受此类改动。

## 依赖安全 / Dependencies

- 运行依赖仅 `pyproj` 与 `openpyxl`（均可选降级），无网络服务端组件。
- 建议定期 `pip install -U pyproj openpyxl` 获取上游安全修复。
- CI（GitHub Actions）只跑离线自检，不接触任何真实 Key。
