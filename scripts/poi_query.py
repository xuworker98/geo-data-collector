# -*- coding: utf-8 -*-
"""POI 任务采集器（兴趣点 / 门牌地址）——与自然村路径彻底分开。

为什么必须分开（2026-10-21 实测）：
  自然村路径 `full_ladder` 会拼「XX村委会」、用 `token=名字去村去组` 做本体判定。
  套到「南宁市公安局出入境管理分局」上会拼出「…出入境管理分局村委会」——荒谬且必错。
  POI 有自己的规律，判据也完全不同：

POI 三规律（2026-10-21 5 条探针实测）
  R-P1 **不能取第一条**。南宁青秀办证点：高德 #1 是「青秀区南湖办证点」偏 932 m，
        #2 才是本体「青秀区办证点」偏 4 m。取第一 = 选中同名兄弟网点。
  R-P2 **单源会跑飞，必须多源交叉**。桂林叠彩分局办证大厅：高德 7 m，
        百度给出「江南公安分局」偏 347 km（跨市）。跑飞的点拿不到别的源支持。
  R-P3 **门牌是 POI 的最强判据，强于名称**。「玉洞大道33号」「祥宾路1-1号」这类
        门牌号在候选地址里命中，几乎是唯一解；而名称覆盖率会被「出入境管理支队」
        「出入境接待大厅」等近名干扰（实测三条近名互相挤）。

判据 = **加权总分**，不是字典序（2026-10-21 实测修定）：
    R-P4 **门牌分不能绝对优先**。桂林叠彩分局：高德正确点「…叠彩分局·扁山巷6号」
         距用户值仅 7 m，但用户地址写「九华路磨盘山巷9号」——同一大院、不同门牌写法，
         门牌分 = 0；百度却有个门牌分 = 2 而**名称完全不像**（覆盖率 0.00）的候选，
         字典序下门牌分把错的点顶上去，仲裁 516 m、裸取反而 7 m。
         教训：门牌是强证据，但图商门牌写法不一致时会**误伤**，只能加权不能独裁。

    总分 = 覆盖率×5.0 + 门牌分×1.0 + 支持源数×1.5
    覆盖率权重最高：名称像不像是最稳定的判据，门牌可能写法不同、源数可能集体跑偏。
    距地市中心只作**并列时的末位 tie-break**，不进总分（避免距离霸权，与自然村 R15 同律）。

设计红线
  * **不削字**：检索词只用「用户原名 / 用户原地址 / 加区县前缀」三种组合，
    任何改写（去「公安局」、去「办证大厅」）都是在猜，猜错就是系统性偏差。
  * **不偷看答案**：空间锚点只用「地市中心」（公开可查），
    绝不用用户提供的坐标做闸门——那是拿答案给自己判卷。
"""
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config                      # noqa: E402
import gc_common as gc             # noqa: E402

#: POI 源顺序（与自然村 gov 相反：商业/政务 POI 高德、百度标注更全）
POI_ORDER = ["amap", "baidu", "tianditu", "qq"]

#: R-P11 总开关。
#: ⚠ **2026-10-23 A/B 实测后默认关闭**：百度+天地图双源下，
#: 改善 0 / 恶化 1 / 持平 86，净变化 **-1 条** —— 没有任何收益。
#: 根因：R-P11 的收益**依赖高德候选池**。
#: 高德可用时，#65 东兴能从 7965 m 修到 13 m（高德提供了「东兴出入境办证大厅」）；
#: 高德缺位时，被剔的车管所本来也不是最优候选，剔了只是白白改变候选池，
#: 还会经聚类环节把 #98 平果从 53 m 推到 664 m。
#: 教训：**剔除型判据的收益取决于"被剔的候选原本有多强"，源弱时剔除是净损耗。**
#: 高德 Key 恢复后必须重跑同口径 A/B，有收益才开启。
#: 用开关而不是切换备份文件，是为了保证两组除 R-P11 外代码完全一致。
_ENABLE_RP11 = False

#: 仲裁权重（R-P4，2026-10-21 实测定，改前先跑 5 条探针回归）
_W_COV = 5.0     # 名称覆盖率（0~1）——最稳定，权重最高
_W_DOOR = 1.0    # 门牌分（0/2/4）——强但会误伤，只加权
_W_SRC = 1.5     # 支持源数（1~4）——多源一致才可信
_W_AREA = 3.0    # 命中本区县（R-P5）——行政区划互斥，比字面相似可靠
_W_ADDR = 3.0    # 候选地址路名/门牌一致性（R-P7，含 -3 惩罚项，可负分）

#: 地市中心缓存 —— 同一地市只查一次（14 市 = 14 次请求，而非 133 次）
_CITY_ANCHOR = {}
_CITY_LOCK = threading.Lock()

#: 区县名 —— **仅作兜底**，且必须**非贪婪** {2,3}（R-P9，2026-10-22）。
#: 主判据已改为民政部名录字典匹配（见 _area_by_dict），此正则只在名录加载失败时兜底。
#:
#: ⚠ 旧版是贪婪 {2,6}，实测稳定吞字（本轮最严重的根因，直接制造 26 km/41 km 巨差）：
#:   「凭祥市友谊关景区大门北面…」 → 解析成「祥市友谊关景区」（首字被吃）
#:   「东兴市互市贸易区北投 31 栋」→ 解析成「兴市互市贸易区」
#: 补上「市」后缀后又出新形态：「东兴市互市」（「互市」的「市」被当成行政区后缀）。
#:
#: 教训（R-P9）：**正则切分行政区是原罪**——中文地址里「区/县/市」既是行政区后缀，
#: 也是普通用字（景区 / 互市 / 城区 / 市区），靠「字数 + 后缀」永远分不清；
#: 必须用**封闭集合**（权威名录）匹配，名录里没有的串天然不会被误认成区县。
_AREA_RE = re.compile(r"([一-龥]{2,3}(?:县|区|旗|市))")
#: 门牌号：支持「33号」「1-1号」「1—1号」
_NO_RE = re.compile(r"(\d+)\s*[-－—]\s*(\d+)\s*号|(\d+)\s*号")
#: 路名：路/街/大道/巷/道/里/弄
_ROAD_RE = re.compile(r"([一-龥]{2,10}(?:路|街|大道|巷|道|里|弄))")

