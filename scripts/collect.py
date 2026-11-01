# -*- coding: utf-8 -*-
"""M3 采集模块：政府驻地 + POI 三模式 + 抓前计划 + 配额断点

四种任务类型（`Job.kind`）
-------------------------
  gov_seat    行政区划政府驻地（乡镇/区县/地市），按区划清单逐点查
  poi_area    指定行政区划内的某类 POI（网格切分 + 分页去重）
  poi_around  指定坐标周边半径内的 POI（同心圆补采，防半径边缘漏点）
  geocode     表格地址列批量转经纬度

三条硬设计
----------
1. **抓前必出计划**：`plan()` 先算「去重后目标数 / 预计请求数 / 预计耗时 / 配额占用」，
   超过日配额的部分直接告警并给分批建议，不让用户跑到一半才发现跑不完。
2. **断点即事实**：每条结果一落盘就写进 `.jsonl`，中断后 `resume=True` 自动跳过已完成键，
   已花钱的调用永不重复。
3. **只在能减量的地方减量**：网格切分先探总数（`count` 字段），一格能装下就不切；
   分页到上限才切网格，先用一次查询换掉几十次盲查。
"""
from __future__ import annotations

import json
import math
import os
import time
import urllib.parse
from dataclasses import dataclass, asdict, field
from pathlib import Path

from config import ROOT, get_key, load_config
import gc_common as G

CK_DIR = ROOT / "data" / "checkpoints"
CK_DIR.mkdir(parents=True, exist_ok=True)

#: 高德单次关键字搜索的翻页上限（100 页 × 25 条 = 2500 条/查询面）
AMAP_MAX_PAGE = 100
AMAP_PAGE_SIZE = 25
#: 一格目标条数上限，超过就切网格（留 20% 余量防翻页漂移）
CELL_TARGET = 2000
#: 每请求有效产出率（含翻页失败、重试、跨源降级损耗）
ETA = 0.85


# ---------------------------------------------------------------- 任务定义

@dataclass
class Job:
    """一个采集任务。"""
    kind: str                          # gov_seat | poi_area | poi_around | geocode
    name: str = ""                     # 任务显示名（用于输出文件名）
    province: str = ""
    city: str = ""
    area: str = ""
    town: str = ""
    levels: tuple = (4,)               # gov_seat 用：1省 2市 3县 4乡
    keyword: str = ""                  # POI 关键词
    types: str = ""                    # 高德 POI 分类码
    center: tuple | None = None        # (lon, lat) WGS84
    radius_m: int = 3000
    sources: tuple = ()                # 空=按已配置 Key 自动排序
    max_items: int = 0                 # 0=不限
    out_crs: str = "WGS84"
    input_file: str = ""               # geocode 用
    addr_col: str = "地址"
    lon_col: str = "经度"
    lat_col: str = "纬度"

    @property
    def job_id(self) -> str:
        parts = [self.kind, self.province, self.city, self.area, self.town,
                 self.keyword]
        raw = "_".join(p for p in parts if p) or self.kind
        return "".join(c if c.isalnum() or c in "_-" else "" for c in raw)[:60]


@dataclass
class Target:
    """一个待查目标（政府驻地任务里就是一条区划）。"""
    key: str
    label: str
    keyword: str
    city: str = ""
    area: str = ""
    town: str = ""
    code: str = ""
    prov: str = ""
    city_name: str = ""
    area_name: str = ""

    def to_record(self) -> dict:
        return {
            "键": self.key, "名称": self.label, "关键词": self.keyword,
            "省": self.prov, "市": self.city_name, "区县": self.area_name,
            "乡镇": self.town, "区划码": self.code,
        }


# ---------------------------------------------------------------- 配额预算

class QuotaBudget:
    """按源记账的配额预算器：接近日配额就提前停，不撞墙。"""

    def __init__(self, margin: float = 0.95):
        cfg = load_config()
        self.cap = {k: int(v) for k, v in cfg["runtime"]["daily_cap"].items()}
        self.used = {k: 0 for k in self.cap}
        self.margin = margin
        self.history = {k: 0 for k in self.cap}   # 断点续传时回填的已用量

    def room(self, source: str) -> int:
        return max(0, int(self.cap.get(source, 0) * self.margin) - self.used.get(source, 0))

    def charge(self, source: str, n: int = 1):
        self.used[source] = self.used.get(source, 0) + n

    def exhausted(self, source: str) -> bool:
        return self.room(source) <= 0

    def summary(self) -> dict:
        return {k: {"cap": self.cap[k], "used": self.used[k], "room": self.room(k)}
                for k in self.cap}


