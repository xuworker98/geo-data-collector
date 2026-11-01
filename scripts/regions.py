# -*- coding: utf-8 -*-
"""M2 区划模块：五级行政区划标准名称与代码

职责
----
1. 从民政部「国家地名信息库」抓取 省/市/区县/乡镇 四级标准名称与代码（9 位码）
2. 从国家统计局「统计用区划代码」加载 村级（村/社区）名称与代码（12 位码）
3. 本地静态缓存：一次下载永久缓存，离线可复用（`data/regions/`）
4. 双套代码错位处理：**村级归属以统计局 2023 为准**，乡镇名称对齐民政部
5. 名称归一与同名消歧：`广西` → `广西壮族自治区`，靠（区县,乡镇）复合键防同名错配

两个必须记住的坑
----------------
* 民政部接口**必须带 Chrome UA**（与天地图正相反：天地图服务端 Key 带 Mozilla 会 301013）
* 民政部 9 位码右侧补零成 12 位（`ljust(9,'0')`），不是 `zfill` —— 补错位会查不到子级

命令行
------
    python regions.py --import-mca  <mca_gx_4level.json>      离线导入民政部缓存
    python regions.py --import-villages <villages_gx_2023.csv> 离线导入统计局村级缓存
    python regions.py --province 广西 --check                  联网抓取 + 自检
    python regions.py --selftest                               纯离线自检（需已缓存）
"""
from __future__ import annotations

import csv
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict, field
from pathlib import Path

from config import ROOT, http_opener

# ---------------------------------------------------------------- 常量

DATA_DIR = ROOT / "data" / "regions"
DATA_DIR.mkdir(parents=True, exist_ok=True)

MCA_API = "https://dmfw.mca.gov.cn/xzqh/getCodeList"
MCA_TABLE = os.environ.get("GDC_MCA_TABLE", "Xzqh20251231")

STATS_BASE = "https://www.stats.gov.cn/sj/tjbz/tjyqhdmhcxhfdm/{year}/"

#: 民政部接口必须带完整浏览器 UA，否则返回空数据
UA_CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

LEVEL_LABEL = {1: "省级", 2: "地市级", 3: "区县级", 4: "乡镇级", 5: "村/社区级"}

#: 名称后缀，按长度降序剥离（长后缀优先，"自治县" 不能被 "县" 先切掉）
_SUFFIXES = [
    "特别行政区", "自治区", "自治州", "自治县", "自治旗",
    "地区", "盟", "省", "市", "县", "区", "旗",
    "街道办事处", "街道", "镇", "乡", "苏木",
    "村民委员会", "居民委员会", "村委会", "居委会",
    "社区", "村",
]

_SPLIT_RE = re.compile(
    r"^(?P<prov>[^省市自治区]{2,10}?(?:省|自治区|特别行政区))?"
    r"(?P<city>[^市县区州盟]{1,10}?(?:市|自治州|地区|盟))?"
    r"(?P<area>[^县区市旗]{1,10}?(?:县|区|旗|市))?"
    r"(?P<rest>.*)$"
)


# ---------------------------------------------------------------- 基础工具

def code9(c) -> str:
    """截取前 9 位有效区划码。"""
    return re.sub(r"\D", "", str(c or ""))[:9]


def pad12(c) -> str:
    """9 位码 → 12 位父码形态（右侧补零，不是左侧！）。"""
    s = code9(c)
    return s.ljust(9, "0") + "000" if len(s) == 9 else str(c)


def short_name(name: str) -> str:
    """剥掉行政级别后缀，得到可用于比对的短名。"""
    s = (name or "").strip()
    if not s:
        return ""
    for suf in _SUFFIXES:
        if len(s) > len(suf) and s.endswith(suf):
            return s[: -len(suf)]
    return s


def norm_key(name: str) -> str:
    """归一化比对键：剥后缀 + 去空白与间隔符。"""
    s = short_name(name)
    return re.sub(r"[\s\u3000·・\-—_]", "", s)


def split_address(text: str) -> dict:
    """从一整条地址里粗切 省/市/区县/乡镇。

    纯规则切分，不做联网校验；切不出的层级留空，由调用方兜底。
    """
    t = re.sub(r"[\s\u3000]+", "", text or "")
    out = {"prov": "", "city": "", "area": "", "town": "", "rest": ""}
    m = _SPLIT_RE.match(t)
    if not m:
        out["rest"] = t
        return out
    for k in ("prov", "city", "area"):
        out[k] = (m.group(k) or "").strip()
    rest = (m.group("rest") or "").strip()
    # 乡镇可能出现在剩余串里（街道/镇/乡），取第一个匹配
    mt = re.search(r"^([^镇乡街道]{1,10}?(?:街道办事处|街道|镇|乡))", rest)
    if mt:
        out["town"] = mt.group(1)
        rest = rest[len(out["town"]):]
    out["rest"] = rest
    return out