#: 园区/楼盘类型词（匹配时按长度降序，长词优先，避免被短词截断）
#:
#: ⚠ 刻意**不含单字泛词**（城/府/湾/苑）。离线单测实测：
#: 「北流市城南客运站一楼南侧」会因单字「城」产出「南客运站城」这种垃圾锚点。
#: 真实园区/楼盘名基本都是双字以上（北投互市贸易区 / 南宁市民中心 / 东兴边贸中心），
#: 单字类型词的误伤率远高于收益。
_ZONE_TYPES = ["经济技术开发区", "高新技术开发区", "边境贸易区", "互市贸易区",
               "边民互市贸易区", "跨境合作区", "科技园", "创业园", "物流园",
               "产业园", "工业园", "贸易区", "开发区", "保税区", "商圈",
               "园区", "新区", "工业区", "写字楼", "商厦", "商城", "大厦",
               "大楼", "广场", "市场", "中心", "新城", "小区", "花园",
               "家园", "公寓"]

#: 楼栋号。⚠ 刻意**不含单独的「楼」**——否则「31 栋一楼」的“一楼”
#: （楼层）会被当成栋号提取出来，反而误导。栋号只认 栋/幢/号楼/号房。
_BLD_RE = re.compile(r"(\d{1,3})\s*(?:栋|幢|号楼|号房)")


# ------------------------------------------------------------------ 输入解析

def parse_addr(addr: str, city: str = "") -> dict:
    """从详细地址里解析 区县 / 路名 / 门牌号。

    ⚠ 易错一（2026-10-21 实测）：必须先剥掉「广西壮族自治区」这类省級前缀，
    否则「广西壮族自治区桂林市叠彩区…」会把「壮族自治区」当成区县，
    百度因此失去城市约束、给出 347 km 的跨市结果。
    ⚠ 易错二（同日实测）：剥完省级还得剥**地市名**。
    「桂林市叠彩区九华路…」里区县正则会贪心匹配到「桂林市叠彩区」——把地市吞进区县。
    必须先用 city 参数剥掉「桂林市」，剩下的「叠彩区…」才切得出「叠彩区」。
    """
    s = (addr or "").strip()
    # 1) 剥省级：自治区 / 省
    body = re.sub(r"^[^市]*?(?:壮族自治区|回族自治区|自治区|省)", "", s) or s
    # 2) 剥地市级（用调用方给的地市名，比正则准）
    if city:
        c = city if city.endswith("市") else city + "市"
        if c in body:
            body = body.replace(c, "", 1)

    # 3) 区县：名录字典优先（R-P9），正则兜底
    area = _area_by_dict(body, city)
    if not area:
        m = _AREA_RE.search(body)
        area = m.group(1) if m else ""

    nos = []
    for a, b, c in _NO_RE.findall(body):
        v = f"{a}-{b}" if a else c
        # 门牌号去前导零：「049 号」与「49 号」是同一个门牌，不归一就永远匹配不上
        nos.append("-".join(p.lstrip("0") or "0" for p in v.split("-")))

    # ⚠ 易错三（2026-10-22 实测）：路名必须在**剥掉区县和乡镇之后**再提。
    # 直接对全串提，会把「兴安县兴安镇银杏路」整个当成路名（{2,10} 贪婪匹配），
    # 而候选地址里写的是「银杏路」——两边永远对不上，
    # 正确候选反而被判成"命中了别的路名"吃 -3 分（兴安县实测 59 m 的点被打死）。
    # 这条与自然村那边踩过的坑同源：**切分会把更高级行政区吞进来**。
    rb = body
    if area and area in rb:
        rb = rb.replace(area, "", 1)
    rb = re.sub(r"^[一-龥]{2,8}(?:镇|乡)", "", rb, count=1)
    roads = [r for r in _ROAD_RE.findall(rb)]

    # ⚠ 锚点必须用 rb（已剥区县+乡镇），不能用 body：
    # 县级市不在地市剥离范围内，会把「东兴市」当成园区专名拼进锚点。
    return {"原地址": s, "区县": area, "门牌号": nos, "路名": roads,
            "去省级": body, "园区锚点": zone_anchors(rb),
            "栋号": [x.strip() for x in _BLD_RE.findall(body)]}


#: 锚点垃圾词：**片段中只要出现**就不是园区名（楼层/楼号/单元/行政区等）。
#: 用包含判断而非前缀判断——「二楼中心」「二层中心」这类是楼层词在后面，
#: 只看前缀放不掉（离线单测实测）。
_JUNK_SUB = ("楼", "层", "号", "栋", "单元", "室", "铺", "排",
             "区", "镇", "乡", "村", "巷", "街", "路")


def zone_anchors(body: str) -> list:
    """构造园区/楼盘检索词，**含语序颠倒重构**（R-P10）。

    ⚠ 语序颠倒（2026-10-22 东兴 #65 实测，本条算法的立论依据）：
    用户写「东兴市互市贸易区北投 31 栋一楼」，而图商收录的是
    「**北投**互市贸易区」——第二个专名「北投」被写到了类型词**后面**。
    只按正序拼「互市贸易区」永远搜不到，必须额外试「后置专名 + 类型词」。

    组合公式（**注意不是三段拼接**，离线单测已证伪）：
      A = 类型词前的紧邻专名，B = 类型词后的紧邻专名
      · A + 类型词      → 正序写法（互市贸易区）
      · B + 类型词      → 倒序写法（北投互市贸易区）← 本例的正确项
    曾误写成 B + A + 类型词，产出「北投东兴市互市贸易区」这种垃圾。

    ⚠ 入参必须是**已剥掉区县**的字符串（parse_addr 传的是 rb）：
    县级市（东兴市/北流市）不在地市剥离范围内，不剥就会把「东兴市」当专名
    拼进来，产出「东兴市互市贸易区」——图商根本没这个名字。
    """
    if not body:
        return []
    out = []
    for t in sorted(_ZONE_TYPES, key=len, reverse=True):
        m = re.search(r"([一-龥]{2,4})?" + t + r"([一-龥]{2,4})?", body)
        if not m:
            continue
        a, b = m.group(1) or "", m.group(2) or ""
        # ⚠ 垃圾词只查**专名片段**，绝不查拼好的锚点整体（离线单测实测）：
        # 「互市贸易区」「北投互市贸易区」本身都含「区」，若对整串做检查，
        # 会把本例唯一正确的锚点全部滤掉（东兴锚点一度变成空列表）。
        # 类型词带「区/城/村」是正常的，专名片段带才说明是楼层或行政区。
        for frag in (a, b):
            if not frag:
                continue
            if any(j in frag for j in _JUNK_SUB):
                continue
            cand = (frag + t).strip()
            if len(cand) >= 4 and cand not in out:
                out.append(cand)
        break                                # 只按最长类型词处理一次
    return out[:3]


# ------------------------------------------------- 区县互斥判据（R-P5）

