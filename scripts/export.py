# -*- coding: utf-8 -*-
"""输出模块 M6 —— 六表 Excel / CSV + 标准输入模板 + 质检报告。

设计原则：
  1. **零第三方依赖优先**：有 openpyxl 时输出多 sheet 的 .xlsx；
     无 openpyxl 时自动降级为同前缀的多个 .csv（Excel 可双击打开）。
     两种形态的**列名与内容完全一致**，只是载体不同。
  2. **原文件只读**：模板回填输出新文件（原名 + `_已补齐`），绝不覆盖用户输入。
  3. **必填满**：每条输入必有一行输出；查不到也要给「未命中 + 原因 + 建议动作」，
     不允许留空行（用户 2026-10-23 明确要求）。
  4. **可溯源**：每行带 来源源 / 支持源数 / 命中级别 / 置信度 / 质检状态。

命令行：
  python export.py --template [路径]       生成标准输入模板
  python export.py --demo                  生成一份示例输出（自检用）
"""
import csv
import json
from pathlib import Path

try:
    import openpyxl  # type: ignore
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False


# ============================================================ 列定义（唯一真源）

#: 输入模板列（用户按此填写；AI 也据此把任意格式整理成标准格式）
TPL_FIELDS = [
    "任务ID", "任务类型", "省", "市", "区县", "乡镇",
    "关键词", "中心点经度", "中心点纬度", "半径米",
    "名称", "详细地址", "参考经度", "参考纬度", "待查列", "备注",
]

#: 任务类型枚举（与 SKILL.md 功能清单一致；F8 边界查询已按用户要求删除）
TASK_TYPES = [
    "政府驻地", "POI周边", "POI区内分类", "POI关键词",
    "地址转坐标", "坐标转换",
]

#: 输出明细列
OUT_FIELDS = [
    "序号", "任务ID", "名称", "所属地市", "所属区县", "详细地址",
    "经度_WGS84", "纬度_WGS84", "经度_GCJ02", "纬度_GCJ02",
    "来源源", "支持源数", "命中方式", "地址级别", "名称覆盖率", "门牌分",
    "置信度", "质检状态", "存疑原因", "建议动作",
]

#: 质检状态枚举（只有三种，禁止再发明第四种）
QC_STATUS = ["通过", "存疑", "未命中"]

#: 六表名
SIX_TABLES = ["地市表", "区县表", "乡镇表", "明细清单", "汇总表", "校验表"]

#: 汇总表列
SUM_FIELDS = ["地市", "任务数", "通过", "存疑", "未命中", "通过率",
              "中位偏差米", "P90偏差米"]

#: 校验表列
CHECK_FIELDS = ["序号", "任务ID", "名称", "偏差米", "质检状态",
                "问题归类", "已采取动作", "需用户确认"]


# ============================================================ 底层写文件

def _ensure_dir(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)


def write_csv(path, rows, fields=None):
    """写 CSV（utf-8-sig，Excel 双击不乱码）。"""
    path = Path(path)
    _ensure_dir(path)
    fields = fields or (list(rows[0].keys()) if rows else [])
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


# ------------------------------------------------------------ 纯标准库 xlsx