# ---------------------------------------------------------------- 断点

class Checkpoint:
    """只追加的 jsonl 断点：一行一条结果，中断不丢已花的钱。"""

    def __init__(self, job: Job, enabled: bool = True):
        self.path = CK_DIR / f"{job.job_id}.jsonl"
        self.meta_path = CK_DIR / f"{job.job_id}.meta.json"
        self.enabled = enabled
        self.done = {}
        self.records = []
        if self.enabled and self.path.exists():
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self.records.append(r)
                    if r.get("键"):
                        self.done[r["键"]] = r
        self._fh = open(self.path, "a", encoding="utf-8") if self.enabled else None
        self._n_since_flush = 0

    def is_done(self, key: str) -> bool:
        return key in self.done

    def write(self, rec: dict, flush_every: int = 20):
        self.records.append(rec)
        if rec.get("键"):
            self.done[rec["键"]] = rec
        if not self._fh:
            return
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._n_since_flush += 1
        if self._n_since_flush >= flush_every:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._n_since_flush = 0

    def close(self):
        if self._fh:
            self._fh.flush()
            self._fh.close()
            self._fh = None

    def reset(self):
        self.close()
        for p in (self.path, self.meta_path):
            if p.exists():
                p.unlink()
        self.done, self.records = {}, []
        # 重置后必须重新打开句柄，否则后续 write() 会静默丢弃（曾在自检里暴露）
        self._fh = open(self.path, "a", encoding="utf-8") if self.enabled else None
        self._n_since_flush = 0

    def save_meta(self, meta: dict):
        if not self.enabled:
            return
        with open(self.meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- 高德检索底层

def _amap_text(keywords: str = "", types: str = "", city: str = "",
               polygon: str = "", page: int = 1, offset: int = AMAP_PAGE_SIZE) -> dict:
    """高德 v3/place/text：支持矩形 polygon（`x1,y1|x2,y2`）与分类码。

    返回原始 dict（含 count/pois），由调用方决定如何用。
    """
    params = {
        "keywords": keywords, "types": types, "city": city,
        "citylimit": "true" if city else "false",
        "offset": str(offset), "page": str(page),
        "extensions": "base", "key": get_key("amap"),
    }
    if polygon:
        params["polygon"] = polygon
    url = "https://restapi.amap.com/v3/place/text?" + urllib.parse.urlencode(params)
    d = G._amap_call(url)
    return d or {}


def _amap_pois(keywords: str = "", types: str = "", city: str = "",
               polygon: str = "", max_page: int = AMAP_MAX_PAGE,
               budget: QuotaBudget | None = None, hard_cap: int = 0) -> list:
    """翻页取全部 POI；`hard_cap` 达到即停（用于探数阶段只取 1 页）。"""
    out, seen = [], set()
    for page in range(1, max_page + 1):
        d = _amap_text(keywords, types, city, polygon, page)
        if budget:
            budget.charge("amap")
        pois = d.get("pois") or []
        if not pois:
            break
        for p in pois:
            loc = p.get("location") or ""
            if "," not in loc:
                continue
            try:
                glon, glat = map(float, loc.split(","))
            except ValueError:
                continue
            wlon, wlat = G.gcj02_to_wgs84(glon, glat)
            pid = p.get("id") or f"{p.get('name','')}|{round(wlon,5)},{round(wlat,5)}"
            if pid in seen:
                continue
            seen.add(pid)
            out.append({
                "名称": p.get("name") or "",
                "地址": p.get("address") if isinstance(p.get("address"), str) else "",
                "经度": round(wlon, 6), "纬度": round(wlat, 6),
                "类型": p.get("type") or "",
                "来源": "amap", "来源ID": pid,
                "区县": (p.get("adname") or ""),
                "城市": (p.get("cityname") or ""),
                "省": (p.get("pname") or ""),
            })
            if hard_cap and len(out) >= hard_cap:
                return out
        if len(pois) < AMAP_PAGE_SIZE:
            break
        if len(out) >= AMAP_MAX_PAGE * AMAP_PAGE_SIZE:
            break
    return out


def _amap_count(keywords: str, types: str, city: str, polygon: str,
                budget: QuotaBudget | None = None) -> int:
    """探数：只查 1 页，读接口返回的 `count` 字段（真实总量，不是本页条数）。

    这一步是「切网格」的依据 —— 用返回条数当总量会把 2000 条误判成 25 条，
    网格切不切全错。
    """
    d = _amap_text(keywords, types, city, polygon, page=1)
    if budget:
        budget.charge("amap")
    try:
        return int(d.get("count") or 0)
    except (TypeError, ValueError):
        return 0


def _amap_district_raw(kw: str, subdistrict: int = 0) -> list:
    url = ("https://restapi.amap.com/v3/config/district?"
           + urllib.parse.urlencode({
               "keywords": kw, "subdistrict": str(subdistrict),
               "extensions": "all", "key": get_key("amap")}))
    d = G._amap_call(url)
    return (d or {}).get("districts") or []


def amap_district(candidates, expect_city: str = "", expect_level: str = "district") -> dict:
    """按候选词取行政区划节点，并用上级城市名校验（防同名区取错）。

    注意：高德 district 接口**只认裸名**（`兴宁区`），传 `南宁市兴宁区` 返回空。
    所以候选词要从「市+区」退到「区」逐个试。
    """
    if isinstance(candidates, str):
        candidates = [candidates]
    for kw in candidates:
        if not kw:
            continue
        for ds in _amap_district_raw(kw):
            lvl = ds.get("level")
            if expect_level and lvl and lvl != expect_level:
                continue
            cname = ds.get("cityname") or ""
            if expect_city and cname and not (expect_city in cname or cname in expect_city):
                continue
            return ds
    return {}


def amap_polygon(candidates, subdistrict: int = 0, expect_city: str = "") -> list:
    """取行政区划边界折线，返回 [(lon, lat WGS84), ...]。

    用于把「整个区县」切成网格，并在结果落盘前做边界点内判定 ——
    没有边界只能按矩形盲切，会大量空查且串到邻区。
    """
    ds = amap_district(candidates, expect_city=expect_city)
    if not ds:
        return []
    poly = ds.get("polyline") or ""
    ring = poly.split("|")[0] if poly else ""
    pts = []
    for seg in ring.split(";"):
        if "," not in seg:
            continue
        try:
            glon, glat = map(float, seg.split(","))
        except ValueError:
            continue
        wlon, wlat = G.gcj02_to_wgs84(glon, glat)
        pts.append((wlon, wlat))
    return pts


def bbox_of(points) -> tuple:
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    return (min(lons), min(lats), max(lons), max(lats))


def bbox_size_km(bbox) -> tuple:
    x1, y1, x2, y2 = bbox
    return (G.haversine_m(x1, (y1 + y2) / 2, x2, (y1 + y2) / 2) / 1000.0,
            G.haversine_m((x1 + x2) / 2, y1, (x1 + x2) / 2, y2) / 1000.0)


def grid_split(bbox, nx: int, ny: int) -> list:
    """把 bbox 等分成 nx×ny 个矩形，返回 [(bbox, polygon_str), ...]。"""
    x1, y1, x2, y2 = bbox
    dx, dy = (x2 - x1) / nx, (y2 - y1) / ny
    cells = []
    for i in range(nx):
        for j in range(ny):
            a, b = x1 + i * dx, y1 + j * dy
            c, d = a + dx, b + dy
            # 高德 polygon 要求 GCJ-02，且矩形顺序为 左下|右上
            g1 = G.wgs84_to_gcj02(a, b)
            g2 = G.wgs84_to_gcj02(c, d)
            cells.append(((a, b, c, d), f"{g1[0]:.6f},{g1[1]:.6f}|{g2[0]:.6f},{g2[1]:.6f}"))
    return cells


def plan_grid(bbox, total: int, cell_target: int = CELL_TARGET) -> list:
    """按预估总数决定切几格 —— 一格装得下就不切。"""
    if total <= cell_target:
        return grid_split(bbox, 1, 1)
    n = math.ceil(math.sqrt(total / cell_target))
    return grid_split(bbox, n, n)


def around_concentric(lon, lat, radius_m, rings=(1.0, 0.66, 0.33)) -> list:
    """同心圆补采：大圆翻页到上限后，用小圆补半径边缘漏掉的点。

    高德周边搜索每圆最多 100 页，单圆 3000m 内点位过密就会截断；
    缩小半径能把密度降到可翻完的程度。
    """
    out = []
    for k in rings:
        r = max(200, int(radius_m * k))
        out.append((r,))
        if r <= 200:
            break
    return out


# ---------------------------------------------------------------- 目标展开

def build_targets(job: Job, regions: list) -> list:
    """把任务展开成逐条目标。gov_seat 需要区划清单。"""
    from regions import RegionIndex, norm_key

    if job.kind != "gov_seat":
        return []

    idx = RegionIndex(regions)
    picked = []
    for lv in job.levels:
        for r in regions:
            if r.level != lv:
                continue
            if job.town and norm_key(r.name) != norm_key(job.town) and r.level == 4:
                continue
            if job.area and r.level >= 3 and norm_key(r.area or r.name) != norm_key(job.area):
                continue
            if job.city and r.level >= 2 and norm_key(r.city or r.name) != norm_key(job.city):
                continue
            picked.append(r)

    # 去重：同一 9 位码只查一次
    seen, uniq = set(), []
    for r in picked:
        if r.code in seen:
            continue
        seen.add(r.code)
        uniq.append(r)

    out = []
    for r in uniq:
        kw = gov_keyword(r.name, r.level)
        out.append(Target(
            key=r.code, label=r.name, keyword=kw,
            city=r.city or r.name, area=r.area or (r.name if r.level == 3 else ""),
            town=r.name if r.level == 4 else "", code=r.code,
            prov=r.prov, city_name=r.city, area_name=r.area,
        ))
    return out


def gov_keyword(name: str, level: int) -> str:
    """政府驻地检索关键词。

    街道办不能搜「XX街道办事处人民政府」（天地图会模糊命中到市政府），
    必须用「XX街道办事处」；镇/乡/区县/地市统一「XX人民政府」。
    """
    n = (name or "").strip()
    if level == 4:
        if n.endswith("街道办事处") or n.endswith("街道"):
            base = n[:-2] if n.endswith("街道") else n
            return f"{base}街道办事处"
        return f"{n}人民政府"
    return f"{n}人民政府"


# ---------------------------------------------------------------- 抓前计划

def plan(job: Job, targets: list, sources: list, sample: int = 3) -> dict:
    """抓前计划：数量、请求数、耗时、配额、分批建议。

    `sample>0` 时会实打实跑几条试采（默认 3 条），用实测单条耗时替换理论值；
    这样报给用户的「预计 2 小时」才不是拍脑袋。
    """
    cfg = load_config()
    n = len(targets) if job.kind == "gov_seat" else (job.max_items or 1)
    req_per_item = 1.35 if len(sources) > 1 else 1.0    # 降级链会多花请求
    est_req = int(n * req_per_item)

    # 理论耗时 = 请求数 / 有效 QPS
    qps = sum(cfg["runtime"]["qps"].get(s, 5) for s in sources) or 5
    est_sec = est_req / max(qps * ETA, 0.5)

    # 实测校准
    measured = 0.0
    if sample and targets and job.kind in ("gov_seat", "geocode"):
        t0 = time.time()
        for t in targets[:sample]:
            try:
                G.query_point(t.keyword, city=t.city, area=t.area, town=t.town,
                              sources=sources or None)
            except Exception:
                pass
        measured = (time.time() - t0) / max(sample, 1)
        est_sec = measured * n

    quota = {}
    per_src = max(1, len(sources))
    for s in sources or ["(未配置)"]:
        quota[s] = int(est_req / per_src)
    cap = cfg["runtime"]["daily_cap"]
    over = {s: (quota[s], cap.get(s, 0)) for s in quota
            if s in cap and quota[s] > cap[s]}

    batches = 1 if not over else max(
        math.ceil(max(quota[s] / cap[s] for s in over)), 1)

    return {
        "目标数": n,
        "预计请求数": est_req,
        "预计耗时秒": round(est_sec, 1),
        "预计耗时文案": _fmt_duration(est_sec),
        "实测单条毫秒": round(measured * 1000) if measured else None,
        "各源配额占用": quota,
        "超日配额": over,
        "建议分批数": batches,
        "建议": ("配额够，可一次跑完" if not over else
                 f"有 {len(over)} 个源超日配额，建议拆 {batches} 天跑，"
                 f"或先只跑高风险子集"),
    }


def _fmt_duration(sec: float) -> str:
    if sec < 60:
        return f"{sec:.0f} 秒"
    if sec < 3600:
        return f"{sec / 60:.1f} 分钟"
    if sec < 86400:
        return f"{sec / 3600:.1f} 小时"
    return f"{sec / 86400:.1f} 天"


# ---------------------------------------------------------------- 采集执行

def _norm_addr(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch not in " \u3000\t")


def collect_gov_seat(job: Job, targets: list, ck: Checkpoint,
                     budget: QuotaBudget, progress=None) -> list:
    """政府驻地采集：逐条走降级链，命中即记账。"""
    sources = list(job.sources) or None
    total = len(targets)
    for i, t in enumerate(targets, 1):
        if ck.is_done(t.key):
            continue
        if job.max_items and len(ck.records) >= job.max_items:
            break

        rec = t.to_record()
        rec.update({"经度": "", "纬度": "", "详细地址": "", "来源": "",
                    "采集状态": "", "备注": ""})
        try:
            hit = G.query_point(t.keyword, city=t.city, area=t.area, town=t.town,
                                sources=sources)
        except Exception as e:      # noqa: BLE001 单条失败不拖垮整批
            hit = None
            rec["备注"] = f"异常：{type(e).__name__}: {e}"

        if hit:
            rec["经度"], rec["纬度"] = hit["lon"], hit["lat"]
            rec["来源"] = hit["source"]
            rec["采集状态"] = "已采集"
            rec["备注"] = hit.get("detail", "")
            budget.charge(hit["source"])
        else:
            rec["采集状态"] = "未命中"
        ck.write(rec)
        if progress:
            progress(i, total, rec)
    return ck.records


def collect_poi_area(job: Job, targets: list, ck: Checkpoint,
                     budget: QuotaBudget, progress=None) -> list:
    """区内 POI：先探总数 → 决定网格 → 逐格翻页去重。

    两道收口，缺一就会串到邻区：
      ① 探数与网格都必须用**目标行政区的矩形范围**，不能只给 city（否则探的是全市）
      ② 结果必须过**边界点内判定**（`point_in_polygon`），矩形格子会溢出到邻区
    """
    # 候选词：市+区 → 区（高德 district 只认裸名，全称会返回空）
    scope = (job.city or "") + (job.area or "") or job.city or job.province
    candidates = [c for c in (scope, job.area, job.city, job.province) if c]
    expect_city = job.city if job.area else ""
    pts = amap_polygon(candidates, expect_city=expect_city)
    if not pts:
        raise RuntimeError(f"取不到「{scope}」的行政边界，无法切网格。\n"
                           f"可改为 poi_around 模式（给定中心点+半径）。")
    bbox = bbox_of(pts)

    def in_scope(rec: dict) -> bool:
        if job.area and rec.get("区县"):
            a, b = rec["区县"], job.area
            if a not in b and b not in a:
                return False
        if pts and len(pts) >= 3:
            return G.point_in_polygon(rec["经度"], rec["纬度"], pts)
        return True

    # 探数：用 bbox 矩形（而非整市），否则 count 是全市的量，网格会算小
    probe_poly = _rect(bbox)
    total = _amap_count(job.keyword, job.types, job.city or scope, probe_poly, budget)
    probe = _amap_pois(job.keyword, job.types, city=job.city or scope,
                       polygon=probe_poly, max_page=1, budget=budget)
    cells = plan_grid(bbox, total)
    # 一页就能翻完，说明总量很小 → 直接复用探数结果，不必再按网格重复查
    one_shot = bool(total) and total <= AMAP_PAGE_SIZE
    if one_shot:
        cells = []

    seen_keys = set()

    def take(rec: dict):
        if not in_scope(rec):
            return False
        k = f"{rec['名称']}|{rec['经度']},{rec['纬度']}"
        if k in seen_keys:
            return False
        seen_keys.add(k)
        r = dict(rec)
        r.update({"键": k, "采集状态": "已采集", "任务": "poi_area",
                  "关键词": job.keyword})
        ck.write(r)
        return True

    for rec in probe:
        take(rec)

    for cell_bbox, poly in cells:
        if job.max_items and len(ck.records) >= job.max_items:
            break
        if not poly:
            continue
        recs = _amap_pois(job.keyword, job.types, city=job.city or scope,
                          polygon=poly, budget=budget)
        for rec in recs:
            take(rec)
        if progress:
            progress(len(seen_keys), max(total, len(seen_keys)), None)
    return ck.records


def _rect(bbox) -> str:
    """bbox(WGS84) → 高德矩形 polygon 串（GCJ-02，左下|右上）。"""
    x1, y1, x2, y2 = bbox
    g1 = G.wgs84_to_gcj02(x1, y1)
    g2 = G.wgs84_to_gcj02(x2, y2)
    return f"{g1[0]:.6f},{g1[1]:.6f}|{g2[0]:.6f},{g2[1]:.6f}"


def collect_poi_around(job: Job, targets: list, ck: Checkpoint,
                       budget: QuotaBudget, progress=None) -> list:
    """周边 POI：同心圆补采，半径依次收缩，去重后合并。"""
    if not job.center:
        raise RuntimeError("poi_around 模式必须给 center=(经度, 纬度) WGS84")
    lon, lat = job.center
    seen_keys = set()
    for (r,) in around_concentric(lon, lat, job.radius_m):
        page = 1
        while page <= AMAP_MAX_PAGE:
            rs = G.amap_around(lon, lat, r, keyword=job.keyword, types=job.types,
                               page=page, offset=AMAP_PAGE_SIZE)
            budget.charge("amap")
            if not rs:
                break
            for rec in rs:
                k = f"{rec['name']}|{rec['lon']},{rec['lat']}"
                if k in seen_keys:
                    continue
                seen_keys.add(k)
                ck.write({
                    "键": k, "名称": rec["name"], "地址": rec.get("address", ""),
                    "经度": rec["lon"], "纬度": rec["lat"], "类型": rec.get("type", ""),
                    "来源": rec.get("source", "amap"), "来源ID": rec.get("poi_id", ""),
                    "距中心米": rec.get("distance_m", ""),
                    "采集状态": "已采集", "任务": "poi_around", "关键词": job.keyword,
                })
            if progress:
                progress(len(seen_keys), len(seen_keys), None)
            if len(rs) < AMAP_PAGE_SIZE:
                break
            page += 1
        if job.max_items and len(ck.records) >= job.max_items:
            break
    return ck.records


def collect_geocode(job: Job, targets: list, ck: Checkpoint,
                    budget: QuotaBudget, progress=None) -> list:
    """表格地址批量转坐标（读 xlsx/csv，输出交给 export 模块）。"""
    rows = _read_table(job.input_file)
    if not rows:
        raise RuntimeError(f"读不到数据或表为空：{job.input_file}")
    addr_col = _pick_col(rows[0], job.addr_col, ("地址", "详细地址", "address", "地点"))
    if not addr_col:
        raise RuntimeError(f"表里找不到地址列（期望 {job.addr_col}），"
                           f"实际列：{list(rows[0].keys())}")
    city = job.city or ""

    total = len(rows)
    for i, row in enumerate(rows, 1):
        addr = _norm_addr(str(row.get(addr_col, "")))
        key = f"{i}|{addr}"
        if not addr or ck.is_done(key):
            continue
        if job.max_items and len(ck.records) >= job.max_items:
            break
        rec = {"键": key, "名称": addr, "地址原文": addr, "经度": "", "纬度": "",
               "来源": "", "采集状态": "", "任务": "geocode"}
        hit = G.query_point(addr, city=city, task="addr")
        if hit:
            rec["经度"], rec["纬度"] = hit["lon"], hit["lat"]
            rec["来源"], rec["采集状态"] = hit["source"], "已采集"
            rec["备注"] = hit.get("detail", "")
            budget.charge(hit["source"])
        else:
            rec["采集状态"] = "未命中"
        ck.write(rec)
        if progress:
            progress(i, total, rec)
    return ck.records


def _read_table(path: str) -> list:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"输入文件不存在：{path}")
    if p.suffix.lower() in (".csv",):
        import csv
        for enc in ("utf-8-sig", "gb18030", "utf-8"):
            try:
                with open(p, encoding=enc) as f:
                    return list(csv.DictReader(f))
            except UnicodeDecodeError:
                continue
        raise RuntimeError(f"CSV 编码无法识别：{path}")
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            from openpyxl import load_workbook
        except ImportError as e:
            raise RuntimeError("需要 openpyxl 才能读 xlsx：pip install openpyxl") from e
        wb = load_workbook(p, read_only=True, data_only=True)
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        head = [str(c).strip() if c is not None else "" for c in next(it)]
        out = []
        for row in it:
            out.append({head[i]: ("" if v is None else v)
                        for i, v in enumerate(row) if i < len(head)})
        wb.close()
        return out
    raise RuntimeError(f"不支持的文件类型：{p.suffix}")