#: 民政部区划缓存（level=3 区县），进程内只加载一次
_AREAS_BY_CITY = None       # {地市名: [(区县全名, 专名), ...]}
_AREAS_BY_STEM = None       # {专名: (地市名, 区县全名)}


def _init_areas(province: str = "450000000"):
    """加载民政部区划名录（权威源，R12 同款），只取 level=3 区县。"""
    global _AREAS_BY_CITY, _AREAS_BY_STEM
    if _AREAS_BY_CITY is not None:
        return
    from regions import load_regions
    by_city, by_stem = {}, {}
    try:
        for r in load_regions(province, levels=(1, 2, 3)):
            if getattr(r, "level", None) != 3:
                continue
            full = (r.name or "").strip()
            stem = re.sub(r"(?:区|县|旗|市)$", "", full)
            if not full:
                continue
            by_city.setdefault(r.city or "", []).append((full, stem))
            if len(stem) >= 2:
                by_stem[stem] = (r.city or "", full)
    except Exception:          # noqa: BLE001
        pass
    _AREAS_BY_CITY, _AREAS_BY_STEM = by_city, by_stem


#: 全省区县扁平表缓存（进程内只建一次，避免每条地址都重建 111 项列表）
_ALL_AREAS = None


def _all_areas() -> list:
    global _ALL_AREAS
    if _ALL_AREAS is None:
        _init_areas()
        _ALL_AREAS = [it for v in (_AREAS_BY_CITY or {}).values() for it in v]
    return _ALL_AREAS


def _area_by_dict(body: str, city: str = "") -> str:
    """用民政部区县名录在地址里找**真实存在**的区县名，取最长命中（R-P9）。

    为什么不用正则：中文地址里「区/县/市」既是行政区后缀、也是普通用字
    （「友谊关景区」「互市贸易区」「凭祥市城区」），靠「字数 + 后缀」的正则
    会稳定吞字——实测「东兴市互市贸易区北投 31 栋」被切成「兴市互市贸易区」，
    连锁打崩下游「区县互斥（R-P5）」判据，把仅差 11 m 的正确答案全当错区剔掉。

    名录是**封闭集合**：「东兴市互市」不在其中 → 天然不会被误认成区县名。
    这一条与 R12「行政编码先行」同源：**凡有权威名录，绝不用正则猜**。

    取最长命中：广西存在「市」与「区」相邻的写法（如「凭祥市城区」），
    长的更具体，优先采纳。

    四级检索（命中即返回，越靠前越可信）：
      1) 本市**全名**（8~14 个，快且不会跨市误命中）
      2) 全省**全名**（应对地址漏写地市、或地市剥除失败）
      3) 本市**专名**（应对「武鸣里建大道」这类——区县名藏在路名里，
         全名写法「武鸣区」根本不出现；限定本市池，跨市重名风险可忽略）
      4) 正则兜底，**但结果必须通过名录校验**（见下）
    """
    if not body:
        return ""
    _init_areas()
    by_city, by_stem = _AREAS_BY_CITY, _AREAS_BY_STEM
    if not by_city:
        # 名录不可用（加载失败）→ 降级为纯正则，至少不比改之前更差
        m = _AREA_RE.search(body)
        return m.group(1) if m else ""

    c = (city or "").strip()
    local = None
    if c:
        for k in (c if c.endswith("市") else c + "市", c):
            if k in by_city:
                local = by_city[k]
                break
    pools = [p for p in (local, _all_areas()) if p]
    # 1)+2)：全名，取最长命中
    for pool in pools:
        best = ""
        for full, _stem in pool:
            if full and full in body and len(full) > len(best):
                best = full
        if best:
            return best
    # 3)：本市专名（不带后缀），取最长命中
    if local:
        best = ""
        for full, stem in local:
            if stem and len(stem) >= 2 and stem in body and len(stem) > len(best):
                best = full
        if best:
            return best
    # 4)：正则兜底 —— ⚠ 必须过名录校验，否则宁可返回空。
    # 实测「南宁东盟经济技术开发区武鸣里建大道」会被正则切成「术开发区」，
    # 这种垃圾值传下去会污染 area_verdict 的 target_stem，比返回空更糟。
    # 教训（R-P9 延伸）：**兜底判据必须能证明自己是对的，证明不了就失效停手**。
    m = _AREA_RE.search(body)
    if m and by_stem:
        stem = re.sub(r"(?:区|县|旗|市)$", "", m.group(1))
        if stem in by_stem:
            return by_stem[stem][1]
    return ""


def area_verdict(text: str, target_stem: str, same_city_stems) -> tuple:
    """区县互斥判定：返回 (判定, 命中他区列表)。

    判定 = 本区 | 错区 | 未判
      * 命中他区县专名 且 未命中本区 → **错区**（硬判据，直接剔除）
      * 两边都命中 → 未判（文本里区县名杂糅，交给其他判据，不硬剔）
      * 都没命中 → 未判（候选地址常省略区县，占多数）

    为什么这是硬判据：同一个 POI 不可能既在叠彩区又在秀峰区，行政区划天然互斥，
    比字面相似度可靠——后者会被「桂林市公安局秀峰分局办证大厅」这种
    **更长、字面重合更多**的错名反超（实测覆盖率 0.75 vs 正确点 0.50）。
    """
    # ⚠ 目标区县未知时**绝不能判错区**（2026-10-22 实测，本轮最严重 bug）：
    # 北流市那条，地址「玉林市北流市城南客运站一楼南侧」里「北流市」是**县级市**，
    # 旧正则只认 县/区/旗 → 解析不出区县 → target_stem="" →
    # 于是所有含「北流」的候选（包括仅差 11 m 的正确答案）全被当成"命中他区"剔除，
    # 剔完只剩 26 km 外的「容县城南客运站」，结果从 11 m 崩到 26618 m。
    # 教训：**判据缺输入时要失效停手，不能反向把正确项全杀掉**。
    if not target_stem:
        return "未判", []
    hit_target = target_stem in text
    hit_other = [s for s in (same_city_stems or ())
                 if s and s != target_stem and s in text]
    if hit_other and not hit_target:
        return "错区", hit_other
    if hit_target:
        return "本区", hit_other
    return "未判", hit_other


def same_city_area_stems(city: str) -> list:
    """取同地市下所有区县专名（用于错区判定）。"""
    _init_areas()
    if not _AREAS_BY_CITY:
        return []
    c = city if city.endswith("市") else (city + "市" if city else "")
    stems = [s for _, s in _AREAS_BY_CITY.get(c, [])]
    # 地市名对不上时（如「崇左市」vs「崇左」），退而求其次：按前缀模糊匹配
    if not stems and city:
        for k, v in _AREAS_BY_CITY.items():
            if city in k or k.replace("市", "") in city:
                stems += [s for _, s in v]
    return [s for s in stems if len(s) >= 2]