def _col_letter(n: int) -> str:
    """1→A, 27→AA"""
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _xlsx_min(path, sheets) -> bool:
    """零第三方依赖写 .xlsx（OOXML 最小实现）。

    为什么要有这条路：分发环境常常没有 openpyxl（本机 pip 因代理失效也装不上），
    但用户要的是 **真 Excel 文件**，不是一堆 CSV。
    本函数用 zipfile + 手写 XML 直接产出可被 Excel 正常打开的 .xlsx。
    失败时返回 False，由调用方降级 CSV。
    """
    from xml.sax.saxutils import escape
    import zipfile

    def sheet_xml(rows):
        L = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/'
             'spreadsheetml/2006/main"><sheetData>']
        for ri, row in enumerate(rows, 1):
            if not row:
                continue
            L.append(f'<row r="{ri}">')
            for ci, v in enumerate(row, 1):
                ref = f"{_col_letter(ci)}{ri}"
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    L.append(f'<c r="{ref}"><v>{v}</v></c>')
                else:
                    t = escape("" if v is None else str(v))
                    L.append(f'<c r="{ref}" t="inlineStr">'
                             f'<is><t xml:space="preserve">{t}</t></is></c>')
            L.append("</row>")
        L.append("</sheetData></worksheet>")
        return "".join(L)

    def clean(name):
        bad = '[]:*?/\\'
        return "".join(c for c in name if c not in bad)[:31] or "Sheet"

    names = [clean(n) for n in sheets]
    ct = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
          'content-types">',
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats'
          '-package.relationships+xml"/>',
          '<Default Extension="xml" ContentType="application/xml"/>',
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
          'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>']
    for i in range(len(names)):
        ct.append(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" '
                  f'ContentType="application/vnd.openxmlformats-officedocument.'
                  f'spreadsheetml.worksheet+xml"/>')
    ct.append("</Types>")

    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
            '2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
            'openxmlformats.org/officeDocument/2006/relationships/officeDocument"'
            ' Target="xl/workbook.xml"/></Relationships>')

    wb = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/'
          '2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/'
          '2006/relationships"><sheets>']
    for i, nm in enumerate(names, 1):
        wb.append(f'<sheet name="{escape(nm)}" sheetId="{i}" r:id="rId{i}"/>')
    wb.append("</sheets></workbook>")

    wbrels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
              '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
              '2006/relationships">']
    for i in range(len(names)):
        wbrels.append(f'<Relationship Id="rId{i + 1}" Type="http://schemas.'
                      f'openxmlformats.org/officeDocument/2006/relationships/'
                      f'worksheet" Target="worksheets/sheet{i + 1}.xml"/>')
    wbrels.append("</Relationships>")

    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", "".join(ct))
            z.writestr("_rels/.rels", rels)
            z.writestr("xl/workbook.xml", "".join(wb))
            z.writestr("xl/_rels/workbook.xml.rels", "".join(wbrels))
            for i, (nm, rows) in enumerate(sheets.items(), 1):
                fields = list(rows[0].keys()) if rows else []
                data = [fields] + [[r.get(k, "") for k in fields] for r in rows]
                z.writestr(f"xl/worksheets/sheet{i}.xml", sheet_xml(data))
        return True
    except Exception:  # noqa: BLE001
        return False


def write_table(path, sheets):
    """写多表文件（三级降级链）。

    sheets: {表名: [dict, ...]}
      1) 有 openpyxl      → 用它写（样式最好）
      2) 无 openpyxl      → 纯标准库 OOXML 写真 .xlsx（零依赖）
      3) 两者都失败       → 降级 `<主名>_<表名>.csv` 多文件
    三种形态的**列名与内容完全一致**，只是载体不同。
    """
    path = Path(path)
    _ensure_dir(path)
    if HAS_OPENPYXL:
        try:
            wb = openpyxl.Workbook()
            wb.remove(wb.active)
            for name, rows in sheets.items():
                ws = wb.create_sheet(name[:31])
                fields = list(rows[0].keys()) if rows else []
                if fields:
                    ws.append(fields)
                    for r in rows:
                        ws.append([r.get(k, "") for k in fields])
                else:
                    ws.append(["（无数据）"])
            wb.save(path)
            return [path]
        except Exception:  # noqa: BLE001
            pass

    if _xlsx_min(path, sheets):
        return [path]

    stem = path.with_suffix("")
    out = []
    for name, rows in sheets.items():
        out.append(write_csv(f"{stem}_{name}.csv", rows))
    return out


# ============================================================ 标准输入模板