# ---------------------------------------------------------------- Region

@dataclass
class Region:
    """一条区划记录（统一形态，五级通用）。"""
    code: str
    name: str
    level: int
    parent_code: str = ""
    path: str = ""
    prov_code: str = ""
    prov: str = ""
    city_code: str = ""
    city: str = ""
    area_code: str = ""
    area: str = ""
    town_code: str = ""
    town: str = ""
    stats_town: str = ""         # 统计局口径的乡镇名（村级记录专用，用于名称对齐）
    source: str = "mca"          # mca=民政部 | stats=国家统计局

    @property
    def level_label(self) -> str:
        return LEVEL_LABEL.get(self.level, "")

    @property
    def full_path(self) -> str:
        return self.path or self.name

    def to_row(self) -> dict:
        d = asdict(self)
        d["级别"] = self.level_label
        return d


def _fill_ancestors(reg: Region, idx: dict) -> Region:
    """按父链回填各级名称与代码，并生成 path。"""
    chain = []
    cur = reg
    guard = 0
    while cur is not None and guard < 8:
        chain.append(cur)
        pc = code9(cur.parent_code)
        cur = idx.get(pc) if pc and pc != "000000000" else None
        guard += 1
    chain.reverse()

    names, codes = [], []
    for r in chain:
        names.append(r.name)
        codes.append(r.code)
    reg.path = "/".join(["全国"] + names[1:] if names and names[0] in ("中华人民共和国", "全国") else names)

    for lv, r in zip([x.level for x in chain], chain):
        if lv == 1:
            reg.prov_code, reg.prov = r.code, r.name
        elif lv == 2:
            reg.city_code, reg.city = r.code, r.name
        elif lv == 3:
            reg.area_code, reg.area = r.code, r.name
        elif lv == 4:
            reg.town_code, reg.town = r.code, r.name

    # 自身即某级时，本级字段也要落到对应槽位
    if reg.level == 1:
        reg.prov_code, reg.prov = reg.code, reg.name
    elif reg.level == 2:
        reg.city_code, reg.city = reg.code, reg.name
    elif reg.level == 3:
        reg.area_code, reg.area = reg.code, reg.name
    elif reg.level == 4:
        reg.town_code, reg.town = reg.code, reg.name
    elif reg.level == 5:
        # 村级：父是乡镇
        pc = idx.get(code9(reg.parent_code))
        if pc is not None:
            reg.town_code, reg.town = pc.code, pc.name
    return reg


# ---------------------------------------------------------------- 民政部抓取

def _mca_post(params: dict, timeout: int = 30, retries: int = 3) -> dict:
    """POST 民政部接口。注意：必须带 Chrome UA。"""
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        MCA_API, data=body,
        headers={
            "User-Agent": UA_CHROME,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://dmfw.mca.gov.cn/xzqh/xzqh.html",
        },
    )
    ctx = None  # 证书与代理交由 config.http_opener() 统一处理
    last = None
    for i in range(retries):
        try:
            raw = http_opener().open(req, timeout=timeout).read()
            return json.loads(raw.decode("utf-8", "ignore"))
        except Exception as e:      # noqa: BLE001 网络层一律退避重试
            last = e
            if i < retries - 1:
                time.sleep(2 * (i + 1))
    raise RuntimeError(f"民政部接口请求失败：{last}\n"
                       f"请检查网络；若在受限环境，改用 --import-mca 离线导入。")


def _mca_paged(parent_code: str, table: str = MCA_TABLE,
               page_size: int = 500, type_val: str = "2") -> list:
    """分页拉取某 parentCode 下全部子级。"""
    out, page = [], 1
    while True:
        r = _mca_post({
            "parentCode": parent_code,
            "placeCode": "",
            "title": "",
            "tableName": table,
            "type": type_val,
            "pageNum": str(page),
            "pageSize": str(page_size),
        })
        data = r.get("data") or []
        out.extend(data)
        if len(data) < page_size:
            break
        page += 1
        if page > 200:      # 安全阀，防接口异常导致死循环
            break
        time.sleep(0.3)
    return out


def _pick_children(items: list, parent: str, expect_level: int) -> list:
    """从原始返回中筛出真正的直接子级（兼容接口把自身/祖辈一并返回的情况）。"""
    p9 = code9(parent)
    p12 = pad12(parent)
    keep = []
    for it in items:
        c = code9(it.get("code"))
        if not c or c == p9:
            continue
        lv = it.get("level")
        lv = int(lv) if str(lv).isdigit() else None
        pc = str(it.get("parentCode") or "")
        if pc.startswith(p9):
            keep.append(it)
        elif expect_level and lv == expect_level and len(c) >= 6:
            # 接口未按 parentCode 精确过滤时，退回凭级别 + 代码前缀判断
            if c[: len(p9[:6])] == p9[:6] or c[:2] == p9[:2]:
                keep.append(it)
    # 同一码去重
    seen, uniq = set(), []
    for it in keep:
        c = code9(it.get("code"))
        if c in seen:
            continue
        seen.add(c)
        uniq.append(it)
    return uniq