# ------------------------------------------------------------------ 判据

def doorplate_score(cand_addr: str, want: dict) -> int:
    """门牌命中分：路名命中 +2，号数命中 +2，两者都中 +4。

    号数用「集合包含」而非全等：候选地址常写成「玉洞大道33号南宁市民中心A座」，
    用户写「玉洞大道33号A座」，号数一致即可，不必串全等。
    """
    ca = (cand_addr or "")
    if not want.get("门牌号") and not want.get("路名"):
        return 0
    score = 0
    if want.get("门牌号"):
        cand_nos = set()
        for a, b, c in _NO_RE.findall(ca):
            cand_nos.add(f"{a}-{b}" if a else c)
        # 号数集合有交集即算命中（「1-1」与「1」「1-1」都算）
        if cand_nos & set(want["门牌号"]):
            score += 2
    if want.get("路名"):
        cand_roads = set(_ROAD_RE.findall(ca))
        if cand_roads & set(want["路名"]):
            score += 2
    return score


# ---------------------------------------------- 阶段一：地址地理编码（R-P6）

#: 地理编码级别白/黑名单。
#: ⚠ 为什么必须过滤（2026-10-22 实测）：灌阳县那条，三源地理编码分别返回
#: 「道路 / 门址 / 乡镇街道」——后两个是**粗定位**，落到县城中心，离真值 1.9 km。
#: 门址才是能落到门牌点的级别。不分级就等于拿乡镇中心当 POI 答案。
_GEO_BAD_TOKENS = ("道路", "乡镇", "街道", "区县", "区", "县", "城市", "省", "村庄")
_GEO_GOOD_TOKENS = ("门址", "兴趣点", "政府", "企事业单位", "POI", "大厦", "大楼")


def geo_level_ok(level: str) -> bool:
    """地理编码结果级别是否够精确（能落到门牌/楼栋，而非路或乡镇中心）。"""
    lv = (level or "").strip()
    if not lv:
        return False
    if any(t in lv for t in _GEO_BAD_TOKENS):
        # 「乡镇街道」含 乡镇/街道 → 剔除；但「门址」不含 → 放行
        if not any(g in lv for g in ("门址",)):
            return False
    return any(g in lv for g in _GEO_GOOD_TOKENS)


def geocode_addr_all(addr: str, city: str, sources: list,
                     parallel: bool = True,
                     per_src_workers: int = 3) -> list:
    """R-P6 阶段一：详细地址 → 各源**地理编码**（不是 POI 搜索）。

    这是 2026-10-22 按用户批评新建的路径。此前把地址当关键词丢给 POI 搜索
    （place/v2/search），门牌串会被降级到近似物；
    正确做法是走地理编码（geocoding），才能落到门牌点。

    实测收益（南宁/柳州样本）：
      良庆受理点 地址地理编码 29 m（旧 POI 路径 2951 m）
      黄村派出所 地址地理编码  1 m（旧 POI 路径 1974 m）
    """
    if not addr:
        return []
    out = []
    lock = __import__("threading").Lock()

    def _one(src):
        v = None
        try:
            if src == "amap":
                r = gc.amap_geocode(addr, city=city)
                if r:
                    v = (r[0], r[1], r[2], r[3] or "")
            elif src == "baidu":
                r = gc.baidu_geocode(addr, city=city)
                if r:
                    v = (r[0], r[1], r[2], "")
            elif src == "tianditu":
                r = gc.td_geocode(addr, city=city)
                if r:
                    v = (r[0], r[1], r[3], "")
        except gc.QuotaExceeded as e:
            gc.mark_source_fail(src, daily=getattr(e, "daily", False))
            return
        except Exception:      # noqa: BLE001
            return
        if not v:
            return
        gc.mark_source_ok(src)
        if not geo_level_ok(v[2]):
            with lock:
                out.append({"源": src, "lon": v[0], "lat": v[1],
                            "级别": v[2], "格式化": v[3], "可用": False})
            return
        with lock:
            out.append({"源": src, "lon": v[0], "lat": v[1],
                        "级别": v[2], "格式化": v[3], "可用": True})

    if parallel and len(sources) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(len(sources) * 2, 8)) as ex:
            list(ex.map(_one, sources))
    else:
        for s in sources:
            _one(s)
    return out


def _pick_by_address(addr_pts: list, name_rows: list,
                     cluster_m: int = 300, proof_m: int = 200) -> dict | None:
    """R-P6 地址优先择优（瀑布式）。返回 None = 地址路径不可用，交回名称仲裁。

    ① **多源地址一致**（≥2 源落在 300 m 内）——互相印证，最可信
    ② **单点地址 + 名称候选互证**（名称候选落在地址点 200 m 内）
       ——名称反过来证明"这个门牌确实有这个单位"，防地址查错地方
    ③ 都不满足 —— 地址信息不足以定位，退回名称仲裁

    互证时选**门牌分最高**的名称候选，而非距离最近的：
    实测柳北黄村派出所，救回它的候选叫「北雀路45号之六」——名字与查询词
    「柳北分局黄村派出所户籍室」**一个字都不重合**（覆盖率极低），
    但门牌 45 号之六完全命中。若按覆盖率选，这条会再次被刷掉。
    """
    good = [p for p in (addr_pts or []) if p.get("可用")]
    if not good:
        return None

    cl = []
    for p in good:
        for c in cl:
            if gc.haversine_m(c["lon"], c["lat"], p["lon"], p["lat"]) <= cluster_m:
                c["pts"].append(p)
                break
        else:
            cl.append({"lon": p["lon"], "lat": p["lat"], "pts": [p]})

    def _level(pts):
        for p in pts:
            if "门址" in (p["级别"] or ""):
                return p["级别"]
        return pts[0]["级别"]

    def _near(c):
        """簇附近 200 m 内的名称候选，按 门牌分→覆盖率→距离 择优"""
        ns = []
        for r in name_rows:
            d = gc.haversine_m(c["lon"], c["lat"], r["lon"], r["lat"])
            if d <= proof_m:
                ns.append((d, r))
        if not ns:
            return None
        # R-P7：地址分（含 -3 分厅惩罚）优先于门牌分与覆盖率。
        # 政务服务中心的主厅/分厅**名字和覆盖率几乎一样**，只有地址路名能分开。
        return sorted(ns, key=lambda x: (-x[1].get("地址分", 0),
                                         -x[1]["门牌分"],
                                         -x[1]["覆盖率"], x[0]))[0]

    # ① 多源一致
    multi = [c for c in cl if len({x["源"] for x in c["pts"]}) >= 2]
    if multi:
        best = max(multi, key=lambda c: len({x["源"] for x in c["pts"]}))
        srcs = sorted({x["源"] for x in best["pts"]})
        lv = _level(best["pts"])
        hit = _near(best)
        if hit:
            d, r = hit
            return {"lon": r["lon"], "lat": r["lat"], "地址源": srcs,
                    "方式": f"地址{len(srcs)}源一致+名称互证", "级别": lv,
                    "覆盖率": r["覆盖率"], "互证名": (r["名称"] or "")[:30]}
        return {"lon": best["lon"], "lat": best["lat"], "地址源": srcs,
                "方式": f"地址{len(srcs)}源一致", "级别": lv,
                "覆盖率": 0.0, "互证名": ""}

    # ② 孤立地址 + 名称互证
    for c in cl:
        hit = _near(c)
        if hit:
            d, r = hit
            return {"lon": r["lon"], "lat": r["lat"],
                    "地址源": [c["pts"][0]["源"]],
                    "方式": "单点地址+名称互证", "级别": _level(c["pts"]),
                    "覆盖率": r["覆盖率"], "互证名": (r["名称"] or "")[:30]}
    return None