def make_task_template(path):
    """生成标准输入模板（空表 + 填写说明 + 示例三张）。

    用户给任意格式时，AI 先把数据整理成这个模板，请用户在其上补充修正后再执行。
    """
    path = Path(path)
    demo = [{k: "" for k in TPL_FIELDS} for _ in range(3)]
    # 三行示例：覆盖三种最常见任务
    demo[0].update({"任务ID": "T001", "任务类型": "地址转坐标", "省": "广西壮族自治区",
                    "市": "南宁市", "区县": "青秀区",
                    "详细地址": "南宁市青秀区民族大道 100 号",
                    "名称": "广西壮族自治区人民政府", "待查列": "经度,纬度"})
    demo[1].update({"任务ID": "T002", "任务类型": "POI关键词", "省": "广西壮族自治区",
                    "市": "柳州市", "区县": "城中区",
                    "关键词": "小学", "待查列": "经度,纬度,地址"})
    demo[2].update({"任务ID": "T003", "任务类型": "坐标转换", "省": "广西壮族自治区",
                    "市": "桂林市", "参考经度": "110.300000",
                    "参考纬度": "25.270000", "待查列": "WGS84经度,WGS84纬度"})

    guide = [{"列": k, "必填": "是" if k in ("任务ID", "任务类型") else "按任务类型",
              "说明": _tpl_help(k)} for k in TPL_FIELDS]
    guide.append({"列": "任务类型", "必填": "是",
                  "说明": "取值只能是：" + " / ".join(TASK_TYPES)})
    guide.append({"列": "——通用规则——", "必填": "",
                  "说明": "① 城市限定必带，否则同名地点必然误匹配；"
                          "② 详细地址尽量写到门牌号（实测有无门牌号达标率差 14.7 个百分点）；"
                          "③ 只填待查列，已有值的行不重复查（省配额）。"})

    # 统一交给 write_table 走三级降级链（openpyxl → 纯标准库 xlsx → CSV），
    # 不要在这里自己判断 openpyxl —— 2026-10-23 曾因此绕过了零依赖 xlsx 路径。
    return write_table(path, {"任务清单": demo, "填写说明": guide})


def _tpl_help(k: str) -> str:
    return {
        "任务ID": "唯一标识，如 T001；同一批不要重复",
        "任务类型": "见填写说明末行的取值枚举",
        "省": "如 广西壮族自治区；坐标转换类可留空",
        "市": "如 南宁市。**城市限定必带**",
        "区县": "如 青秀区；无门牌号时至少填到这一级",
        "乡镇": "如 伶俐镇；政府驻地类建议填",
        "关键词": "POI 类任务的检索词，如 小学、加油站",
        "中心点经度": "仅 POI周边 用；WGS84",
        "中心点纬度": "仅 POI周边 用；WGS84",
        "半径米": "仅 POI周边 用；>2000 自动切网格防漏",
        "名称": "目标名称（政务网点/机构名），用于辅助互证",
        "详细地址": "**优先按地址查询**，请尽量写全到门牌号",
        "参考经度": "用户已有的参考坐标（用于比对，不是必须）",
        "参考纬度": "同上",
        "待查列": "需要补齐的列，逗号分隔；留空表示查全部",
        "备注": "补充说明，如「该点有两个同名，请确认是山上那个」",
    }.get(k, "")


# ============================================================ 输出六表