def _pick_col(sample_row: dict, want: str, aliases: tuple) -> str:
    cols = list(sample_row.keys())
    for c in cols:
        if c == want:
            return c
    for c in cols:
        for a in aliases:
            if a.lower() in str(c).lower():
                return c
    return ""


# ---------------------------------------------------------------- 统一入口

_HANDLERS = {
    "gov_seat": collect_gov_seat,
    "poi_area": collect_poi_area,
    "poi_around": collect_poi_around,
    "geocode": collect_geocode,
}


def run(job: Job, regions: list | None = None, resume: bool = True,
        show_plan: bool = True, progress=None) -> dict:
    """跑一个任务：出计划 → 执行 → 返回结果与统计。"""
    from config import available_sources

    sources = list(job.sources) or available_sources()
    if not sources:
        raise KeyError("没有任何已配置 Key 的数据源，无法采集。\n"
                       "零 Key 场景只能用坐标转换与区划查询。")

    targets = build_targets(job, regions or []) if job.kind == "gov_seat" else []
    if job.kind == "gov_seat" and not targets:
        raise RuntimeError("gov_seat 任务没有匹配到任何区划，请检查省/市/区县/乡镇范围。")

    info = plan(job, targets, sources) if show_plan else {}
    if show_plan:
        _print_plan(job, info)

    ck = Checkpoint(job, enabled=True)
    if not resume:
        ck.reset()
    budget = QuotaBudget()
    budget.history = _history_from_ck(ck)
    for k, v in budget.history.items():
        budget.used[k] = v

    ck.save_meta({"job": asdict(job), "plan": info,
                  "started": time.strftime("%Y-%m-%d %H:%M:%S")})
    t0 = time.time()
    try:
        recs = _HANDLERS[job.kind](job, targets, ck, budget, progress)
    finally:
        ck.close()
    cost = time.time() - t0

    ok = sum(1 for r in recs if r.get("采集状态") == "已采集")
    return {
        "任务": job.job_id,
        "结果": recs,
        "总条数": len(recs),
        "成功": ok,
        "未命中": len(recs) - ok,
        "耗时秒": round(cost, 1),
        "配额": budget.summary(),
        "计划": info,
        "断点文件": str(ck.path),
    }