# ------------------------------------------------- 业务条线互斥（R-P11）

#: 公安/政务系统内部的业务条线词。**跨条线即不同的人、不同的窗口、不同的位置**。
_BIZ_LINES = {
    "出入境": ["出入境", "办证大厅", "签证", "口岸签证", "出国境", "护照"],
    "车管交管": ["车辆管理", "交通管理", "车管所", "交警", "驾驶证", "机动车",
                "查验岗", "驾驶人"],
    "户政户籍": ["户政", "户籍", "身份证", "户口"],
    "治安": ["治安管理", "治安大队", "治安支队", "特行"],
    "刑侦": ["刑侦", "刑事侦查", "刑警"],
    "禁毒": ["禁毒", "毒品"],
    "经侦": ["经侦", "经济侦查", "经济犯罪"],
    "网安": ["网安", "网络安全保卫"],
    "巡特警": ["巡警", "特警", "巡特"],
    "信访": ["信访"],
    "看守所": ["看守所", "拘留所", "戒毒所"],
}


def biz_lines(text: str) -> set:
    """返回文本命中的业务条线集合。"""
    t = text or ""
    return {ln for ln, kws in _BIZ_LINES.items() if any(k in t for k in kws)}


def cross_biz_conflict(target: str, cand_text: str) -> str:
    """业务条线是否互斥。返回空串=放行，非空=应剔除的原因。

    为什么这是**硬判据**而不是降权（R-P11，2026-10-22 实测）：
    名称覆盖率对「同一个系统里不同业务条线」的 POI 天然失效——
      · 目标「东兴市公安局出入境管理大队」
      · 正确的「东兴出入境办证大厅」          覆盖率 0.29，距你 13 m
      · 错选的「…公安局交通管理大队车辆管理所」覆盖率 0.71，距你 7965 m
    车管所与出入境是两条线、两个窗口、两栋楼，可「东兴市公安局…管理大队」
    这几个字完全重合，字面更像的那个反而赢了，直接跑偏 8 km。

    为什么要求目标**恰好命中 1 条线**（len==1）：
    不带条线词的目标（如「XX分局办证大厅」）无法判定所属线，一律放行；
    同时命中多条线的目标（如「车管、出入境业务窗口」）本身就是综合窗口，
    此时剔除任何一侧都会误伤 → 也放行。
    """
    tl = biz_lines(target)
    if len(tl) != 1:
        return ""
    cl = biz_lines(cand_text)
    if cl and cl != tl:
        return f"跨条线(目标={'/'.join(tl)};候选={'/'.join(cl)})"
    return ""


def addr_match_score(cand_addr: str, want: dict) -> int:
    """R-P7 候选地址与用户地址的路名/门牌**一致性**（含惩罚项）。

    发现过程（2026-10-22 实测）：政务服务中心在图商库里是一个**主厅 + N 个分厅**
    的集合体，名字都以「XX政务服务中心」开头，光看名字无法区分：
      凤山县政务服务中心  主厅址=思源社区龙华路1号（用户地址正是这里）
      凤山县政务服务中心办证大厅  址=朝阳大道      ← 分厅
      凤山县政务服务中心工商质监分厅 址=中山路      ← 分厅
      凤山县政务服务中心残联分厅  址=教育路         ← 分厅
    四条名字、覆盖率几乎一样，**只有地址里的路名能区分**。
    旧版门牌分只加分不扣分，分厅照样得分 → 选错。

    返回值：
      +4 命中用户路名 **且** 命门牌号（最强，就是用户写的那个门面）
      +2 仅命中路名
      -3 命中了**别的**路名（说明是另一个门面/分厅）
       0 候选地址里没有路名（图商常省略，信息不足，不奖不罚——避免误伤）
    """
    ca = cand_addr or ""
    if not ca:
        return 0
    want_roads = set(want.get("路名") or [])
    want_nos = set(want.get("门牌号") or [])
    if not want_roads and not want_nos:
        return 0
    cand_roads = list(_ROAD_RE.findall(ca))
    cand_nos = set()
    for a, b, c in _NO_RE.findall(ca):
        v = f"{a}-{b}" if a else c
        cand_nos.add("-".join(p.lstrip("0") or "0" for p in v.split("-")))
    # 用**子串**匹配而非集合相等：候选地址常写成「博白镇锦绣东路」，
    # 用户侧已剥干净为「锦绣东路」，集合交集为空、子串包含为真。
    hit_road = any(w and w in ca for w in want_roads)
    hit_no = bool(cand_nos & want_nos)
    cand_matched = any(any(w and w in cr for w in want_roads) for cr in cand_roads)
    other_road = bool(cand_roads) and not cand_matched
    if hit_road and hit_no:
        return 4
    if hit_road:
        return 2
    if other_road:
        return -3
    return 0


def city_anchor(city: str) -> tuple | None:
    """查地市中心（公开可查，非答案），带缓存。用于末位距离判据。"""
    city = (city or "").strip()
    if not city:
        return None
    with _CITY_LOCK:
        if city in _CITY_ANCHOR:
            return _CITY_ANCHOR[city]
    anc = None
    try:
        r = gc.query_point(f"{city}市人民政府", city=city, task="gov")
        if r:
            anc = (r["lon"], r["lat"])
    except Exception:      # noqa: BLE001
        anc = None
    with _CITY_LOCK:
        _CITY_ANCHOR[city] = anc
    return anc


