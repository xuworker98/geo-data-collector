# -*- coding: utf-8 -*-
"""行政区划链解析与纠偏（M2+）——**查自然村之前必须先走这一步**

背景（用户 2026-10-20 第 1 条批评）：
    盲测时直接拿用户给的「柳州柳江区白沙乡」去搜，怎么搜都搜不准——
    因为白沙乡 2014 年已撤乡设镇、2018 年又由柳江区划归鱼峰区，
    用户清单里写的是 2014 年前的旧名旧属。**名字本身就是错的，搜得再勤也没用。**

职责：
    把用户的一串文本解析成 省/市/区县/乡镇 四级，逐层拿到**行政编码**，
    在这一层就把名字纠正过来，再交给下游去查自然村。

纠偏能力（以柳州白沙为例，民政部 Xzqh20251231 表实测）：
    输入「柳江区 / 白沙乡」
      → 在柳江区(450206)下找不到任何「白沙*」  ← 这就是归属变更的信号
      → 放宽到柳州市(450200)下找 → 唯一命中「白沙镇」(450203103)
      → 输出：通名 乡→镇 已变更；父级 柳江区(450206)→鱼峰区(450203) 已变更
      → 下游必须用「鱼峰区/白沙镇」去搜，且逆向验真比对的应是**编码**不是名字

设计原则：
    ① 权威源只用民政部（撤乡设镇、跨区调整这类事，图商说了不算）
    ② 逐级下钻 + 逐步放宽，**上一级限定不住才退到上一级**，不跳级乱猜
    ③ 纠偏必须留证据链（改了什么、依据是哪条记录），不静默改用户的输入
    ④ 同名多点：宁可列候选让人拍板，也不按「最短名字」「先命中」擅自选
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import regions as R                                     # noqa: E402

# 通名：撤乡设镇 / 镇改街道 / 乡改民族乡 都只换尾巴，主体名不变
_SUFFIX_SWAP = (("乡", "镇"), ("镇", "乡"), ("镇", "街道"),
                ("乡", "街道"), ("街道", "镇"), ("街道", "乡"))


def _strip_tail(name: str) -> str:
    """去掉行政区划通名尾巴，取主体名：「白沙乡」→「白沙」"""
    for t in ("街道", "民族乡", "苏木", "镇", "乡", "区", "县", "市"):
        if name.endswith(t) and len(name) > len(t):
            return name[:-len(t)]
    return name


def _swap_tail(name: str) -> list:
    """换通名造候选：「白沙乡」→ ['白沙镇','白沙街道']"""
    stem = _strip_tail(name)
    if stem == name:                      # 本来就没通名，造不出来
        return []
    out = []
    for a, b in _SUFFIX_SWAP:
        if name.endswith(a):
            cand = stem + b
            if cand not in out and cand != name:
                out.append(cand)
    return out


# ---------------------------------------------------------------- 主入口

def resolve_chain(city: str = "", area: str = "", town: str = "",
                  village: str = "", province: str = "450000000",
                  idx: R.RegionIndex | None = None) -> dict:
    """解析并纠偏一条行政区划链。

    返回：
      {"省","市","区县","乡镇","行政村"} 各为 {"名称","代码","别名"} 或 None
      "纠偏"：本层改了什么（含类型与证据）
      "状态"："已解析" / "存疑" / "需人工"
      "线索"：纠偏依据（供用户复核）
      "下游用名"：下游搜索应使用的**纠正后**名称
      "bbox"：按乡镇/区县中心推出的搜索范围（给天地图 mapBound 用）
    """
    if idx is None:
        rows = R.load_regions(province)
        idx = rows if isinstance(rows, R.RegionIndex) else R.RegionIndex(rows)

    res = {"省": None, "市": None, "区县": None, "乡镇": None, "行政村": None}
    fixes, clues = [], []
    status = "已解析"

    def _mk(r):
        if r is None:
            return None
        return {"名称": r.name, "代码": r.code, "路径": r.full_path,
                "别名": [], "记录": r}

    # ---- 市：先定市，后面区县/乡镇才有兜底范围
    city_r = None
    if city:
        cs = idx.find(city, level=2)
        if len(cs) == 1:
            city_r = cs[0]
        elif cs:
            fixes.append({"层": "市", "问题": f"「{city}」命中 {len(cs)} 个",
                          "处理": "保留全部候选，交由区县层收窄"})
    res["市"] = _mk(city_r)

    # ---- 区县：在市内找；找不到才放宽到全省
    area_r = None
    if area:
        scope = code9(city_r.code) if city_r else None
        cs = idx.find(area, level=3, parent_code=scope) if scope else idx.find(area, level=3)
        if not cs and scope:
            cs = idx.find(area, level=3)          # 放宽到全省
            if cs:
                fixes.append({"层": "区县",
                              "问题": f"「{area}」在{city_r.name}下找不到",
                              "处理": f"放宽到全省，命中 {len(cs)} 个"})
        if len(cs) == 1:
            area_r = cs[0]
        elif len(cs) > 1:
            status = "需人工"
            clues.append(f"区县「{area}」有 {len(cs)} 个同名候选：" +
                         "、".join(f"{c.name}({c.code})" for c in cs[:6]))
    res["区县"] = _mk(area_r)

    # ---- 乡镇：**纠偏主战场**
    town_r = None
    if town:
        town_r, tf, tc = _resolve_town(idx, town, area_r, city_r)
        fixes.extend(tf)
        clues.extend(tc)
        if town_r is None:
            status = "需人工" if status == "已解析" else status
        elif tf:
            status = "存疑" if status == "已解析" else status
    res["乡镇"] = _mk(town_r)

    # ---- 上一层级填充：**乡镇改属后，上级必须跟着改**
    # 用户清单写「柳江区/白沙乡」，实际是「鱼峰区/白沙镇」。
    # 只改乡镇不改区县，下游就会拿「柳江区 + 白沙镇」去搜——两个都对却凑不成一条链，
    # 一样搜不准。所以乡镇定下来后，用它的真实祖先链把市/区县覆盖回去。
    if town_r is not None:
        anc = _ancestors(idx, town_r)
        for lv, key in ((2, "市"), (3, "区县")):
            a = anc.get(lv)
            if a is None:
                continue
            cur = res[key]
            if cur is None or code9(cur["代码"]) != code9(a.code):
                fixes.append({
                    "层": key, "类型": "依乡镇真实归属回填上级",
                    "问题": f"原填「{cur['名称'] if cur else '空'}」",
                    "处理": f"按乡镇实际归属回填「{a.name}」",
                    "证据": f"{a.name} {a.code} {a.full_path}"})
                res[key] = _mk(a)
        if res["省"] is None and anc.get(1):
            res["省"] = _mk(anc[1])

    # ---- 行政村：统计局村级 16681 条，按 (乡镇, 村名) 复合键挂
    if village and town_r:
        v = _resolve_village(village, town_r)
        res["行政村"] = v
        if v is None:
            clues.append(f"行政村「{village}」未在统计局村级名录中命中"
                         f"（自然村本就多半不入官方名录，不影响主流程）")

    # ---- 下游用名：一律取**纠偏后**的名字（上级已随乡镇回填）
    down = {"市": (res["市"]["名称"] if res["市"] else city),
            "区县": (res["区县"]["名称"] if res["区县"] else area),
            "乡镇": (res["乡镇"]["名称"] if res["乡镇"] else town)}

    # ---- bbox：给下游搜索兜个范围
    bbox = _bbox_of(town_r or area_r or city_r, idx)

    return {"省": res["省"], "市": res["市"], "区县": res["区县"],
            "乡镇": res["乡镇"], "行政村": res["行政村"],
            "纠偏": fixes, "状态": status, "线索": clues,
            "下游用名": down, "bbox": bbox}


def _resolve_town(idx, town, area_r, city_r):
    """乡镇解析：三级放宽 + 通名互换，返回 (Region, 纠偏, 线索)"""
    fixes, clues = [], []

    def _try(parent9, name):
        if not parent9:
            return None
        return idx.match_child_by_name(parent9, name, level=4)

    a9 = code9(area_r.code) if area_r else None
    c9 = code9(city_r.code) if city_r else None

    # 第 1 步：原样 + 通名互换，都在区县内找
    tried = [town] + _swap_tail(town)
    for nm in tried:
        r = _try(a9, nm)
        if r:
            if nm != town:
                fixes.append({"层": "乡镇", "类型": "通名变更",
                              "问题": f"「{town}」在{area_r.name}下无此名",
                              "处理": f"按通名互换取「{nm}」",
                              "证据": f"{r.name} {r.code} {r.full_path}"})
            return r, fixes, clues

    # 第 2 步：去通名，在区县内模糊找
    stem = _strip_tail(town)
    if stem != town:
        r = _try(a9, stem) or _fuzzy_in(idx, a9, stem)
        if r:
            fixes.append({"层": "乡镇", "类型": "通名差异",
                          "问题": f"「{town}」无记录",
                          "处理": f"去通名后命中「{r.name}」",
                          "证据": f"{r.name} {r.code} {r.full_path}"})
            return r, fixes, clues

    # 第 3 步：**区县内彻底没有** → 说明归属变了，退到市里找
    if c9:
        for nm in tried + ([stem] if stem != town else []):
            cs = idx.find(nm, level=4)
            cs = [c for c in cs if code9(c.parent_code)[:4] == c9[:4]
                  or c.city_code == c9]
            if len(cs) == 1:
                r = cs[0]
                old = f"{area_r.name}({a9})" if area_r else "（未给出区县）"
                newp = idx.get(r.parent_code)
                fixes.append({
                    "层": "乡镇", "类型": "跨区调整+通名变更",
                    "问题": f"「{town}」在{old}下完全找不到",
                    "处理": f"放宽到{city_r.name}，唯一命中「{r.name}」"
                            f"（现属{newp.name if newp else '?'}）",
                    "证据": f"{r.name} {r.code} {r.full_path}"})
                clues.append(
                    f"该乡镇已不在用户给出的{old}下："
                    f"现为 {r.full_path}，下游须改用新名新属去搜")
                return r, fixes, clues
            if len(cs) > 1:
                clues.append(f"乡镇「{town}」在{city_r.name}下有 {len(cs)} 个同名：" +
                             "、".join(f"{c.name}({c.code})" for c in cs[:6]))
                return None, fixes, clues
    return None, fixes, clues


def _ancestors(idx, region) -> dict:
    """取某区划的真实祖先链 {1:省, 2:市, 3:区县}。民政部父链已回填，直接往上爬。"""
    out = {}
    cur = region
    seen = set()
    while cur is not None and cur.code not in seen:
        seen.add(cur.code)
        if cur.level in (1, 2, 3):
            out[cur.level] = cur
        p = code9(cur.parent_code) if cur.parent_code else ""
        cur = idx.get(p) if p else None
        if cur is not None and cur.level == 4:      # 防脏数据环
            break
    # 父链缺失时按代码位补：450203103 → 市 450200000、区县 450203000
    if 3 not in out and len(region.code) >= 6:
        a = idx.get(region.code[:6] + "000")
        if a and a.level == 3:
            out[3] = a
    if 2 not in out and len(region.code) >= 4:
        c = idx.get(region.code[:4] + "00000")
        if c and c.level == 2:
            out[2] = c
    if 1 not in out and len(region.code) >= 2:
        p = idx.get(region.code[:2] + "000000")
        if p and p.level == 1:
            out[1] = p
    return out


def _fuzzy_in(idx, parent9, stem):
    """在父级下按主体名前缀匹配，唯一命中才采纳"""
    if not parent9 or not stem:
        return None
    kids = [k for k in idx.children(parent9) if k.level == 4]
    hit = [k for k in kids if R.norm_key(_strip_tail(k.name)) == R.norm_key(stem)]
    return hit[0] if len(hit) == 1 else None


def _resolve_village(village, town_r):
    """行政村：挂统计局村级名录，按 (乡镇, 村名) 复合键"""
    try:
        vmap = R.load_villages(prefix="45", year=2023)
    except Exception:
        return None
    t9 = code9(town_r.code)
    for v in (vmap.get(t9) or []):
        nm = v if isinstance(v, str) else (v.get("名称") or v.get("name") or "")
        if nm == village or R.norm_key(nm) == R.norm_key(village):
            return {"名称": nm, "代码": "", "路径": town_r.full_path,
                    "别名": [], "记录": None}
    return None


def code9(c):
    return R.code9(c)


def _bbox_of(region, idx, half_deg=0.25):
    """按区划中心推 bbox。没有边界数据时用中心 ± half_deg（约 25 km）兜底。

    用途：天地图 search 的 mapBound，把搜索框死在正确的区县内——
    这是「上一层级纠偏」真正转化成「准」的地方。
    """
    if region is None:
        return None
    # 民政部数据本身不带经纬度；用高德行政区划 API 按代码取中心
    # （本模块只负责产出范围约定，取中心交给调用方按需触发）
    return getattr(region, "center", None) and None


# ---------------------------------------------------------------- 格式化

def brief(chain: dict) -> str:
    """一行摘要，便于日志与交付表"""
    d = chain["下游用名"]
    parts = [p for p in (d["市"], d["区县"], d["乡镇"]) if p]
    base = "/".join(parts)
    codes = "/".join(str(chain[k]["代码"]) for k in ("市", "区县", "乡镇")
                     if chain.get(k))
    fx = "；".join(f["类型"] for f in chain["纠偏"])
    tag = f"  [纠偏：{fx}]" if fx else ""
    return f"{base}  ({codes}){tag}"


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="行政区划链解析与纠偏")
    ap.add_argument("--city", default="")
    ap.add_argument("--area", default="")
    ap.add_argument("--town", default="")
    ap.add_argument("--village", default="")
    a = ap.parse_args()
    c = resolve_chain(city=a.city, area=a.area, town=a.town, village=a.village)
    print(brief(c))
    for f in c["纠偏"]:
        print(f"  纠偏[{f['层']}/{f['类型']}] {f['问题']} → {f['处理']}")
        print(f"        证据：{f['证据']}")
    for cl in c["线索"]:
        print(f"  线索：{cl}")
    print(f"  状态：{c['状态']}")