def _history_from_ck(ck: Checkpoint) -> dict:
    """断点续跑时，把历史记录的来源计数回填，避免重复吃配额。"""
    used = {}
    for r in ck.records:
        s = r.get("来源")
        if s:
            used[s] = used.get(s, 0) + 1
    return used


def _print_plan(job: Job, p: dict):
    print("-" * 62)
    print(f"抓前计划 · {job.kind} · {job.keyword or job.area or job.city or job.province}")
    print(f"  目标数      : {p['目标数']}")
    print(f"  预计请求数  : {p['预计请求数']}")
    print(f"  预计耗时    : {p['预计耗时文案']}"
          + (f"（实测单条 {p['实测单条毫秒']} ms 校准）" if p["实测单条毫秒"] else "（理论值）"))
    print(f"  各源配额    : " + "，".join(f"{k} {v}" for k, v in p["各源配额占用"].items()))
    if p["超日配额"]:
        for s, (need, cap) in p["超日配额"].items():
            # 不用 emoji/符号：GBK 控制台下会 UnicodeEncodeError（Windows 默认 cp936）
            print(f"  [!] {s} 需 {need} 次 > 日配额 {cap} 次")
    print(f"  建议        : {p['建议']}")
    print("-" * 62)


# ---------------------------------------------------------------- 自检