# ------------------------------------------------------------------ 主入口

def poi_ladder(name: str, addr_info: dict, city: str = "") -> list:
    """POI 检索词阶梯 —— 只组合，不改写。

    顺序按命中率：全称最准（探针 5 条中 4 条全称直接命中），
    地址次之（门牌地理编码，救「名称查不到」的），区县+名称兜底（补城市约束救百度）。
    """
    out = []

    def add(s):
        s = (s or "").strip()
        if s and s not in out:
            out.append(s)

    add(name)                                    # 原名（不削字）
    add(addr_info.get("去省级") or addr_info.get("原地址"))   # 详细地址
    a = addr_info.get("区县") or ""
    if a and a not in name:
        add(f"{a}{name}")                        # 区县 + 原名（补城市约束）
    return out


def query_poi_full(name: str, addr: str = "", city: str = "",
                   sources: list | None = None, count: int = 10,
                   max_ladder: int = 3, parallel: bool = True,
                   per_src_workers: int = 5, cluster_m: int = 300,
                   city_anchor_m: float | None = None) -> dict:
    """POI 单点采集：四源 × 阶梯 → 候选池 → 仲裁 → WGS84。

    返回 dict（必然返回，未命中也带完整候选供判断）：
      {"命中", "lon", "lat", "来源", "生效检索词", "门牌分", "名称覆盖率",
       "支持源", "支持源数", "偏差参考", "候选", "轨迹", "请求数"}
    """
    srcs = sources or [s for s in POI_ORDER if s in config.available_sources()]
    if not srcs:
        raise KeyError("没有任何已配置 Key 的数据源，无法联网查询。")

    info = parse_addr(addr, city)
    ladder = poi_ladder(name, info, city)[:max_ladder]

    # R-P5 区县互斥：目标区县专名 + 同城其他区县专名
    _init_areas()
    tgt_stem = re.sub(r"(?:区|县|旗|市)$", "", info["区县"] or "")
    city_stems = same_city_area_stems(city)

    # 地市锚点：**公开查询**，绝不拿用户坐标当闸门
    anchor = city_anchor(city)

    per_src_workers = max(1, min(int(per_src_workers or 1), gc._MAX_PER_SRC))
    tasks = [(qi, q, s) for qi, q in enumerate(ladder, 1) for s in srcs]

    rows, trace, n_req = [], [], [0]
    lock = threading.Lock()

    def _one(src: str, q: str, qi: int):
        # 熔断前置检查：源已熔断就别再发请求（实测腾讯配额耗尽后每点白烧 3 次，
        # 133 点 = 399 次无效请求）。省下的配额留给真正能返回数据的检索词。
        if not gc.source_available(src):
            with lock:
                trace.append({"阶梯": qi, "检索词": q, "源": src,
                              "结果": "已熔断，跳过"})
            return
        try:
            hits = gc.multi_search(src, q, city=city, area=info["区县"],
                                   count=count)
        except gc.QuotaExceeded as e:
            tripped = gc.mark_source_fail(src, daily=getattr(e, "daily", False))
            with lock:
                trace.append({"阶梯": qi, "检索词": q, "源": src,
                              "结果": f"配额 {e}" + ("｜已熔断" if tripped else "")})
            return
        except Exception as e:                    # noqa: BLE001
            with lock:
                trace.append({"阶梯": qi, "检索词": q, "源": src,
                              "结果": f"ERR {type(e).__name__}"})
            return
        gc.mark_source_ok(src)
        with lock:
            n_req[0] += 1
            trace.append({"阶梯": qi, "检索词": q, "源": src, "原始": len(hits)})
            for h in hits:
                if h.get("lon") is None:
                    continue
                text = (h.get("name") or "") + (h.get("addr") or "")
                vd, others = area_verdict(text, tgt_stem, city_stems)
                rows.append({
                    "源": src, "阶梯": qi, "检索词": q,
                    "lon": h["lon"], "lat": h["lat"],
                    "名称": h.get("name") or "",
                    "地址": h.get("addr") or "",
                    "门牌分": doorplate_score(h.get("addr") or "", info),
                    "地址分": addr_match_score(h.get("addr") or "", info),
                    "覆盖率": gc._coverage(h.get("name") or "", name),
                    "区县判定": vd, "命中他区": others,
                })

    # ---- 阶段一：地址地理编码（R-P6）
    # 仅在地址含「路名 + 门牌号」时启用。地址信息不足时
    # （如「…自治县安阳镇政务服务中心四楼」无路名无门牌），地理编码只会退到
    # 乡镇中心，反而比名称搜索更差（2026-10-22 实测都安/灌阳两条）。
    addr_q = info["去省级"] or info["原地址"]
    use_addr = bool(info["路名"] and info["门牌号"])
    addr_pts = []
    addr_lock = threading.Lock()

    def _geo_one(src):
        v = None
        try:
            if src == "amap":
                r = gc.amap_geocode(addr_q, city=city)
                if r:
                    v = (r[0], r[1], r[2], r[3] or "")
            elif src == "baidu":
                r = gc.baidu_geocode(addr_q, city=city)
                if r:
                    v = (r[0], r[1], r[2], "")
            elif src == "tianditu":
                r = gc.td_geocode(addr_q, city=city)
                if r:
                    v = (r[0], r[1], r[3], "")
        except gc.QuotaExceeded as e:
            gc.mark_source_fail(src, daily=getattr(e, "daily", False))
            with addr_lock:
                trace.append({"阶梯": "地址", "检索词": addr_q[:30], "源": src,
                              "结果": f"配额 {e}"})
            return
        except Exception as e:                  # noqa: BLE001
            with addr_lock:
                trace.append({"阶梯": "地址", "检索词": addr_q[:30], "源": src,
                              "结果": f"ERR {type(e).__name__}"})
            return
        if not v:
            with addr_lock:
                trace.append({"阶梯": "地址", "检索词": addr_q[:30], "源": src,
                              "结果": "无返回"})
            return
        gc.mark_source_ok(src)
        ok = geo_level_ok(v[2])
        with addr_lock:
            n_req[0] += 1
            trace.append({"阶梯": "地址", "检索词": addr_q[:30], "源": src,
                          "原始": 1, "级别": v[2], "可用": ok})
            addr_pts.append({"源": src, "lon": v[0], "lat": v[1],
                             "级别": v[2], "格式化": v[3] or "", "可用": ok})

    if parallel and len(tasks) > 1:
        sems = {s: threading.Semaphore(per_src_workers) for s in srcs}

        def _run(t):
            qi, q, s = t
            with sems[s]:
                _one(s, q, qi)

        # 名称搜索与地址地理编码**并发**发起，地址路径不额外增加墙钟时间
        with ThreadPoolExecutor(max_workers=len(srcs) * per_src_workers + 4) as ex:
            futs = [ex.submit(_run, t) for t in tasks]
            if use_addr:
                futs += [ex.submit(_geo_one, s) for s in srcs]
            for f in futs:
                f.result()
    else:
        for qi, q, s in tasks:
            _one(s, q, qi)
        if use_addr:
            for s in srcs:
                _geo_one(s)

    out = {"名称": name, "地址": addr, "地市": city, "区县": info["区县"],
           "命中": False, "lon": None, "lat": None, "来源": "", "生效检索词": "",
           "门牌分": 0, "覆盖率": 0.0, "支持源": [], "支持源数": 0,
           "候选": [], "轨迹": trace, "请求数": n_req[0], "存疑": []}
    if not rows:
        out["存疑"].append("四源全部无命中")
        return out

    # --- R-P5 硬剔除：命中的是**别的区县**且不是本区的候选
    keep = [r for r in rows if r["区县判定"] != "错区"]
    dropped = [{"名称": r["名称"][:30], "地址": r["地址"][:40],
                "命中他区": r["命中他区"], "源": r["源"]}
               for r in rows if r["区县判定"] == "错区"]
    if not keep:
        # 宁可不剔也不误剔：整池判错区说明区县解析本身可疑，恢复全池并标注
        keep, dropped = rows, []
        out["存疑"].append("全部候选被判错区，区县解析可疑，已恢复全池")
    out["剔出"] = dropped[:6]
    rows = keep

    # --- R-P11 业务条线互斥硬剔除（2026-10-22）
    # 详见 cross_biz_conflict() 注释：车管所把出入境办证大厅顶掉的是同一 DID 里
    # 字面重合更高、业务却完全不同的错名，必须靠条线互斥才拦得住。
    _biz = []
    if _ENABLE_RP11 and biz_lines(name):
        keep2 = []
        for r in rows:
            # ⚠ 只用**名称**判条线，不拼地址（2026-10-22 样本修正）：
            # 地址里常出现交叉字眼（如户政大厅的地址写着"…出入境办证大厅旁"），
            # 会把本该放行/本该剔除的候选判反。「某某户政业务办证大厅」是户政窗口
            # 这件事，写在它的名字里，不写在地址里。
            why = cross_biz_conflict(name, r.get("名称") or "")
            if why:
                _biz.append({"名称": (r.get("名称") or "")[:30], "原因": why})
            else:
                keep2.append(r)
        if keep2:
            # R-P11 保护（2026-10-22 全量实测后补）：
            # 剔除完不能让候选池退化到「泛指实体」。
            # 实测 #133 扶绥：剔掉交警大队/公路巡警后，最优候选变成「扶南大道」
            # ——一条路的名字，既不是 POI 也不是任何业务窗口，距离从 24 m 掉到 139 m。
            # 判据：剔除后的最优候选**连任何业务条线词都没有** → 说明好候选被剔光了，
            # 此时宁可放错候选进来也不能让路名上位。
            # （#65 东兴不受影响：剔除后最优是「东兴出入境办证大厅」，含「出入境」。）
            top2 = max(keep2, key=lambda x: x["覆盖率"])
            if biz_lines(top2.get("名称") or ""):
                rows = keep2
                out["剔出跨条线"] = _biz[:6]
            else:
                out["存疑"].append(
                    f"业务条线剔除后最优候选退化为泛指实体"
                    f"（{top2.get('名称') or ''[:20]}），已放弃剔除")
        else:
            out["存疑"].append("候选全部命中跨业务条线，疑似词表误伤，已恢复全池")
    out["剔出跨条线"] = _biz[:6]

    # 地址路径结果**先算出来但不短路**（R-P8，见下方瀑布式）
    addr_pick = _pick_by_address(addr_pts, rows) if addr_pts else None

    # --- 聚簇（同一点的不同源表述归为一簇）
    clusters = []
    for r in sorted(rows, key=lambda x: (x["阶梯"], POI_ORDER.index(x["源"])
                                         if x["源"] in POI_ORDER else 9)):
        for c in clusters:
            if gc.haversine_m(c["lon"], c["lat"], r["lon"], r["lat"]) <= cluster_m:
                c["rows"].append(r)
                break
        else:
            clusters.append({"lon": r["lon"], "lat": r["lat"],
                             "rows": [r]})

    ranked = []
    for i, c in enumerate(clusters, 1):
        srcs_c = sorted({x["源"] for x in c["rows"]})
        d_anchor = (gc.haversine_m(c["lon"], c["lat"], anchor[0], anchor[1])
                    if anchor else None)
        ranked.append({
            "簇号": i, "lon": c["lon"], "lat": c["lat"],
            "门牌分": max(x["门牌分"] for x in c["rows"]),
            # R-P7：簇内地址分取 max——主厅(+4) 与分厅(-3) 常落在同一簇，
            # 取 max 才能选出"用户写的那个门面"，而不是被分厅拖平。
            "地址分": max((x.get("地址分", 0) for x in c["rows"]), default=0),
            "覆盖率": max(x["覆盖率"] for x in c["rows"]),
            "区县判定": "本区" if any(x["区县判定"] == "本区" for x in c["rows"])
                        else "未判",
            "源数": len(srcs_c), "支持源": srcs_c,
            "代表名": max(c["rows"], key=lambda x: x["覆盖率"])["名称"][:40],
            "地址": max(c["rows"], key=lambda x: x["门牌分"])["地址"][:50],
            "距地市中心_m": round(d_anchor) if d_anchor is not None else None,
        })

    # --- 排序：加权总分（R-P4/R-P5）→ 并列时才看距地市中心
    for c in ranked:
        c["总分"] = round(c["覆盖率"] * _W_COV
                          + c["门牌分"] * _W_DOOR
                          + c["源数"] * _W_SRC
                          + c["地址分"] * _W_ADDR
                          + (_W_AREA if c["区县判定"] == "本区" else 0.0), 2)
    ranked.sort(key=lambda c: (-c["总分"],
                               c["距地市中心_m"] if c["距地市中心_m"] is not None
                               else 1e12))
    for i, c in enumerate(ranked, 1):
        c["名次"] = i

    # --- R-P8 瀑布式判定（2026-10-22 第三版，实测 8/8 判对）
    # 之前两版都是"按路径偏好"排序，改成"按证据强度"后才稳定：
    #   旧版一（地址独裁）：博白地址落点 570 m，盖住了名称候选里 2 m 的答案
    #   旧版二（名称优先）：资源县被名称簇带偏到 1515 m，而地址路径只要 19 m
    # 逐条实测后发现**唯一能区分的判据是地址候选的源数一致性**：
    #   地址 ≥2 源聚在 300 m 内 → 图商对这段门牌有共识 → 用地址
    #       （资源县 19/21 m、田东 15/22 m、龙胜 12/32/32 m、贺州 55/62/74 m）
    #   地址单点 / 分散 >300 m → 图商对地址没共识，地址路径会飘 → 用名称
    #       （博白仅 1 源且 574 m、甘圩仅 1 源 221 m、兴安 0 源、黄村 2 源但相距 433 m）
    best, way = None, ""
    addr_multi = bool(addr_pick and "源一致" in (addr_pick.get("方式") or ""))

    # ① 地址多源一致 → 直接用（最强证据）
    if addr_multi:
        ap = addr_pick
        out.update({
            "命中": True, "lon": ap["lon"], "lat": ap["lat"],
            "来源": "地址·" + "/".join(ap["地址源"]),
            "生效检索词": addr_q[:40],
            "门牌分": 2, "覆盖率": ap.get("覆盖率", 0.0),
            "支持源": ["地址"] + ap["地址源"], "支持源数": len(ap["地址源"]),
            "候选": ranked[:6], "命中方式": "①" + ap["方式"],
            "地址级别": ap["级别"], "地址互证名": ap.get("互证名", ""),
        })
        if ap.get("级别") and any(t in ap["级别"]
                                  for t in ("道路", "乡镇", "街道")):
            out["存疑"].append("地址仅解析到道路/乡镇级，精度有限")
        return out

    # ② 地址单点 + 名称互证
    #    ⚠ 顺序实测（第三版）：只要地址路径有结果，就排在名称路径**前面**。
    #    第二版把「名称门牌+多源」放在地址单点之前，结果防城港/平南/黄村三条
    #    被名称簇带偏到 2080/1632/588 m，而地址单点只给 37/138/1 m。
    #    原因：地址单点至少是**图商对这段门牌的解析**，比靠名字相似度猜的位置有据。
    if addr_pick:
        ap = addr_pick
        out.update({
            "命中": True, "lon": ap["lon"], "lat": ap["lat"],
            "来源": "地址·" + "/".join(ap["地址源"]),
            "生效检索词": addr_q[:40],
            "门牌分": 2, "覆盖率": ap.get("覆盖率", 0.0),
            "支持源": ["地址"] + ap["地址源"], "支持源数": len(ap["地址源"]),
            "候选": ranked[:6], "命中方式": "②" + ap["方式"],
            "地址级别": ap["级别"], "地址互证名": ap.get("互证名", ""),
        })
        if ap.get("级别") and any(t in ap["级别"]
                                  for t in ("道路", "乡镇", "街道")):
            out["存疑"].append("地址仅解析到道路/乡镇级，精度有限")
        return out

    # ③ 名称 地址分≥2 且 源数≥2：候选地址含用户门牌，且不止一家这么说
    for c in ranked:
        if c["地址分"] >= 2 and c["源数"] >= 2:
            best, way = c, "③门牌+多源互证"; break
    # ④ 名称三源一致 + 覆盖率≥0.85：名字一样且三家都指同一处
    if best is None:
        for c in ranked:
            if c["源数"] >= 3 and c["覆盖率"] >= 0.85:
                best, way = c, "④名称3源一致"; break
    if best is None:
        best, way = ranked[0], "⑤名称仲裁"

    out.update({
        "命中": True, "lon": best["lon"], "lat": best["lat"],
        "来源": "/".join(best["支持源"]),
        "生效检索词": best.get("代表名", ""),
        "门牌分": best["门牌分"], "覆盖率": round(best["覆盖率"], 2),
        "支持源": best["支持源"], "支持源数": best["源数"],
        "候选": ranked[:6], "命中方式": way,
    })

    # --- 存疑标注（不裸改，只提示）
    if best["门牌分"] == 0 and best["覆盖率"] < 0.5:
        out["存疑"].append("门牌与名称均未命中，可能是近似实体")
    if best["源数"] == 1:
        out["存疑"].append("仅单源支持，无交叉验证")
    if len(ranked) > 1 and ranked[1]["门牌分"] == best["门牌分"] \
            and ranked[1]["覆盖率"] == best["覆盖率"] \
            and ranked[1]["源数"] == best["源数"]:
        out["存疑"].append(f"与第 2 名判据完全并列，禁止机器破平，须用户判定")
    return out