def fetch_provinces(table: str = MCA_TABLE) -> list:
    """取全国省级节点（34 个）。"""
    for parent in ("000000000000", "000000000", "0", ""):
        items = _mca_paged(parent, table=table)
        lv1 = [it for it in items if str(it.get("level")) == "1"]
        if lv1:
            return lv1
    return []


def fetch_children(code: str, expect_level: int, table: str = MCA_TABLE,
                   verbose: bool = False) -> list:
    """取某节点下的子级。

    strategy 依次尝试 12 位父码 / 9 位码 / 上级前缀（老接口只认 2 位前缀），
    哪一档能筛出子级就用哪一档，全程不需要人判断。
    """
    c9 = code9(code)
    tries = [pad12(c9), c9]
    if expect_level == 2:
        tries.append(c9[:2])
    elif expect_level == 3:
        tries.append(c9[:4])
    for parent in tries:
        try:
            items = _mca_paged(parent, table=table)
        except RuntimeError:
            raise
        kids = _pick_children(items, c9, expect_level)
        kids = [k for k in kids if _level_of(k) == expect_level] or kids
        if kids:
            if verbose:
                print(f"    {c9} 子级 {len(kids)} 条（parentCode={parent!r}）")
            return kids
    return []


def _level_of(it: dict):
    lv = it.get("level")
    return int(lv) if str(lv).isdigit() else None


def crawl_mca(root_code: str = "450000000", levels=(1, 2, 3, 4),
              table: str = MCA_TABLE, verbose: bool = True,
              progress=None) -> list:
    """抓取指定省域的四级区划，返回 Region 列表（含根节点）。"""
    root_code = code9(root_code)

    prov = None
    for p in fetch_provinces(table=table):
        if code9(p.get("code")) == root_code:
            prov = p
            break
    if prov is None:
        raise RuntimeError(
            f"未在民政部接口找到省级节点 {root_code}。\n"
            f"可先用 --import-mca 离线导入已缓存的标准名称文件。"
        )

    rows = [Region(code=code9(prov["code"]), name=prov["name"], level=1,
                   parent_code=code9(prov.get("parentCode")), source="mca")]
    if verbose:
        print(f"  [1/4] {prov['name']} ({root_code})")

    cur_level = [rows[0]]
    for lv in (2, 3, 4):
        if lv not in levels:
            continue
        nxt = []
        for node in cur_level:
            time.sleep(0.15)
            kids = fetch_children(node.code, lv, table=table)
            for k in kids:
                r = Region(code=code9(k.get("code")), name=k["name"], level=lv,
                           parent_code=code9(k.get("parentCode")), source="mca")
                nxt.append(r)
                rows.append(r)
            if progress:
                progress(lv, node, len(kids))
        if verbose:
            print(f"  [{lv}/4] {LEVEL_LABEL.get(lv,'')} {len(nxt)} 条")
        cur_level = nxt

    idx = {r.code: r for r in rows}
    # 父码若为空，按代码结构推断（9 位码：省2 市2 县2 乡3）
    for r in rows:
        if not r.parent_code:
            r.parent_code = _infer_parent(r.code, idx)
        _fill_ancestors(r, idx)
    return rows


def _infer_parent(c: str, idx: dict) -> str:
    c = code9(c)
    for cut in (6, 4, 2):
        if cut >= len(c):
            continue
        cand = (c[:cut]).ljust(9, "0")
        if cand in idx:
            return cand
    return ""


# ---------------------------------------------------------------- 统计局村级

