# -*- coding: utf-8 -*-
"""M4 质检模块（先行件）：混淆点定向复核 + 自适应抽检

设计依据（2026-10-20 用户定案）
------------------------------
* 不是每个点都跑四源验真 —— **只对易混淆点做定向复核**，其余走抽检；
* 抽检强度用工业抽样检验的「放宽—正常—加严」转移规则动态调节；
* 自然村只要有名字，图商就能取准，因此不再一律停手，交给 R5/R6 控质量。

阈值（经验初值，跑一批后按实测调整）
----------------------------------
  合格   ≤300 m      存疑 300~1000 m（引第三源仲裁）      不合格 >1000 m
  抽样率：正常 5% / 放宽 2% / 加严 15%
"""
from __future__ import annotations

import random

import gc_common as G

PASS_M = 300.0
SUSPECT_M = 1000.0

RATES = {"正常": 0.05, "放宽": 0.02, "加严": 0.15}
#: 距所属行政村中心的合理上限（超出 → 定向复核）
FAR_FROM_VILLAGE_M = 3000.0


# ---------------------------------------------------------------- 自适应抽检

class SamplePlan:
    """R6 自适应抽检状态机。

    转移规则：
      正常 5% → 连续 5 批不合格率 ≤1% → 放宽 2%
      任一批出现不合格（偏差 >300 m） → 加严 15%，并标记需回查同批已采点
    """

    def __init__(self, seed: int = 20261020):
        self.mode = "正常"
        self.good_batches = 0
        self.need_recheck = False
        self._rnd = random.Random(seed)

    @property
    def rate(self) -> float:
        return RATES[self.mode]

    def should_sample(self, idx: int) -> bool:
        """按索引确定性抽样（同一批重复运行结论一致，便于复现）。"""
        return self._rnd.random() < self.rate

    def report_batch(self, bad_rate: float, batch_size: int = 0):
        """每批跑完调用一次，更新抽样状态。"""
        if bad_rate > 0:
            self.mode = "加严"
            self.good_batches = 0
            self.need_recheck = True
            return f"批次不合格率 {bad_rate:.1%} > 0 → 加严至 {RATES['加严']:.0%}，需回查同批已采点"
        self.good_batches += 1
        if self.good_batches >= 5 and self.mode == "正常":
            self.mode = "放宽"
            return f"连续 {self.good_batches} 批合格 → 放宽至 {RATES['放宽']:.0%}"
        if self.mode == "加严" and self.good_batches >= 3:
            self.mode = "正常"
            self.need_recheck = False
            return "加严后连续 3 批合格 → 回落正常档"
        return f"保持「{self.mode}」档，抽样率 {self.rate:.0%}"

    def summary(self) -> dict:
        return {"档位": self.mode, "抽样率": self.rate,
                "连续合格批数": self.good_batches, "待回查": self.need_recheck}


# ---------------------------------------------------------------- 单条判定

def qc_record(rec: dict, truth: tuple | None = None,
              village_center: tuple | None = None) -> dict:
    """判定一条采集记录。

    truth 为已知真值（抽检或黄金样本）；无真值时只做几何合理性检查。
    """
    out = {"名称": rec.get("名称", ""), "判定": "未验", "偏差米": None, "原因": []}
    lon, lat = rec.get("lon"), rec.get("lat")
    if lon is None or lat is None:
        out["原因"].append("无坐标")
        out["判定"] = "不合格"
        return out

    if truth:
        d = G.haversine_m(lon, lat, truth[0], truth[1])
        out["偏差米"] = round(d)
        if d <= PASS_M:
            out["判定"] = "合格"
        elif d <= SUSPECT_M:
            out["判定"] = "存疑"
            out["原因"].append(f"偏差 {d:.0f} m，需第三源仲裁")
        else:
            out["判定"] = "不合格"
            out["原因"].append(f"偏差 {d:.0f} m > {SUSPECT_M:.0f} m")
    else:
        out["判定"] = "未验（无真值，仅几何检查）"

    if village_center:
        dv = G.haversine_m(lon, lat, village_center[0], village_center[1])
        out["距行政村米"] = round(dv)
        if dv > FAR_FROM_VILLAGE_M:
            out["原因"].append(f"距行政村中心 {dv:.0f} m > {FAR_FROM_VILLAGE_M:.0f} m，需定向复核")
            if out["判定"] == "合格":
                out["判定"] = "存疑"
    return out