def classify_deviation(dev_m, coverage: float = 0.0) -> tuple:
    """给用户看的结论分级（**只标注，绝不替用户改数**）。

    返回 (结论, 建议动作)。结论以编号开头，方便在 Excel 里筛选排序。

    五级的依据（2026-10-21 133 条实测）：
      ①② 偏差 ≤300 m —— 中位 50 m，与图商标注精度同量级，可用；
      ③ 名称覆盖率 ≥0.9 却差 >300 m —— **名字几乎完全一致、坐标却对不上**，
         无法判断是图商标错还是参考值标错，只能人工核（秀峰分局办证大厅即此类，
         覆盖率 1.00 仍差 2007 m）。这类最危险，因为它"看起来全对"；
      ④ 覆盖率 0.3~0.9 —— 命中了同院/同名的其他实体（雁山分局→刑侦一大队）；
      ⑤ 覆盖率 <0.3 —— 图商库里疑似根本没有这个 POI（平果市→供销大厦 117 km），
         改算法无效，只能换数据源或人工补录。
    """
    if dev_m is None or dev_m == "":
        return "⑦ 无采集结果", "人工补录"
    d = float(dev_m)
    cov = float(coverage or 0)
    # 门槛按用户 2026-10-22 拍板：**≤100 m 为达标**
    if d <= 50:
        return "① 可直接用（≤50m）", "无需处理"
    if d <= 100:
        return "② 可用（50–100m）", "无需处理"
    if d <= 300:
        return "③ 未达标（100–300m）", "需复核"
    if cov >= 0.9:
        return "④ 名称一致但坐标冲突", "必须人工判定谁标错"
    if cov >= 0.3:
        return "⑤ 疑似命中其他近似实体", "人工确认是否同一处"
    return "⑥ 图商疑似无此POI", "换数据源或人工补录"


if __name__ == "__main__":
    import json
    r = query_poi_full("南宁市公安局出入境管理分局",
                       "南宁市良庆区玉洞大道33号A座出入境接待大厅（南宁市民中心）",
                       city="南宁")
    print(json.dumps({k: v for k, v in r.items() if k != "轨迹"},
                     ensure_ascii=False, indent=2))