def _stats_get(url: str, timeout: int = 30, retries: int = 3) -> str:
    """取统计局页面 HTML（编码探测：utf-8 → gb18030）。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA_CHROME})
    last = None
    for i in range(retries):
        try:
            raw = http_opener().open(req, timeout=timeout).read()
            for enc in ("utf-8", "gb18030"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", "ignore")
        except Exception as e:      # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(2 * (i + 1))
    raise RuntimeError(f"统计局页面抓取失败：{url} —— {last}")


def crawl_stats_villages(prefix: str = "45", year: int = 2023,
                         verbose: bool = True) -> tuple:
    """抓取统计局五级区划到村级（较慢，一次抓取后永久缓存）。

    页面链：`{year}/{省2}.html` → 市6 → 县6 → 乡9 → 村12。
    返回 (villages, streets)：streets 为 {9位街镇码: (乡镇名, 区县码)}。
    """
    base = STATS_BASE.format(year=year)
    rows, streets = [], {}

    def pairs(html, pat):
        """从页面里抽出 (代码, 名称) 对，保持出现顺序并去重。"""
        out, seen = [], set()
        for m in re.finditer(pat, html, re.S):
            c = m.group(1)
            nm = re.sub(r"<[^>]+>|&nbsp;|\s", "", m.group(2) if m.lastindex >= 2 else "")
            if c in seen:
                continue
            seen.add(c)
            out.append((c, nm))
        return out

    prov_html = _stats_get(f"{base}{prefix}.html")
    cities = pairs(prov_html, rf'href="(\d{{6}})\.html"[^>]*>([^<]*)<')
    if verbose:
        print(f"  统计局：省级页解析出 {len(cities)} 个地市页")

    for c6, _cname in cities:
        time.sleep(0.3)
        try:
            city_html = _stats_get(f"{base}{prefix}/{c6}.html")
        except RuntimeError:
            continue
        for a6, _aname in pairs(city_html, rf'href="(\d{{6}})\.html"[^>]*>([^<]*)<'):
            time.sleep(0.3)
            try:
                area_html = _stats_get(f"{base}{prefix}/{c6}/{a6}.html")
            except RuntimeError:
                continue
            for t9, tname in pairs(area_html, r'href="(\d{9})\.html"[^>]*>([^<]*)<'):
                streets.setdefault(t9, (tname, a6))
                time.sleep(0.3)
                try:
                    town_html = _stats_get(f"{base}{prefix}/{c6}/{a6}/{t9}.html")
                except RuntimeError:
                    continue
                for m in re.finditer(
                        r'<tr[^>]*class=["\']?villagetr["\']?[^>]*>(.*?)</tr>',
                        town_html, re.S):
                    tds = re.findall(r"<td[^>]*>(.*?)</td>", m.group(1), re.S)
                    if len(tds) < 2:
                        continue
                    code = re.sub(r"<[^>]+>|\s", "", tds[0])
                    name = re.sub(r"<[^>]+>|\s", "", tds[1])
                    if len(code) != 12:
                        continue
                    rows.append({
                        "code": code, "name": name, "streetCode": code[:9],
                        "provinceCode": code[:2], "cityCode": code[:4],
                        "areaCode": code[:6],
                    })
            if verbose:
                print(f"    {a6} 累计村级 {len(rows)} 条")

    # 顺手把乡镇名落盘，供名称对齐使用
    sp = _streets_cache_path(prefix, year)
    with open(sp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["code", "name", "areaCode"])
        w.writeheader()
        for c, (n, a) in streets.items():
            w.writerow({"code": c, "name": n, "areaCode": a})
    return rows, streets


# ---------------------------------------------------------------- 缓存读写

def _mca_cache_path(root_code: str, table: str = MCA_TABLE) -> Path:
    return DATA_DIR / f"mca_{code9(root_code)}_{table}.json"


def save_mca_cache(rows: list, root_code: str, table: str = MCA_TABLE) -> Path:
    p = _mca_cache_path(root_code, table)
    payload = {
        "meta": {
            "source": "民政部 国家地名信息库",
            "api": MCA_API,
            "table": table,
            "root_code": code9(root_code),
            "count": len(rows),
            "by_level": _count_by_level(rows),
            "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "rows": [asdict(r) for r in rows],
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return p


def load_mca_cache(root_code: str, table: str = MCA_TABLE):
    p = _mca_cache_path(root_code, table)
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        payload = json.load(f)
    rows = [Region(**r) for r in payload.get("rows", [])]
    return rows, payload.get("meta", {})


def import_mca_json(src: str | Path, root_code: str = "450000000",
                    table: str = MCA_TABLE) -> Path:
    """把「平铺 JSON 列表」（历史项目格式）导入为模块缓存格式。"""
    with open(src, encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, dict) and "rows" in raw:
        raw = raw["rows"]
    rows = []
    for it in raw:
        lv = it.get("level") or it.get("_level")
        if not str(lv).isdigit():
            continue
        rows.append(Region(
            code=code9(it.get("code")), name=it.get("name", ""),
            level=int(lv), parent_code=code9(it.get("parentCode")),
            source="mca",
        ))
    idx = {r.code: r for r in rows}
    for r in rows:
        if not r.parent_code:
            r.parent_code = _infer_parent(r.code, idx)
        _fill_ancestors(r, idx)
    return save_mca_cache(rows, root_code, table)


def _villages_cache_path(prefix: str, year: int = 2023) -> Path:
    return DATA_DIR / f"stats{year}_villages_{prefix}.csv"


def _streets_cache_path(prefix: str, year: int = 2023) -> Path:
    return DATA_DIR / f"stats{year}_streets_{prefix}.csv"


def import_streets_csv(src: str | Path, prefix: str = "45",
                       year: int = 2023) -> Path:
    """导入统计局乡镇级（街道/镇/乡）CSV。

    村级 CSV 只有 9 位街镇码、没有乡镇名；对齐民政部乡镇必须靠名称，
    所以乡镇名要单独缓存一份（列：code,name,areaCode）。
    """
    with open(src, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if rows and not {"code", "name"}.issubset(rows[0].keys()):
        raise RuntimeError(f"乡镇 CSV 列名不符，需含 code/name，实际 {list(rows[0].keys())}")
    kept = [r for r in rows if str(r["code"]).startswith(prefix)]
    p = _streets_cache_path(prefix, year)
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["code", "name", "areaCode"])
        w.writeheader()
        for r in kept:
            w.writerow({"code": r["code"], "name": r["name"],
                        "areaCode": r.get("areaCode", str(r["code"])[:6])})
    return p


def load_stats_streets(prefix: str = "45", year: int = 2023) -> dict:
    """加载统计局乡镇名映射 {9位街镇码: 乡镇名}；无缓存返回空字典。"""
    p = _streets_cache_path(prefix, year)
    if not p.exists():
        return {}
    with open(p, encoding="utf-8-sig") as f:
        return {r["code"]: r["name"] for r in csv.DictReader(f) if r.get("code")}


def import_villages_csv(src: str | Path, prefix: str = "45",
                        year: int = 2023) -> Path:
    """导入统计局村级 CSV（按省前缀过滤后缓存）。"""
    with open(src, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    need = {"code", "name", "streetCode"}
    if rows and not need.issubset(rows[0].keys()):
        raise RuntimeError(
            f"村级 CSV 列名不符，需包含 {sorted(need)}，实际 {list(rows[0].keys())}"
        )
    kept = [r for r in rows if str(r["code"]).startswith(prefix)]
    p = _villages_cache_path(prefix, year)
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["code", "name", "streetCode",
                                          "provinceCode", "cityCode", "areaCode"])
        w.writeheader()
        for r in kept:
            w.writerow({k: r.get(k, "") for k in w.fieldnames})
    return p


def load_villages(prefix: str = "45", year: int = 2023,
                  auto_crawl: bool = False, verbose: bool = True) -> list:
    """加载村级区划。优先本地缓存；无缓存且 auto_crawl=True 时联网抓取。"""
    p = _villages_cache_path(prefix, year)
    if not p.exists():
        if not auto_crawl:
            raise FileNotFoundError(
                f"缺少村级缓存：{p}\n"
                f"解决方式（任选）：\n"
                f"  ① 联网抓取：load_villages('{prefix}', auto_crawl=True)\n"
                f"  ② 离线导入：python regions.py --import-villages <villages.csv>\n"
                f"  ③ 只跑到乡镇级，村级留空由采集阶段补。"
            )
        rows, _streets = crawl_stats_villages(prefix, year, verbose=verbose)
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["code", "name", "streetCode",
                                              "provinceCode", "cityCode", "areaCode"])
            w.writeheader()
            w.writerows(rows)

    with open(p, encoding="utf-8-sig") as f:
        raw = list(csv.DictReader(f))
    streets = load_stats_streets(prefix, year)
    out = []
    for r in raw:
        out.append(Region(code=r["code"], name=r["name"], level=5,
                          parent_code=r["streetCode"], source="stats",
                          stats_town=streets.get(r["streetCode"], "")))
    return out


# ---------------------------------------------------------------- 索引与消歧

class RegionIndex:
    """区划索引：按码查、按名查、同名消歧、复合键匹配。"""

    def __init__(self, regions: list):
        self.regions = regions
        self.by_code = {r.code: r for r in regions}
        self.by_pkey = {}          # (level, norm_key) -> [Region]
        self.children_map = {}
        for r in regions:
            self.by_pkey.setdefault((r.level, norm_key(r.name)), []).append(r)
            self.children_map.setdefault(code9(r.parent_code), []).append(r)

    # -- 基础查询
    def get(self, code) -> Region | None:
        return self.by_code.get(code9(code))

    def children(self, code) -> list:
        return self.children_map.get(code9(code), [])

    def of_level(self, level: int) -> list:
        return [r for r in self.regions if r.level == level]

    # -- 名称解析
    def find(self, text: str, level: int | None = None,
             parent_code: str | None = None) -> list:
        """按名称找候选。返回列表（可能多个，同名不合并）。"""
        if not text:
            return []
        t = str(text).strip()
        c9 = code9(t)
        if len(c9) == 9 and c9 in self.by_code:
            return [self.by_code[c9]]

        k = norm_key(t)
        cands = []
        for (lv, nk), group in self.by_pkey.items():
            if level and lv != level:
                continue
            if nk == k or (len(k) >= 2 and nk.startswith(k)) or (len(nk) >= 2 and k.startswith(nk)):
                cands.extend(group)
        if parent_code:
            p9 = code9(parent_code)
            cands = [r for r in cands if code9(r.parent_code) == p9
                     or r.area_code == p9 or r.city_code == p9 or r.prov_code == p9]
        # 全名精确命中的排前面
        cands.sort(key=lambda r: (r.name != t, len(r.name)))
        return cands

    def resolve_one(self, text: str, level: int | None = None,
                    parent_code: str | None = None) -> Region:
        """要求唯一命中；多个候选时抛错并列出，绝不自己猜。"""
        cands = self.find(text, level=level, parent_code=parent_code)
        if not cands:
            raise KeyError(f"未找到区划：{text!r}（级别={LEVEL_LABEL.get(level, '不限')}）")
        if len(cands) > 1:
            head = "、".join(f"{c.name}({c.code}, {c.level_label})" for c in cands[:6])
            raise KeyError(
                f"区划名称 {text!r} 命中 {len(cands)} 个候选，无法唯一确定：{head}\n"
                f"请补全上级（如「南宁市青秀区」）或直接给 9 位区划代码。"
            )
        return cands[0]

    # -- 复合键匹配（防同名错配）
    def match_child_by_name(self, parent_code: str, name: str,
                            level: int | None = None) -> Region | None:
        """在指定父级下按名称找子级 —— 复合键 (父码, 名称)，同名跨区不会错配。"""
        kids = self.children(parent_code)
        if level:
            kids = [k for k in kids if k.level == level]
        k = norm_key(name)
        if not k:
            return None
        exact = [x for x in kids if x.name == name]
        if len(exact) == 1:
            return exact[0]
        loose = [x for x in kids if norm_key(x.name) == k]
        if len(loose) == 1:
            return loose[0]
        prefix = [x for x in kids if norm_key(x.name).startswith(k) or k.startswith(norm_key(x.name))]
        if len(prefix) == 1:
            return prefix[0]
        return None


# ---------------------------------------------------------------- 双套代码错位

def consistency_check(mca_rows: list, village_rows: list,
                      verbose: bool = True) -> dict:
    """校验民政部（省/市/县/乡）与统计局（乡/村）两套代码的一致性。

    已知问题：两套代码的乡镇调整节奏不同，直接按 9 位码把村挂到乡镇会出现
    **系统性错位**（广西实测 127 个乡镇）。本函数把错位量出来，并给出
    「按名称 + 区县复合键对齐」的可行率，供后续模块选择对齐策略。
    """
    mca_town = {r.code: r for r in mca_rows if r.level == 4}
    mca_area = {r.code: r for r in mca_rows if r.level == 3}
    idx = RegionIndex(mca_rows)

    by_street = {}
    for v in village_rows:
        by_street.setdefault(v.parent_code, []).append(v)

    exact = name_ok = unmatched = 0
    examples, orphans = [], []
    for st_code, vs in by_street.items():
        if st_code in mca_town:
            exact += 1
            continue
        area_code = st_code[:6] + "000"
        sname = vs[0].stats_town if vs else ""
        hit = None
        if sname and area_code in mca_area:
            hit = idx.match_child_by_name(area_code, sname, level=4)
        if hit is not None:
            name_ok += 1
            if len(examples) < 8:
                examples.append({
                    "stats_street": st_code, "stats_name": sname or "(缺乡镇名)",
                    "mca_town_code": hit.code, "mca_town_name": hit.name,
                    "area": mca_area[area_code].name,
                })
        else:
            unmatched += 1
            orphans.append({"stats_street": st_code, "stats_name": sname,
                            "area": mca_area[area_code].name if area_code in mca_area else area_code,
                            "villages": len(vs)})

    res = {
        "mca_town_total": len(mca_town),
        "stats_street_total": len(by_street),
        "code_exact_match": exact,
        "code_mismatch_name_recoverable": name_ok,
        "both_fail": unmatched,
        "village_total": len(village_rows),
        "examples": examples,
        "orphan_streets": sorted(orphans, key=lambda x: -x["villages"])[:20],
    }
    if verbose:
        print(f"    民政部乡镇 {res['mca_town_total']} / 统计局街镇 {res['stats_street_total']}")
        print(f"    9 位码直接命中 {exact} 个，按名称可救回 {name_ok} 个，"
              f"两法均无解 {unmatched} 个")
        for e in examples[:5]:
            print(f"      {e['area']} {e['stats_name']}({e['stats_street']}) "
                  f"→ {e['mca_town_name']}({e['mca_town_code']})")
    return res


def attach_villages(mca_rows: list, village_rows: list,
                    verbose: bool = True) -> tuple:
    """把村级挂到乡镇下，返回 (region 列表, 统计)。

    对齐策略（三级，绝不硬塞）：
      L1 9 位乡镇码直接命中民政部乡镇 → 直接用
      L2 码不中 → 用（区县码, 统计局乡镇名）复合键在民政部乡镇里找
      L3 仍不中 → 归入 orphan，保留区县归属但不编造乡镇
    """
    idx = RegionIndex(mca_rows)
    areas = {r.code: r for r in mca_rows if r.level == 3}
    out, orphan = [], []
    stat = {"L1_code": 0, "L2_name": 0, "L3_orphan": 0}

    for v in village_rows:
        st = v.parent_code
        tgt = idx.get(st)
        if tgt is not None and tgt.level != 4:
            tgt = None
        if tgt is not None:
            stat["L1_code"] += 1
        else:
            area_code = st[:6] + "000"
            tgt = idx.match_child_by_name(area_code, v.stats_town, level=4) if v.stats_town else None
            if tgt is not None:
                stat["L2_name"] += 1
            else:
                stat["L3_orphan"] += 1
                orphan.append(v)

        area = areas.get(st[:6] + "000")
        out.append(Region(
            code=v.code, name=v.name, level=5,
            parent_code=tgt.code if tgt else st,
            prov_code=tgt.prov_code if tgt else (area.prov_code if area else ""),
            prov=tgt.prov if tgt else (area.prov if area else ""),
            city_code=tgt.city_code if tgt else (area.city_code if area else ""),
            city=tgt.city if tgt else (area.city if area else ""),
            area_code=tgt.area_code if tgt else (area.code if area else st[:6] + "000"),
            area=tgt.area if tgt else (area.name if area else ""),
            town_code=tgt.code if tgt else "",
            town=tgt.name if tgt else "",
            stats_town=v.stats_town,
            path=(tgt.path + "/" + v.name) if tgt else ((area.path + "/" + v.name) if area else v.name),
            source="stats",
        ))

    if verbose:
        print(f"    村级挂载：L1 码直挂 {stat['L1_code']} / L2 名称救回 {stat['L2_name']} "
              f"/ L3 无法归属 {stat['L3_orphan']}")
    return out, stat


# ---------------------------------------------------------------- 高层入口

def _count_by_level(rows: list) -> dict:
    d = {}
    for r in rows:
        d[str(r.level)] = d.get(str(r.level), 0) + 1
    return dict(sorted(d.items()))


def load_regions(province: str | int = "450000000", levels=(1, 2, 3, 4),
                 use_cache: bool = True, refresh: bool = False,
                 verbose: bool = True) -> list:
    """获取某省域的 省/市/区县/乡镇 区划（默认走缓存）。"""
    root = code9(province)
    if len(root) != 9:
        # 允许传 "45" 或 "广西"
        if str(province).isdigit():
            root = str(province).ljust(6, "0")[:6].ljust(9, "0")
        else:
            alias = _PROV_ALIAS.get(str(province).strip(), "")
            if not alias:
                raise KeyError(f"无法识别省级名称：{province!r}，请传 9 位省级代码（如 450000000）")
            root = alias

    if use_cache and not refresh:
        got = load_mca_cache(root)
        if got:
            rows, meta = got
            if verbose:
                print(f"  命中缓存：{_mca_cache_path(root).name} "
                      f"（{meta.get('fetched_at','')}，{meta.get('count')} 条）")
            return rows

    rows = crawl_mca(root, levels=levels, verbose=verbose)
    save_mca_cache(rows, root)
    return rows


_PROV_ALIAS = {
    "北京": "110000000", "天津": "120000000", "河北": "130000000", "山西": "140000000",
    "内蒙古": "150000000", "辽宁": "210000000", "吉林": "220000000", "黑龙江": "230000000",
    "上海": "310000000", "江苏": "320000000", "浙江": "330000000", "安徽": "340000000",
    "福建": "350000000", "江西": "360000000", "山东": "370000000", "河南": "410000000",
    "湖北": "420000000", "湖南": "430000000", "广东": "440000000", "广西": "450000000",
    "海南": "460000000", "重庆": "500000000", "四川": "510000000", "贵州": "520000000",
    "云南": "530000000", "西藏": "540000000", "陕西": "610000000", "甘肃": "620000000",
    "青海": "630000000", "宁夏": "640000000", "新疆": "650000000", "台湾": "710000000",
    "香港": "810000000", "澳门": "820000000",
}


# ---------------------------------------------------------------- 自检

#: 广西黄金样例（历史项目实测值，用于回归）
GX_GOLDEN = {"1": 1, "2": 14, "3": 111, "4": 1256, "5": 16681}


def selftest(verbose: bool = True) -> int:
    """离线自检：缓存完整性 + 索引 + 名称解析 + 复合键消歧。"""
    fails = []
    print("[M2] 区划模块自检")
    print("-" * 62)

    # 1) 民政部缓存
    got = load_mca_cache("450000000")
    if not got:
        print("[1] 民政部缓存        未找到 [FAIL]")
        # 2026-10-23 修正：原提示让用户去找一个他根本拿不到的 json 文件，
        # 属于「首次使用必然卡死」的发布缺陷。首次使用应走联网抓取命令。
        print("    首次使用请先联网抓取（广西实测约 60 s），随后本地永久缓存：")
        print("        python regions.py --province 广西 --check")
        print("    仅完全无网的环境才需要离线导入：")
        print("        python regions.py --import-mca <json>")
        return 1
    rows, meta = got
    cnt = _count_by_level(rows)
    ok = all(cnt.get(k, 0) == v for k, v in GX_GOLDEN.items() if k in cnt)
    print(f"[1] 民政部四级区划    省{cnt.get('1',0)} 市{cnt.get('2',0)} "
          f"县{cnt.get('3',0)} 乡{cnt.get('4',0)}  "
          f"{'[OK]' if ok else '[FAIL]'}")
    if not ok:
        fails.append("区划条数与黄金样例不符")

    # 2) 路径与祖先回填
    idx = RegionIndex(rows)
    bad = [r for r in rows if r.level >= 2 and (not r.city_code or not r.prov_code)]
    bad4 = [r for r in rows if r.level == 4 and not r.town_code]
    print(f"[2] 父链回填         缺城市/省 {len(bad)} 条，缺乡镇 {len(bad4)} 条  "
          f"{'[OK]' if not bad and not bad4 else '[FAIL]'}")
    if bad or bad4:
        fails.append("父链回填不完整")

    # 3) path 示例
    nanning = idx.find("南宁市", level=2)
    qx = idx.find("青秀区", level=3)
    print(f"[3] 名称解析         南宁市→{nanning[0].code if nanning else '未命中'}，"
          f"青秀区→{qx[0].code if qx else '未命中'}  "
          f"{'[OK]' if nanning and qx else '[FAIL]'}")
    if not (nanning and qx):
        fails.append("名称解析失败")

    # 4) 简称/别名
    gx = idx.find("广西")
    print(f"[4] 简称归一         '广西' 命中 {len(gx)} 个"
          f"{'（' + gx[0].name + '）' if gx else ''}  {'[OK]' if gx else '[FAIL]'}")
    if not gx:
        fails.append("简称归一失败")

    # 5) 复合键消歧：同名乡镇不串区
    dup_towns = [t for t in rows if t.level == 4]
    name_count = {}
    for t in dup_towns:
        name_count[norm_key(t.name)] = name_count.get(norm_key(t.name), 0) + 1
    dups = [k for k, v in name_count.items() if v > 1]
    sample_ok = 0
    for k in dups[:20]:
        cands = [t for t in dup_towns if norm_key(t.name) == k]
        parent = cands[0].area_code
        hit = idx.match_child_by_name(parent, cands[0].name, level=4)
        if hit is not None and hit.area_code == parent:
            sample_ok += 1
    print(f"[5] 复合键消歧       同名乡镇 {len(dups)} 组，抽 20 组命中 {sample_ok}  "
          f"{'[OK]' if not dups or sample_ok >= min(20, len(dups)) else '[WARN]'}")

    # 6) 村级
    try:
        vs = load_villages("45")
        print(f"[6] 统计局村级       {len(vs)} 条  "
              f"{'[OK]' if len(vs) == GX_GOLDEN['5'] else '[WARN]'}")
    except FileNotFoundError as e:
        vs = []
        print(f"[6] 统计局村级       未导入 [SKIP]  （{str(e).splitlines()[0]}）")

    # 7) 两套代码一致性
    if vs:
        print("[7] 双套代码一致性")
        consistency_check(rows, vs, verbose=True)
    else:
        print("[7] 双套代码一致性   跳过（无村级数据）")

    print("-" * 62)
    if fails:
        print(f"自检结论：FAIL —— {len(fails)} 项不通过：{'；'.join(fails)}")
        return 1
    print("自检结论：PASS（离线项全部通过）")
    return 0


# ---------------------------------------------------------------- CLI

def _main(argv: list) -> int:
    if "--import-mca" in argv:
        src = argv[argv.index("--import-mca") + 1]
        p = import_mca_json(src)
        print(f"已导入民政部区划缓存：{p}")
        return 0
    if "--import-villages" in argv:
        src = argv[argv.index("--import-villages") + 1]
        prefix = argv[argv.index("--prefix") + 1] if "--prefix" in argv else "45"
        p = import_villages_csv(src, prefix=prefix)
        print(f"已导入统计局村级缓存：{p}")
        return 0
    if "--import-streets" in argv:
        src = argv[argv.index("--import-streets") + 1]
        prefix = argv[argv.index("--prefix") + 1] if "--prefix" in argv else "45"
        p = import_streets_csv(src, prefix=prefix)
        print(f"已导入统计局乡镇名缓存：{p}")
        return 0
    if "--selftest" in argv:
        return selftest()
    if "--province" in argv:
        prov = argv[argv.index("--province") + 1]
        rows = load_regions(prov, refresh="--refresh" in argv)
        print(f"区划条数：{_count_by_level(rows)}")
        if "--check" in argv:
            return selftest()
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