def build_six_tables(rows):
    """由明细行构建六表。rows 用 OUT_FIELDS 的键。"""
    detail = [{k: r.get(k, "") for k in OUT_FIELDS}
              for r in rows] or [{k: "" for k in OUT_FIELDS}]

    def _grp(key):
        d = {}
        for r in rows:
            k = r.get(key) or "(未填写)"
            d.setdefault(k, []).append(r)
        return d

    def _stat(items):
        ds = []
        for r in items:
            v = r.get("偏差米")
            if v not in ("", None):
                try:
                    ds.append(float(v))
                except (TypeError, ValueError):
                    pass
        st = {s: sum(1 for r in items if r.get("质检状态") == s)
              for s in QC_STATUS}
        ds.sort()
        n = len(items)
        return {
            "任务数": n, "通过": st["通过"], "存疑": st["存疑"],
            "未命中": st["未命中"],
            "通过率": f"{st['通过'] / n * 100:.1f}%" if n else "0.0%",
            "中位偏差米": int(ds[len(ds) // 2]) if ds else "",
            "P90偏差米": int(ds[int(len(ds) * 0.9) - 1]) if ds else "",
        }

    city = [{"地市": k, **_stat(v)} for k, v in _grp("所属地市").items()]
    area = []
    for k, v in _grp("所属区县").items():
        c = v[0].get("所属地市", "")
        area.append({"地市": c, "区县": k, **_stat(v)})
    town = []
    for k, v in _grp("所属乡镇").items():
        c, a = v[0].get("所属地市", ""), v[0].get("所属区县", "")
        town.append({"地市": c, "区县": a, "乡镇": k, **_stat(v)})

    total = _stat(rows)
    summary = [{"地市": "合计", **total}] + city

    check = []
    for i, r in enumerate(rows, 1):
        if r.get("质检状态") == "通过":
            continue
        check.append({
            "序号": i, "任务ID": r.get("任务ID", ""),
            "名称": r.get("名称", ""), "偏差米": r.get("偏差米", ""),
            "质检状态": r.get("质检状态", ""),
            "问题归类": r.get("问题归类", "") or _guess_reason(r),
            "已采取动作": r.get("建议动作", "") or "已标注，未改动坐标",
            "需用户确认": r.get("需用户确认", "") or "是",
        })
    if not check:
        check = [{"序号": "", "任务ID": "", "名称": "（全部通过，无异常）",
                  "偏差米": "", "质检状态": "通过", "问题归类": "",
                  "已采取动作": "", "需用户确认": "否"}]

    return {"地市表": city, "区县表": area, "乡镇表": town,
            "明细清单": detail, "汇总表": summary, "校验表": check}


def _guess_reason(r) -> str:
    """按已知归因规则猜问题归类（供人工复核，不替代判定）。"""
    if r.get("质检状态") == "未命中":
        return "图商无收录 / 地址信息不足"
    if (r.get("支持源数") or 0) <= 1:
        return "仅单源响应，无法交叉验真（实测单源达标率仅 55.6%）"
    if "政务" in (r.get("名称") or ""):
        return "政务服务中心类：图商收录整栋楼，专区位置可能偏差"
    return "多源分歧或中等偏差，需人工核对"


# ============================================================ 质检报告

def write_qc_report(path, meta, rows, extra=None):
    """写质检报告（Markdown，五段固定格式）。

    meta: {"任务ID","模块","条数","起止","版本","计划请求数","实际请求数",
           "计划耗时","实际耗时"}
    """
    path = Path(path)
    _ensure_dir(path)
    st = {s: sum(1 for r in rows if r.get("质检状态") == s) for s in QC_STATUS}
    n = len(rows)
    ds = []
    for r in rows:
        v = r.get("偏差米")
        if v not in ("", None):
            try:
                ds.append(float(v))
            except (TypeError, ValueError):
                pass
    ok = sum(1 for d in ds if d <= 100)
    strict = ok / n * 100 if n else 0.0
    ds.sort()

    L = []
    A = L.append
    A("# 质检报告 · 地理数据采集器")
    A("")
    A("## ① 任务概览")
    A("")
    A("| 项 | 值 |")
    A("|---|---|")
    for k in ("任务ID", "模块", "条数", "起止", "版本"):
        A(f"| {k} | {meta.get(k, '')} |")
    A("")
    A("## ② 计划对账")
    A("")
    A("| 项 | 计划 | 实际 |")
    A("|---|---|---|")
    A(f"| 请求数 | {meta.get('计划请求数', '')} | {meta.get('实际请求数', '')} |")
    A(f"| 耗时 | {meta.get('计划耗时', '')} | {meta.get('实际耗时', '')} |")
    A(f"| 分批 | {meta.get('分批', '')} | {meta.get('分批', '')} |")
    A("")
    A("## ③ 质量结论")
    A("")
    A("| 口径 | 计算 | 结果 |")
    A("|---|---|---|")
    A(f"| **主口径**（分母=总条数，留空/存疑/未命中均计不达标） "
      f"| {ok}/{n} | **{strict:.1f}%** |")
    if ds:
        A(f"| 参考口径（分母=有结果条数 {len(ds)}） | {ok}/{len(ds)} | "
          f"{ok / len(ds) * 100:.1f}% |")
    A(f"| 通过 | {st['通过']} | |")
    A(f"| 存疑 | {st['存疑']} | |")
    A(f"| 未命中 | {st['未命中']} | |")
    if ds:
        A(f"| 中位偏差 | {int(ds[len(ds) // 2])} m | |")
        A(f"| P90 偏差 | {int(ds[int(len(ds) * 0.9) - 1])} m | |")
    A("")
    A("## ④ 异常清单")
    A("")
    bad = [r for r in rows if r.get("质检状态") != "通过"]
    if not bad:
        A("全部通过，无异常条目。")
    else:
        A("| # | 名称 | 偏差 | 问题归类 | 已采取动作 |")
        A("|---|---|---|---|---|")
        for i, r in enumerate(bad[:200], 1):
            A(f"| {i} | {r.get('名称', '')} | {r.get('偏差米', '')} | "
              f"{_guess_reason(r)} | {r.get('建议动作', '') or '已标注'} |")
        if len(bad) > 200:
            A(f"| … | 其余 {len(bad) - 200} 条见 校验表 | | | |")
    A("")
    A("## ⑤ 遗留与建议")
    A("")
    if extra:
        for k, v in extra.items():
            A(f"- **{k}**：{v}")
    A("- 存疑条目**未改动坐标**，仅标注；请按「建议动作」人工核对。")
    A("- 需要坐标核对的条目，明细清单附 GCJ-02 列，可直接在高德地图取点比对。")
    A("")
    path.write_text("\n".join(L), encoding="utf-8")
    return path


# ============================================================ 自检

def _demo():
    rows = []
    for i in range(1, 6):
        rows.append({
            "序号": i, "任务ID": f"T{i:03d}", "名称": f"示例点{i}",
            "所属地市": "南宁市", "所属区县": "青秀区",
            "详细地址": "南宁市青秀区民族大道 100 号",
            "经度_WGS84": 108.377045, "纬度_WGS84": 22.742956,
            "经度_GCJ02": "", "纬度_GCJ02": "",
            "来源源": "amap,baidu", "支持源数": 2, "命中方式": "①地址多源一致",
            "地址级别": "门址", "名称覆盖率": 0.85, "门牌分": 3,
            "置信度": "高", "质检状态": "通过", "存疑原因": "", "建议动作": "",
            "偏差米": 10 if i < 4 else 3500,
        })
    rows[-1]["质检状态"] = "存疑"
    rows[-1]["存疑原因"] = "单源响应，无法交叉验真"
    rows[-1]["建议动作"] = "请在地图上核对后回填"
    return rows


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="输出模块 M6 自检")
    ap.add_argument("--template", nargs="?", const="assets/模板_采集任务清单.xlsx",
                    help="生成标准输入模板")
    ap.add_argument("--demo", action="store_true", help="生成示例输出")
    a = ap.parse_args()

    print(f"openpyxl: {'可用' if HAS_OPENPYXL else '不可用 → 用纯标准库写 xlsx'}")
    if a.template:
        p = make_task_template(Path(a.template))
        print("模板已生成:", p)
    if a.demo:
        rows = _demo()
        p = write_table(Path("output/示例_六表输出.xlsx"), build_six_tables(rows))
        print("示例六表:", p)
        r = write_qc_report(Path("output/示例_质检报告.md"),
                            {"任务ID": "DEMO", "模块": "地址转坐标", "条数": 5,
                             "起止": "-", "版本": "demo",
                             "计划请求数": 15, "实际请求数": 15,
                             "计划耗时": "10s", "实际耗时": "9s", "分批": "1 批"},
                            rows)
        print("示例质检报告:", r)
    if not a.template and not a.demo:
        print("用法: python export.py --template | --demo")