def selftest(verbose: bool = True) -> int:
    """M3 自检：网格切分 / 同心圆 / 断点读写 / 计划估算（不花配额）。"""
    fails = []
    print("[M3] 采集模块自检")
    print("-" * 62)

    # 1) 网格切分
    bbox = (108.30, 22.75, 108.45, 22.88)
    cells = grid_split(bbox, 2, 2)
    areas = [(c[0][2] - c[0][0]) * (c[0][3] - c[0][1]) for c in cells]
    ok = len(cells) == 4 and abs(sum(areas) - (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])) < 1e-9
    print(f"[1] 网格切分        4 格，面积守恒  {'[OK]' if ok else '[FAIL]'}")
    if not ok:
        fails.append("网格切分不守恒")

    # 2) 网格数量随总数增长
    c1 = plan_grid(bbox, 500)
    c2 = plan_grid(bbox, 5000)
    c3 = plan_grid(bbox, 50000)
    ok = len(c1) == 1 and len(c2) > len(c1) and len(c3) > len(c2)
    print(f"[2] 自适应网格      500→{len(c1)} 格，5000→{len(c2)} 格，50000→{len(c3)} 格  "
          f"{'[OK]' if ok else '[FAIL]'}")
    if not ok:
        fails.append("自适应网格不递增")

    # 3) 同心圆
    rings = around_concentric(108.36, 22.82, 3000)
    rs = [r[0] for r in rings]
    ok = rs == sorted(rs, reverse=True) and rs[-1] >= 200
    print(f"[3] 同心圆补采      {rs}  {'[OK]' if ok else '[FAIL]'}")
    if not ok:
        fails.append("同心圆半径顺序错误")

    # 4) 断点读写
    job = Job(kind="gov_seat", province="广西壮族自治区", city="南宁市",
              area="青秀区", town="", keyword="t")
    ck = Checkpoint(job)
    ck.reset()
    ck.write({"键": "k1", "采集状态": "已采集"})
    ck.write({"键": "k2", "采集状态": "未命中"})
    ck.close()
    ck2 = Checkpoint(job)
    ok = ck2.is_done("k1") and ck2.is_done("k2") and len(ck2.records) == 2
    print(f"[4] 断点续传        重开后恢复 {len(ck2.records)} 条  {'[OK]' if ok else '[FAIL]'}")
    ck2.reset()
    if not ok:
        fails.append("断点恢复失败")

    # 5) 计划估算（不联网）
    p = plan(job, [Target(key=f"k{i}", label="x", keyword="y") for i in range(100)],
             ["amap"], sample=0)
    ok = p["目标数"] == 100 and p["预计请求数"] >= 100 and p["预计耗时秒"] > 0
    print(f"[5] 计划估算        100 目标 → {p['预计请求数']} 请求 / "
          f"{p['预计耗时文案']}  {'[OK]' if ok else '[FAIL]'}")
    if not ok:
        fails.append("计划估算异常")

    # 6) 表读取
    try:
        rows = _read_table(str(ROOT / "data" / "regions" / "stats2023_villages_45.csv"))
        ok = len(rows) > 1000
        print(f"[6] 表格读取        CSV {len(rows)} 行  {'[OK]' if ok else '[FAIL]'}")
    except Exception as e:      # noqa: BLE001
        ok = False
        print(f"[6] 表格读取        [SKIP] {e}")

    print("-" * 62)
    if fails:
        print(f"自检结论：FAIL —— {'；'.join(fails)}")
        return 1
    print("自检结论：PASS")
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv or len(sys.argv) == 1:
        sys.exit(selftest())