def verify_admin(lon: float, lat: float, expect_area: str = "",
                 expect_town: str = "") -> dict:
    """用逆地理编码核验行政区归属（唯一能发现"名称对但归属错"的手段）。"""
    rg = G.amap_regeo(lon, lat)
    if not rg:
        return {"核验": "失败", "原因": "逆地理编码无返回"}
    hit = True
    reasons = []
    if expect_area and expect_area not in (rg.get("区县") or ""):
        hit = False
        reasons.append(f"区县为「{rg.get('区县')}」，期望「{expect_area}」")
    if expect_town and expect_town not in (rg.get("乡镇") or ""):
        hit = False
        reasons.append(f"乡镇为「{rg.get('乡镇')}」，期望「{expect_town}」")
    return {"核验": "通过" if hit else "不通过", "区县": rg.get("区县", ""),
            "乡镇": rg.get("乡镇", ""), "地址": rg.get("地址", ""),
            "原因": reasons}


# ---------------------------------------------------------------- 四层质检漏斗 L0-L3

#: 广西经纬度合理范围（L0 越界判据；跨省任务请按实际范围替换）
BBOX_GX = (104.0, 112.5, 20.5, 26.5)   # minlon maxlon minlat maxlat


def robust_upper(vals: list, floor: float = 3000.0) -> float:
    """自适应上界：中位数 + 3×MAD×1.4826，且不低于 floor。

    用户教法（优于硬编码 3 km）：「其他村都没超 3 km，突然来个 5733 m 的，那肯定存疑」。
    拿**同批数据自身的分布**当尺子，山区/平原都不会系统性误判。
    """
    if not vals:
        return floor
    s = sorted(vals)
    n = len(s)
    med = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    mad = sorted(abs(v - med) for v in vals)[n // 2]
    return max(med + 3 * mad * 1.4826, floor)


def screen_l0(rows: list) -> list:
    """L0 零成本筛查：格式 / 越界 / 重复。API 成本 0，覆盖 100%。"""
    bad = []
    seen = {}
    for i, r in enumerate(rows, 1):
        name = r.get("名称", "")
        lon, lat = r.get("经度_WGS84"), r.get("纬度_WGS84")
        try:
            lon, lat = float(lon), float(lat)
        except (TypeError, ValueError):
            bad.append({"序号": i, "名称": name, "层": "L0",
                        "问题": "坐标缺失或非数值", "处置": "判不合格"})
            continue
        lo1, lo2, la1, la2 = BBOX_GX
        if not (lo1 <= lon <= lo2 and la1 <= lat <= la2):
            bad.append({"序号": i, "名称": name, "层": "L0",
                        "问题": f"坐标越界（{lon:.4f},{lat:.4f} 超出任务范围）",
                        "处置": "判不合格"})
        key = (round(lon, 5), round(lat, 5))
        if key in seen:
            bad.append({"序号": i, "名称": name, "层": "L0",
                        "问题": f"与 #{seen[key]} 坐标完全重复", "处置": "标记待去重"})
        else:
            seen[key] = i
    return bad


def screen_l1(rows: list, group_key: str = "所属区县",
              anchors: dict | None = None, min_group: int = 3) -> tuple:
    """L1 零成本筛查：距锚点的距离，超过自适应上界判离群。

    **2026-10-23 实测修正（重要）**：
    原实现按「地市」分组 + 组内中位数当锚点，在广西实测**富集倍数仅 0.69×（无效）**。
    根因：一个地市内县城间距本就是 50~200 km（桂林自适应上界被撑到 224 km），
    组内距离分布是双峰的，自适应上界必然失效 —— **分组粒度错了，算法再对也没用**。

    正解：
      ① **分组粒度下沉到区县**（同一区县内的点应彼此靠近）；
      ② 优先用**外部锚点**（区县政府驻地，来自 F2 政府驻地功能或本地缓存）；
      ③ 无锚点且组内样本 < min_group 时，**诚实跳过该组**，不用无效判据误报。

    返回 (问题清单, 上界dict, 跳过组数)
    """
    import statistics

    def _xy(r):
        try:
            return float(r.get("经度_WGS84")), float(r.get("纬度_WGS84"))
        except (TypeError, ValueError):
            return None

    groups = {}
    for i, r in enumerate(rows, 1):
        xy = _xy(r)
        if xy:
            groups.setdefault(r.get(group_key) or "(未分组)", []).append((i, r, xy))

    bad, uppers, skipped = [], {}, 0
    for g, items in groups.items():
        anchor = (anchors or {}).get(g)
        src = "外部锚点"
        if anchor is None:
            if len(items) < min_group:
                skipped += 1          # 样本不足，不判（宁可不判，不可误判）
                continue
            lons = [x[2][0] for x in items]
            lats = [x[2][1] for x in items]
            anchor = (statistics.median(lons), statistics.median(lats))
            src = "组内中位数"
        dists = [G.haversine_m(x[2][0], x[2][1], anchor[0], anchor[1])
                 for x in items]
        up = robust_upper(dists)
        uppers[g] = up
        for (i, r, xy), d in zip(items, dists):
            if d > up:
                bad.append({"序号": i, "名称": r.get("名称", ""), "层": "L1",
                            "问题": f"距「{g}」{src} {d:.0f} m > 上界 {up:.0f} m",
                            "处置": "进 L2 定向验真"})
    return bad, uppers, skipped


def risk_score(r: dict) -> float:
    """风险分（越高越该花钱验真）。用于把有限验真预算投到最可能错的地方。"""
    s = 0.0
    try:
        src = int(r.get("支持源数") or 0)
    except (TypeError, ValueError):
        src = 0
    if src <= 1:
        s += 3.0                      # 实测单源达标率仅 55.6%
    elif src == 2:
        s += 0.5
    try:
        cov = float(r.get("名称覆盖率") or 0)
    except (TypeError, ValueError):
        cov = 0.0
    if cov < 0.4:
        s += 1.5
    if r.get("地址级别") in ("乡镇", "道路", ""):
        s += 1.5
    if "政务" in (r.get("名称") or ""):
        s += 1.0                      # 政务服务中心类实测仅 64.9%
    return s


def plan_l2_l3(rows: list, l1_flags: list | None = None,
               plan: "SamplePlan | None" = None) -> dict:
    """L2/L3 分层计划：高风险子集定向验真，低风险子集抽样监控。

    核心原理（需求文档 §4.5）：**验真成本 ∝ 风险**，不是验得越多越准。
    本函数只出计划不真跑，便于离线回归与预算评估。
    """
    plan = plan or SamplePlan()
    l1_idx = {b["序号"] for b in (l1_flags or [])}
    l2, l3 = [], []
    for i, r in enumerate(rows, 1):
        if i in l1_idx or risk_score(r) >= 3.0:
            l2.append({"序号": i, "名称": r.get("名称", ""),
                       "风险分": risk_score(r), "动作": "第二源定向验真"})
        elif plan.should_sample(i):
            l3.append({"序号": i, "名称": r.get("名称", ""),
                       "风险分": risk_score(r), "动作": "抽样监控"})
    return {
        "L2_定向验真": l2, "L3_抽样": l3,
        "L2条数": len(l2), "L3条数": len(l3),
        "总条数": len(rows),
        "验真覆盖率": f"{(len(l2) + len(l3)) / max(1, len(rows)) * 100:.1f}%",
        "预计额外请求": len(l2) * 2 + len(l3),
        "抽样档位": plan.mode,
    }


def funnel(rows: list, anchors: dict | None = None) -> dict:
    """串起四层漏斗，返回完整质检结论（离线部分真实执行，联网部分出计划）。

    anchors: {区县名: (lon, lat)} 外部锚点（区县政府驻地）。无锚点则按组内中位数，
    组内样本不足的组会跳过 —— 宁可不判，不可误判。
    """
    l0 = screen_l0(rows)
    l1, uppers, skipped = screen_l1(rows, anchors=anchors)
    total_g = skipped + len(uppers)

    # 样本结构适配（2026-10-23 实测）：
    # L1 空间离群判据的原生场景是「一个行政村下多个自然村，彼此应靠近」。
    # 在「一区县一点」的任务结构下（133 条散布 100+ 区县），同组内 1~2 条样本
    # 无法构成分布，实测富集倍数 0.00× —— 抓到的全是对的、漏掉的全是错的。
    # 此时必须**声明不适用并退出**，而不是硬产出一个反向的判据。
    if anchors:
        l1_note = "适用（外部锚点）"
    elif total_g and skipped / total_g < 0.5:
        l1_note = "适用（组内中位数）"
    else:
        l1_note = (f"**不适用**：{skipped}/{total_g} 组样本不足（多为「一区县一点」结构），"
                   f"本层已跳过以避免误报。改用外部锚点（区县政府驻地）可启用")
        l1 = []

    p = plan_l2_l3(rows, l1)
    return {
        "L0": l0, "L1": l1, "L2_L3计划": p,
        "自适应上界": {k: round(v) for k, v in uppers.items()},
        "L1跳过组数": skipped, "L1适用性": l1_note,
        "锚点来源": "外部锚点" if anchors else "组内中位数（组样本不足则跳过）",
        "结论": ("全部通过零成本筛查" if not l0 and not l1
                else f"L0 命中 {len(l0)} 条、L1 命中 {len(l1)} 条，需按计划验真"),
    }


# ---------------------------------------------------------------- 离线自检

def selftest(verbose: bool = True) -> int:
    fails = []
    print("[M4-先行件] 质检模块自检")
    print("-" * 62)

    # 1) 抽样转移规则
    sp = SamplePlan(seed=1)
    ok = abs(sp.rate - 0.05) < 1e-9
    print(f"[1] 初始档位            {sp.mode} {sp.rate:.0%}  {'[OK]' if ok else '[FAIL]'}")
    if not ok:
        fails.append("初始抽样率错误")
    msg = sp.report_batch(0.10)
    ok = sp.mode == "加严" and sp.need_recheck
    print(f"[2] 出现不合格→加严     {sp.mode} {sp.rate:.0%}，待回查={sp.need_recheck}  "
          f"{'[OK]' if ok else '[FAIL]'}")
    if not ok:
        fails.append("加严转移失败")
    for _ in range(3):
        sp.report_batch(0.0)
    ok = sp.mode == "正常"
    print(f"[3] 加严后连合格→回落   {sp.mode} {sp.rate:.0%}  {'[OK]' if ok else '[FAIL]'}")
    if not ok:
        fails.append("回落转移失败")
    for _ in range(5):
        sp.report_batch(0.0)
    ok = sp.mode == "放宽"
    print(f"[4] 连续合格→放宽       {sp.mode} {sp.rate:.0%}  {'[OK]' if ok else '[FAIL]'}")
    if not ok:
        fails.append("放宽转移失败")

    # 2) 抽样可复现（同 seed 同序列；注意必须复用同一个实例，重播 seed 会退化成同一张牌）
    _sp_a, _sp_b = SamplePlan(seed=7), SamplePlan(seed=7)
    a = [_sp_a.should_sample(i) for i in range(200)]
    b = [_sp_b.should_sample(i) for i in range(200)]
    ok = a == b and any(a)
    print(f"[5] 抽样可复现          抽中 {sum(a)}/200  {'[OK]' if ok else '[FAIL]'}")
    if not ok:
        fails.append("抽样不可复现")

    # 3) 单条判定阈值（0 m / 约 500 m / 约 1112 m 三档）
    r1 = qc_record({"名称": "t", "lon": 110.774202, "lat": 23.729496},
                   truth=(110.774202, 23.729500))
    r2 = qc_record({"名称": "t", "lon": 110.774202, "lat": 23.729496},
                   truth=(110.774202, 23.734000))     # ≈ 500 m
    r3b = qc_record({"名称": "t", "lon": 110.774202, "lat": 23.729496},
                    truth=(110.774202, 23.739496))    # ≈ 1112 m
    ok = (r1["判定"] == "合格" and r2["判定"] == "存疑" and r3b["判定"] == "不合格")
    print(f"[6] 阈值判定            {r1['偏差米']}m={r1['判定']}，{r2['偏差米']}m={r2['判定']}，"
          f"{r3b['偏差米']}m={r3b['判定']}  {'[OK]' if ok else '[FAIL]'}")
    if not ok:
        fails.append("阈值判定错误")

    # 4) 距行政村过远 → 降级为存疑
    r3 = qc_record({"名称": "t", "lon": 110.80, "lat": 23.70},
                   truth=(110.800001, 23.700001),
                   village_center=(110.760797, 23.733605))
    ok = r3["判定"] == "存疑"
    print(f"[7] 距行政村过远降级     {r3.get('距行政村米')} m → {r3['判定']}  "
          f"{'[OK]' if ok else '[FAIL]'}")
    if not ok:
        fails.append("距离降级失效")

    # 5) L0 零成本筛查：越界 + 重复（API 成本 0）
    _l0_rows = [
        {"名称": "正常点", "经度_WGS84": 108.30, "纬度_WGS84": 22.80, "所属地市": "南宁市"},
        {"名称": "越界点", "经度_WGS84": 116.00, "纬度_WGS84": 39.90, "所属地市": "南宁市"},
        {"名称": "重复点", "经度_WGS84": 108.30, "纬度_WGS84": 22.80, "所属地市": "南宁市"},
        {"名称": "缺坐标", "经度_WGS84": "", "纬度_WGS84": "", "所属地市": "南宁市"},
    ]
    _l0 = screen_l0(_l0_rows)
    _kinds = {("越界" if "越界" in b["问题"] else
               "重复" if "重复" in b["问题"] else
               "缺坐标" if "缺失" in b["问题"] else "?") for b in _l0}
    ok = len(_l0) == 3 and _kinds == {"越界", "重复", "缺坐标"}
    print(f"[8] L0 零成本筛查       命中 {len(_l0)} 条（{sorted(_kinds)}）  "
          f"{'[OK]' if ok else '[FAIL]'}")
    if not ok:
        fails.append("L0 筛查未抓全越界/重复/缺坐标")

    # 6) L1 离群筛查（零 API）：5 个聚集点 + 1 个远点（分组粒度 = 区县）
    _l1_rows = [{"名称": f"n{i}", "经度_WGS84": 108.30 + i * 0.001,
                 "纬度_WGS84": 22.80 + i * 0.001, "所属区县": "青秀区"}
                for i in range(5)]
    _l1_rows.append({"名称": "离群点", "经度_WGS84": 110.50, "纬度_WGS84": 24.80,
                     "所属区县": "青秀区"})
    _l1, _ups, _skip = screen_l1(_l1_rows)
    ok = len(_l1) == 1 and _l1[0]["名称"] == "离群点"
    print(f"[9] L1 离群筛查         上界 {_ups.get('青秀区', 0):.0f} m，"
          f"命中 {len(_l1)} 条 → {_l1[0]['名称'] if _l1 else '(无)'}  "
          f"{'[OK]' if ok else '[FAIL]'}")
    if not ok:
        fails.append("L1 未抓出离群点")

    # 6b) 外部锚点优先 + 样本不足组诚实跳过（2026-10-23 修正后的行为）
    _l1b = [{"名称": "a", "经度_WGS84": 108.30, "纬度_WGS84": 22.80, "所属区县": "A区"},
            {"名称": "b", "经度_WGS84": 108.31, "纬度_WGS84": 22.81, "所属区县": "A区"},
            {"名称": "c", "经度_WGS84": 110.50, "纬度_WGS84": 24.80, "所属区县": "A区"}]
    _b1, _u1, _s1 = screen_l1(_l1b, anchors={"A区": (108.30, 22.80)})
    _b2, _u2, _s2 = screen_l1([{"名称": "x", "经度_WGS84": 108.3,
                               "纬度_WGS84": 22.8, "所属区县": "B区"}])
    ok_b = (len(_b1) == 1 and _b1[0]["名称"] == "c" and _s2 == 1 and not _b2)
    print(f"[9b] 锚点优先/样本不足跳过 锚点组命中 {len(_b1)} 条，"
          f"单样本组跳过 {_s2} 组且零误报  {'[OK]' if ok_b else '[FAIL]'}")
    if not ok_b:
        fails.append("外部锚点或跳过逻辑失效")

    # 7) 自适应上界不低于 floor（防样本过近时上界塌成 0）
    ok = robust_upper([10.0, 12.0, 11.0]) >= 3000.0
    print(f"[10] 自适应上界下限      {robust_upper([10.0, 12.0, 11.0]):.0f} m ≥ 3000  "
          f"{'[OK]' if ok else '[FAIL]'}")
    if not ok:
        fails.append("自适应上界低于 floor")

    # 8) 四层漏斗串起来：结构完整 + 覆盖率可解释
    _f = funnel(_l0_rows + _l1_rows)
    ok = all(k in _f for k in ("L0", "L1", "L2_L3计划", "结论")) and \
        _f["L2_L3计划"]["总条数"] == len(_l0_rows) + len(_l1_rows)
    print(f"[11] 四层漏斗串联       L0 {len(_f['L0'])} / L1 {len(_f['L1'])} / "
          f"L2 {_f['L2_L3计划']['L2条数']} / L3 {_f['L2_L3计划']['L3条数']}  "
          f"{'[OK]' if ok else '[FAIL]'}")
    if not ok:
        fails.append("四层漏斗结构不完整")

    print("-" * 62)
    if fails:
        print(f"自检结论：FAIL —— {'；'.join(fails)}")
        return 1
    print("自检结论：PASS（离线项全部通过）")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(selftest())
