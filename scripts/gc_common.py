# -*- coding: utf-8 -*-
"""能力层：坐标引擎 + 四源适配 + 限速重试

坐标系归一链路：
  天地图 CGCS2000 -> pyproj EPSG:4490->4326 -> WGS84
  高德/腾讯 GCJ-02 -> 迭代逆解 -> WGS84
  百度  BD-09 -> GCJ-02 -> 迭代逆解 -> WGS84

重要：坐标转换算法基于公开的 GCJ-02 / BD-09 转换公式实现（黄金向量往返校验误差 <1m），
      请勿改写公式，尤其是 gcj02_to_wgs84 的 6 次迭代逆解。
"""
import json
import math
import os
import random
import re
import ssl
import time
import warnings
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from config import get_key, http_opener, load_config, mask_key

# ---------------------------------------------------------------- 坐标转换

_A = 6378245.0
_EE = 0.00669342162296594323

try:
    import pyproj
    _T4490_4326 = pyproj.Transformer.from_crs("EPSG:4490", "EPSG:4326", always_xy=True)

    def cgcs2000_to_wgs84(lon, lat):
        """天地图 CGCS2000 -> WGS84（严格转换）"""
        return _T4490_4326.transform(lon, lat)
except ImportError:  # 无 pyproj 时降级为恒等（CGCS2000 与 WGS84 差异 <1m）
    warnings.warn(
        "未安装 pyproj：天地图 CGCS2000 坐标将按恒等处理（与 WGS84 差异 <1m）。"
        "需要严格转换请执行 pip install pyproj",
        RuntimeWarning, stacklevel=2)

    def cgcs2000_to_wgs84(lon, lat):
        return lon, lat


def _transform_lat(x, y):
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x, y):
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lon, lat):
    """WGS84 -> GCJ-02（正向加密）"""
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * math.pi)
    dlon = (dlon * 180.0) / (_A / sqrtmagic * math.cos(radlat) * math.pi)
    return lon + dlon, lat + dlat


def gcj02_to_wgs84(lon, lat):
    """GCJ-02 -> WGS84（标准迭代逆解，6 次迭代往返误差 <1e-9 度）

    ⚠ 不可改为单次近似偏移：那是本项目最初偏移 2 公里的根因。
    """
    wlon, wlat = lon, lat
    for _ in range(6):
        glon, glat = wgs84_to_gcj02(wlon, wlat)
        wlon -= (glon - lon)
        wlat -= (glat - lat)
    return wlon, wlat


_X_PI = math.pi * 3000.0 / 180.0


def bd09_to_gcj02(lng, lat):
    """BD-09 -> GCJ-02"""
    x = lng - 0.0065
    y = lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * _X_PI)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * _X_PI)
    return z * math.cos(theta), z * math.sin(theta)


def gcj02_to_bd09(lng, lat):
    """GCJ-02 -> BD-09"""
    z = math.sqrt(lng * lng + lat * lat) + 0.00002 * math.sin(lat * _X_PI)
    theta = math.atan2(lat, lng) + 0.000003 * math.cos(lng * _X_PI)
    return z * math.cos(theta) + 0.0065, z * math.sin(theta) + 0.006


def bd09_to_wgs84(lng, lat):
    """BD-09 -> WGS84（两级转换）"""
    g_lon, g_lat = bd09_to_gcj02(lng, lat)
    return gcj02_to_wgs84(g_lon, g_lat)


def wgs84_to_bd09(lon, lat):
    """WGS84 -> BD-09"""
    return gcj02_to_bd09(*wgs84_to_gcj02(lon, lat))


# 统一入口：任意坐标系 -> WGS84
def to_wgs84(lon, lat, from_crs):
    """按源坐标系转 WGS84。from_crs: WGS84 | GCJ02 | BD09 | CGCS2000"""
    c = (from_crs or "WGS84").upper().replace("-", "").replace("_", "")
    if c in ("WGS84", "WGS", "EPSG4326"):
        return lon, lat
    if c in ("GCJ02", "GCJ", "MARS"):
        return gcj02_to_wgs84(lon, lat)
    if c in ("BD09", "BD", "BAIDU"):
        return bd09_to_wgs84(lon, lat)
    if c in ("CGCS2000", "CGCS", "EPSG4490"):
        return cgcs2000_to_wgs84(lon, lat)
    raise ValueError(f"未知源坐标系：{from_crs}")


def from_wgs84(lon, lat, to_crs):
    """WGS84 转为目标交付坐标系"""
    c = (to_crs or "WGS84").upper().replace("-", "").replace("_", "")
    if c in ("WGS84", "WGS", "EPSG4326"):
        return lon, lat
    if c in ("GCJ02", "GCJ", "MARS"):
        return wgs84_to_gcj02(lon, lat)
    if c in ("BD09", "BD", "BAIDU"):
        return wgs84_to_bd09(lon, lat)
    raise ValueError(f"未知目标坐标系：{to_crs}")


# ---------------------------------------------------------------- 几何计算

def haversine_m(lon1, lat1, lon2, lat2):
    """两点球面距离（米）"""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def point_in_polygon(lon, lat, polygon):
    """射线法：判断点是否落在多边形内。polygon 为 [(lon,lat), ...]"""
    if not polygon or len(polygon) < 3:
        return None
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def in_bbox(lon, lat, bbox):
    """bbox = (min_lon, min_lat, max_lon, max_lat)"""
    return bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]


# ---------------------------------------------------------------- HTTP 层

class QuotaExceeded(Exception):
    """配额 / 限流异常。

    daily=True  → **日配额**用尽（百度 302、腾讯 121、高德日限额）。
                  这类错误重试到明天才有意义，必须**一次熔断到次日 0 点**。
    daily=False → 临时限流（QPS/并发抖动），短冷却后重试即可。

    ⚠ 为什么要分（2026-10-21 实测）：不分的话，「天配额超限」会被当成普通抖动，
    每 60 秒重新试探一轮——每次白烧 3 次请求、干等 60 秒，
    133 条样本里百度因此反复熔断十几次，全程几乎没贡献数据。
    """

    def __init__(self, msg="", daily=False):
        super().__init__(msg)
        self.daily = bool(daily)


class PermissionError_(Exception):
    """权限/Key 类型错误，应终止并提示用户"""


#: 证书校验保持默认开启（Key 与用户地址都在 URL/正文里，不做明文降级）。
#: 实际请求由 config.http_opener() 统一出网（含失效代理自动跳过），
#: 这里的 _CTX 仅保留给第三方库/特殊场景手工调用。
_CTX = ssl.create_default_context()

#: 最近一次网络失败原因（诊断用；调用方可通过它区分「查无结果」与「没查成」）
_LAST_NET_ERROR = ""


def _get(url, timeout=None, no_ua=True):
    """HTTP GET

    ⚠ no_ua 默认 True：天地图服务端 Key 绝不能带 Mozilla UA，否则 301013 权限类型错误。
       民政部接口相反，需带 Chrome UA（见 regions.py）。

    网络走 config.http_opener()：会自动跳过「端口不监听」的失效代理。
    """
    cfg = load_config()
    timeout = timeout or cfg["runtime"]["timeout"]
    headers = {} if no_ua else {"User-Agent": "python-urllib/3"}
    req = urllib.request.Request(url, headers=headers)
    with http_opener().open(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _get_retry(url, retries=None, **kw):
    """带退避重试的 GET。权限类错误不重试。"""
    cfg = load_config()
    retries = retries or cfg["runtime"]["retries"]
    base = cfg["runtime"]["backoff_base"]
    last_err = None
    for i in range(retries):
        try:
            return _get(url, **kw)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "ignore")[:200]
            except Exception:
                pass
            if e.code in (401, 403) and "301013" in body:
                raise PermissionError_("301013 权限类型错误：Key 类型不符或带了浏览器 UA") from e
            # 4xx 是**永久错**（参数写错 / 权限不足），重试一万次也是同一个错。
            # 实测：天地图逆地理 URL 参数写错报 400，每次白烧 6 s 退避休眠，
            # 44 点共计 264 s——占整套 414 s 的 64%。不重试，让它立刻暴露。
            if 400 <= e.code < 500:
                raise
            last_err = f"HTTP {e.code} {body}"
            if i == retries - 1:
                break
            time.sleep(base * (i + 1) + random.random())
        except Exception as e:
            last_err = str(e)
            if i == retries - 1:
                break
            time.sleep(base * (i + 1) + random.random())
    # 不再静默：网络层失败必须冒泡到 stderr，避免把「没查成」写成「未命中」。
    global _LAST_NET_ERROR
    _LAST_NET_ERROR = last_err or "unknown"
    warnings.warn(f"网络请求失败（已重试 {retries} 次）：{last_err}",
                  RuntimeWarning, stacklevel=2)
    return json.dumps({"net_error": last_err})


class RateLimiter:
    """QPS 限速器：最小间隔 + 随机抖动，防并发触发平台限流"""

    def __init__(self, min_interval=0.125):
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self):
        now = time.time()
        d = now - self._last
        if d < self.min_interval:
            time.sleep(self.min_interval - d + random.random() * 0.05)
        self._last = time.time()


_LIMITERS = {}


def limiter(source):
    """按源取限速器（QPS 反比即最小间隔）"""
    if source not in _LIMITERS:
        cfg = load_config()
        qps = cfg["runtime"]["qps"].get(source, 5)
        _LIMITERS[source] = RateLimiter(1.0 / max(qps, 1))
    return _LIMITERS[source]


# ------------------------------------------------- 源健康 · 熔断（"稳"的支柱）

#: 同一家连续失败几次就熔断。设成 1 太敏感（偶发 5xx 也会断），设太大则白烧配额。
_BREAK_THRESHOLD = 3
#: 熔断后冷却秒数。冷却期内该源的请求直接跳过，不占用配额也不拖慢整体。
_BREAK_COOLDOWN = 60.0

#: 每源并发**硬上限**（用户 2026-10-21 明确）：正常每源 5（四源全局 20），
#: 最高不超过每源 10（全局 40）。超过这个数会直接撞图商限流乃至封 Key，
#: 所以在代码里夹死，调用方传多大都会被压回上限。
_MAX_PER_SRC = 10
#: 每源并发默认起步值
_DEF_PER_SRC = 5

_BREAKERS = {}


def _breaker(source):
    if source not in _BREAKERS:
        _BREAKERS[source] = {"fails": 0, "until": 0.0}
    return _BREAKERS[source]


def _next_midnight_ts() -> float:
    """次日 0 点的时间戳（本地时区）。日配额熔断用它做恢复时刻。"""
    import datetime as _dt
    n = _dt.datetime.now() + _dt.timedelta(days=1)
    return _dt.datetime(n.year, n.month, n.day).timestamp()


def mark_source_fail(source, daily: bool = False):
    """图商返回配额/权限类错误时调用。连续失败达阈值 → 熔断该源，返回 True。

    daily=True（**日配额**用尽）→ 直接熔断到次日 0 点，不再周期性试探；
    daily=False（临时限流）    → 常规 _BREAK_COOLDOWN 短冷却。
    """
    b = _breaker(source)
    if daily:
        b["until"] = max(b["until"], _next_midnight_ts())
        b["fails"] = 0
        b["daily"] = True
        return True
    b["fails"] += 1
    if b["fails"] >= _BREAK_THRESHOLD:
        b["until"] = time.time() + _BREAK_COOLDOWN
        b["fails"] = 0
        return True
    return False


def mark_source_ok(source):
    """成功一次即清零连续失败计数（只惩罚连续错误，不惩罚偶发抖动）。"""
    _breaker(source)["fails"] = 0


def source_available(source):
    """该源当前是否可用（未处于熔断冷却期）。"""
    return time.time() >= _breaker(source)["until"]


def breaker_state():
    """各源熔断状态快照，用于报告与自检。"""
    return {s: (0.0 if time.time() >= v["until"]
                else round(v["until"] - time.time(), 1))
            for s, v in _BREAKERS.items()}


def _fmt_dur(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec} 秒"
    if sec < 3600:
        return f"{sec // 60} 分 {sec % 60} 秒"
    h, r = divmod(sec, 3600)
    return f"{h} 小时 {r // 60} 分"


#: 默认可用源（腾讯今日 status=121 日配额耗尽，运行时会被熔断自动摘除）
_ALL_SOURCES = ("tianditu", "amap", "baidu", "qq")


def precheck_quota(n_points: int, per_point: int = 8,
                   srcs: tuple | None = None) -> dict:
    """配额预检（"稳"的第 4 点）：开工前估算请求量与工期，避免跑到一半崩。

    n_points   待采集条数
    per_point  每点平均请求次数。当前完整路径约 36，优化目标是 6~8。
               这个数**必须用实测标定**，不能拍脑袋——差 5 倍工期就差 5 倍。
    """
    srcs = srcs or _ALL_SOURCES
    total = n_points * per_point
    cfg = load_config()
    qps_each = {s: cfg["runtime"]["qps"].get(s, 5) for s in srcs}
    qps_sum = sum(qps_each.values())
    eta = total / max(qps_sum, 1)
    return {"条数": n_points, "每点请求数": per_point, "总请求数": total,
            "可用源": list(srcs), "每源分摊请求数": round(total / max(len(srcs), 1)),
            "各家QPS上限": qps_each, "合计QPS": qps_sum,
            "理论工期": _fmt_dur(eta),
            "提示": ("总请求数已按 %d 次/点估算；实际次数请在小规模标定后回填"
                     % per_point)}


# ---------------------------------------------------------------- 天地图

def td_geocode(keyword, city=""):
    """天地图地理编码 -> (wgs84_lon, wgs84_lat, score, level, name) | None

    city 参数用于拼接查询词（天地图 geocoder 不支持城市限定参数，靠关键词构造）
    """
    limiter("tianditu").wait()
    kw = f"{city}{keyword}" if city else keyword
    ds = urllib.parse.quote(json.dumps({"keyWord": kw}, ensure_ascii=False))
    url = f"https://api.tianditu.gov.cn/geocoder?ds={ds}&tk={get_key('tianditu')}"
    try:
        d = json.loads(_get_retry(url))
    except (PermissionError_, KeyError):
        raise
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    code = d.get("code")
    if code in (301001, 301002, 301003, 301004, 301005):
        raise QuotaExceeded(f"天地图配额/并发限制：{code} {d.get('msg', '')}")
    if code not in (0, None):
        return None
    loc = d.get("location")
    if not isinstance(loc, dict) or not loc.get("lon"):
        return None
    lon, lat = cgcs2000_to_wgs84(float(loc["lon"]), float(loc["lat"]))
    return (round(lon, 6), round(lat, 6), int(loc.get("score") or 0),
            loc.get("level") or "", kw)


def td_search(keyword: str, bbox: str = "-180,-90,180,90", count: int = 10) -> list:
    """天地图 v2/search 多结果检索 -> list[dict]（已转 WGS84）。

    R7 基础设施（2026-10-20 立）：`td_geocode` 只返回排第一的那一条，
    而天地图一个关键词常返回多个同名本体点（实测「巷碌村」2 个、「花围村」7 个），
    取第一 = 系统性选中离群点。批量采自然村必须走本接口全量展开。

    坑：v2/search 的坐标字段是 **`lonlat` 字符串**「116.1,39.9」，
    不是 geocoder 那样的 lon/lat 两个字段。写成 p["lon"] 会 KeyError
    并被引擎静默吞成"无结果"——本次正是这个坑导致天地图整源假性失效。

    返回 [{"name","addr","lon","lat","level","source",...}]
    """
    limiter("tianditu").wait()
    post = {"keyWord": keyword, "level": 15, "mapBound": bbox,
            "queryType": 7, "count": count, "start": 0}
    ds = urllib.parse.quote(json.dumps(post, ensure_ascii=False))
    url = (f"https://api.tianditu.gov.cn/v2/search?type=query&postStr={ds}"
           f"&tk={get_key('tianditu')}")
    try:
        d = json.loads(_get_retry(url))
    except (PermissionError_, KeyError):
        raise
    except Exception:
        return []
    if not isinstance(d, dict):
        return []
    code = d.get("code") or (d.get("status") or {}).get("infocode")
    if code in (301001, 301002, 301003, 301004, 301005):
        raise QuotaExceeded(f"天地图配额/并发限制：{code} {d.get('msg', '')}")
    out = []
    for p in d.get("pois") or []:
        ll = p.get("lonlat") or ""
        if not ll:
            ll = f"{p.get('lon','')},{p.get('lat','')}"
        try:
            a, b = ll.split(",")
            lon, lat = cgcs2000_to_wgs84(float(a), float(b))
        except Exception:
            continue
        out.append({"name": p.get("name") or "", "addr": p.get("address") or "",
                    "lon": round(lon, 6), "lat": round(lat, 6),
                    "level": p.get("poiType") or "", "source": "tianditu"})
    return out


# ---------------------------------------------------------------- 高德

_AMAP_QUOTA = ("10003", "10044")   # 日配额 -> 停机
_AMAP_QPS = ("10019", "10021")     # 限速 -> 等待重试
_AMAP_PERM = ("10001", "10002", "10022", "10023")


def _amap_call(url, qps_retries=4):
    limiter("amap").wait()
    last = None
    for attempt in range(qps_retries + 1):
        d = json.loads(_get_retry(url))
        if not isinstance(d, dict):
            return None
        code = d.get("infocode")
        if code in _AMAP_QPS:
            last = d
            time.sleep(2.5 + attempt * 2.0)
            continue
        if code in _AMAP_QUOTA:
            raise QuotaExceeded(f"高德日配额用尽：{code} {d.get('info')}",
                                daily=True)
        if code in _AMAP_PERM:
            print(f"[高德权限异常] {code} {d.get('info')}", flush=True)
            return None
        return d
    raise QuotaExceeded(f"高德 QPS 持续超限：{last.get('infocode') if last else '?'}")


def amap_geocode(address, city=""):
    """高德地理编码 -> (wgs84_lon, wgs84_lat, level, formatted) | None"""
    url = ("https://restapi.amap.com/v3/geocode/geo?"
           f"address={urllib.parse.quote(address)}&city={urllib.parse.quote(city)}"
           f"&key={get_key('amap')}")
    d = _amap_call(url)
    if not d or d.get("status") != "1" or not d.get("geocodes"):
        return None
    g = d["geocodes"][0]
    lon, lat = map(float, g["location"].split(","))
    wlon, wlat = gcj02_to_wgs84(lon, lat)
    return (round(wlon, 6), round(wlat, 6), g.get("level") or "", g.get("formatted_address") or "")


def amap_tips(keyword, city: str = "", count: int = 10) -> list:
    """高德**输入提示**（assistant/inputtips）——自然村采集的关键接口。

    ⚠ 它和 `amap_search`（place/text）**不是一回事**：
      place/text  只查 **POI 库**
      inputtips   走 **综合检索**（POI + 地名注记 + 行政区）
    自然村在地图上是**地名注记**，不在 POI 库里，所以：
      place     查不到  ┐
      inputtips 查得到  ┘  实测宾阳中华镇·沙江村：place 查无此村，
                          inputtips 第一条直接命中，距标准答案 0 m。

    在线地图的搜索框用的就是 inputtips 这一路——
    「我在在线地图上能看到，你的工具却查不到」的根源即在此（R21）。

    注意接口差异：返回的 `address` 有时是 list 有时是空，需兼容。
    """
    url = ("https://restapi.amap.com/v3/assistant/inputtips?keywords="
           f"{urllib.parse.quote(keyword)}"
           f"&city={urllib.parse.quote(city)}"
           f"&datatype=all&key={get_key('amap')}")
    limiter("amap").wait()
    try:
        d = json.loads(_get_retry(url))
    except (QuotaExceeded, PermissionError_):
        raise
    except Exception:
        return []
    if not isinstance(d, dict) or d.get("status") != "1":
        return []
    out = []
    for it in d.get("tips") or []:
        loc = (it.get("location") or "").split(",")
        if len(loc) != 2 or not loc[0] or not loc[1]:
            continue
        try:
            glon, glat = float(loc[0]), float(loc[1])
        except ValueError:
            continue
        wl, wt = gcj02_to_wgs84(glon, glat)
        ad = it.get("address")
        ad = " ".join(str(x) for x in ad if x) if isinstance(ad, list) else (ad or "")
        out.append({"name": it.get("name") or "", "address": ad,
                    "lon": round(wl, 6), "lat": round(wt, 6),
                    "source": "amap_tips", "poi_id": it.get("id") or ""})
    return out[:count]


def amap_regeo(lon_wgs84, lat_wgs84, radius=1000):
    """高德逆地理编码（入参 WGS84）：坐标 → 省/市/区县/乡镇 + 格式化地址。

    用途：
      1. 单点采集后回填乡镇归属（正向 geocode 往往不带乡镇）
      2. 坐标系侦测的「双假设反向验证」：把点按 WGS84 / GCJ-02 两种假设
         反查行政区，匹配率高者即真实坐标系
    """
    glon, glat = wgs84_to_gcj02(lon_wgs84, lat_wgs84)
    url = ("https://restapi.amap.com/v3/geocode/regeo?"
           f"location={glon:.6f},{glat:.6f}&extensions=base"
           f"&radius={radius}&key={get_key('amap')}")
    d = _amap_call(url)
    if not d or d.get("status") != "1":
        return {}
    rg = d.get("regeocode") or {}
    ac = rg.get("addressComponent") or {}
    return {
        "地址": rg.get("formatted_address") or "",
        "省": ac.get("province") or "",
        "市": ac.get("city") or ac.get("province") or "",
        "区县": ac.get("district") or "",
        "乡镇": ac.get("township") or "",
        "区县码": ac.get("adcode") or "",
        "乡镇码": ac.get("towncode") or "",
        "来源": "amap",
    }


def amap_poi(keyword, city=""):
    """高德 POI 关键词搜索 -> (wgs84_lon, wgs84_lat, name, address) | None"""
    url = ("https://restapi.amap.com/v3/place/text?"
           f"keywords={urllib.parse.quote(keyword)}&city={urllib.parse.quote(city)}"
           f"&citylimit=true&offset=20&page=1&key={get_key('amap')}")
    d = _amap_call(url)
    if not d or d.get("status") != "1" or not d.get("pois"):
        return None
    p = d["pois"][0]
    lon, lat = map(float, p["location"].split(","))
    wlon, wlat = gcj02_to_wgs84(lon, lat)
    return (round(wlon, 6), round(wlat, 6), p.get("name") or "", p.get("address") or "")


def amap_search(keyword: str, city: str = "", count: int = 20,
                city_limit: bool = False) -> list:
    """高德 place/text 多结果检索 -> list[dict]（已转 WGS84）。

    R7 基础设施：`amap_poi` 同样只取第一条。见 `td_search` 说明。
    """
    url = ("https://restapi.amap.com/v3/place/text?"
           f"keywords={urllib.parse.quote(keyword)}&city={urllib.parse.quote(city)}"
           f"{'&citylimit=true' if city_limit else ''}"
           f"&offset={count}&page=1&key={get_key('amap')}")
    d = _amap_call(url)
    if not d or d.get("status") != "1":
        return []
    out = []
    for p in d.get("pois") or []:
        try:
            lon, lat = map(float, p["location"].split(","))
        except Exception:
            continue
        wl, wt = gcj02_to_wgs84(lon, lat)
        out.append({"name": p.get("name") or "", "addr": p.get("address") or "",
                    "lon": round(wl, 6), "lat": round(wt, 6),
                    "level": p.get("type") or "", "source": "amap"})
    return out


def amap_around(lon_wgs84, lat_wgs84, radius_m, keyword="", types="", page=1, offset=25):
    """高德周边搜索（入参 WGS84，内部转 GCJ-02）-> list[dict]"""
    glon, glat = wgs84_to_gcj02(lon_wgs84, lat_wgs84)
    url = ("https://restapi.amap.com/v3/place/around?"
           f"location={glon:.6f},{glat:.6f}&radius={int(radius_m)}"
           f"&keywords={urllib.parse.quote(keyword)}&types={urllib.parse.quote(types)}"
           f"&offset={offset}&page={page}&key={get_key('amap')}")
    d = _amap_call(url)
    if not d or d.get("status") != "1":
        return []
    out = []
    for p in d.get("pois") or []:
        try:
            plon, plat = map(float, p["location"].split(","))
        except Exception:
            continue
        wl, wt = gcj02_to_wgs84(plon, plat)
        out.append({
            "name": p.get("name") or "",
            "address": p.get("address") if isinstance(p.get("address"), str) else "",
            "lon": round(wl, 6),
            "lat": round(wt, 6),
            "type": p.get("type") or "",
            "source": "amap",
            "poi_id": p.get("id") or "",
            "distance_m": int(haversine_m(lon_wgs84, lat_wgs84, wl, wt)),
        })
    return out


# ---------------------------------------------------------------- 百度

def _baidu_place_raw(query, region, city_limit=True):
    url = ("https://api.map.baidu.com/place/v2/search?"
           f"query={urllib.parse.quote(query)}&region={urllib.parse.quote(region)}"
           f"{'&city_limit=true' if city_limit else ''}&output=json&ak={get_key('baidu')}")
    limiter("baidu").wait()
    d = json.loads(_get_retry(url))
    if not isinstance(d, dict):
        return None
    st = d.get("status")
    # 302 实测 msg="天配额超限，限制访问"（2026-10-21 亲测），属**日**配额，
    # 必须 daily=True 一次性熔断到次日；否则会每 60s 白烧一轮请求。
    if st in (302, 4):
        raise QuotaExceeded(f"百度天配额超限：status={st} {d.get('message', '')}",
                            daily=True)
    if st in (1, 2, 3):
        raise QuotaExceeded(f"百度配额/权限：status={st} {d.get('message', '')}")
    if st != 0:
        return []
    return d.get("results") or []


def baidu_place(query, region, expect_area=None, expect_town=None):
    """百度地点检索 -> (wgs84_lon, wgs84_lat, name, address) | None

    严格过滤：area 必须等于 expect_area（防外县同名），或地址含 expect_town。
    """
    for it in _baidu_place_raw(query, region) or []:
        loc = it.get("location") or {}
        if not loc.get("lng"):
            continue
        area = it.get("area") or ""
        addr = it.get("address") or ""
        if expect_area and area and area != expect_area:
            continue
        if expect_area and not area and expect_town and expect_town not in addr:
            continue
        lon, lat = bd09_to_wgs84(float(loc["lng"]), float(loc["lat"]))
        return (round(lon, 6), round(lat, 6), it.get("name") or "", addr)
    return None


def baidu_search(query, region, expect_area=None, expect_town=None,
                 count=20) -> list:
    """百度 place/v2/search 多结果检索 -> list[dict]（已转 WGS84，不自带过滤）。

    R7 基础设施：`baidu_place` 只取第一条且自带强过滤，看不到同名多点。
    本接口原样返回，过滤交给上层 R1/R5，保证"候选可见"。
    """
    out = []
    for it in (_baidu_place_raw(query, region, city_limit=True) or [])[:count]:
        loc = it.get("location") or {}
        if not loc.get("lng"):
            continue
        lon, lat = bd09_to_wgs84(float(loc["lng"]), float(loc["lat"]))
        out.append({"name": it.get("name") or "", "addr": it.get("address") or "",
                    "lon": round(lon, 6), "lat": round(lat, 6),
                    "level": it.get("area") or "", "source": "baidu"})
    return out


def baidu_geocode(address, city=""):
    """百度地理编码 -> (wgs84_lon, wgs84_lat, level) | None"""
    url = ("https://api.map.baidu.com/geocoding/v3/?"
           f"address={urllib.parse.quote(address)}&city={urllib.parse.quote(city)}"
           f"&output=json&ak={get_key('baidu')}")
    limiter("baidu").wait()
    d = json.loads(_get_retry(url))
    if not isinstance(d, dict):
        return None
    st = d.get("status")
    if st in (302, 4):
        raise QuotaExceeded(f"百度天配额超限：status={st}", daily=True)
    if st in (1, 2, 3):
        raise QuotaExceeded(f"百度配额/权限：status={st}")
    if st != 0:
        return None
    loc = (d.get("result") or {}).get("location") or {}
    if not loc.get("lng"):
        return None
    wlon, wlat = bd09_to_wgs84(float(loc["lng"]), float(loc["lat"]))
    return (round(wlon, 6), round(wlat, 6), (d.get("result") or {}).get("level") or "")


# ---------------------------------------------------------------- 腾讯

def qq_place(keyword, region, page_size=20):
    """腾讯地点搜索 -> list[dict]"""
    url = ("https://apis.map.qq.com/ws/place/v1/search?"
           f"keyword={urllib.parse.quote(keyword)}"
           f"&boundary={urllib.parse.quote('region(' + region + ',0)')}"
           f"&page_size={page_size}&key={get_key('qq')}")
    limiter("qq").wait()
    d = json.loads(_get_retry(url))
    if not isinstance(d, dict):
        return []
    st = d.get("status")
    # 121 实测 msg="此key每日调用量已达到上限" → 日配额；190 = 权限问题
    if st in (120, 121):
        raise QuotaExceeded(f"腾讯天配额超限：status={st} {d.get('message', '')}",
                            daily=True)
    if st in (190,):
        raise QuotaExceeded(f"腾讯权限：status={st} {d.get('message', '')}")
    if st != 0:
        return []
    out = []
    for it in d.get("data") or []:
        loc = it.get("location") or {}
        if not loc.get("lng"):
            continue
        wl, wt = gcj02_to_wgs84(float(loc["lng"]), float(loc["lat"]))
        out.append({
            "name": it.get("title") or "",
            "address": it.get("address") or "",
            "lon": round(wl, 6),
            "lat": round(wt, 6),
            "source": "qq",
            "poi_id": it.get("id") or "",
        })
    return out


# ---------------------------------------------------------------- 统一降级链

FALLBACK_ORDER = ("tianditu", "amap", "baidu", "qq")

#: 按任务类型区分的源优先级（2026-10-20 用户实测校准）：
#:   POI/小区/学校等兴趣点 —— 高德/百度明显更准（腾讯连"那考河小学"都搜不到，
#:   天地图 POI 库偏官方地名），故高德、百度优先；
#:   政府驻地/行政区划 —— 天地图是官方平台、权威地名库优先；
#:   门牌地址 —— 高德/百度地理编码强，天地图兜底。
TASK_ORDER = {
    "poi": ("amap", "baidu", "tianditu", "qq"),
    "addr": ("amap", "baidu", "tianditu", "qq"),
    "gov": ("tianditu", "amap", "baidu", "qq"),
}


def query_point(keyword, city="", area="", town="", sources=None, task="gov"):
    """按降级链查询单点坐标。

    task: poi=兴趣点 / addr=门牌地址 / gov=政府驻地与行政区划（默认）
    返回 dict: {lon, lat, source, detail, tried:[...]} 或 None
    """
    from config import available_sources

    order = sources or [s for s in (TASK_ORDER.get(task) or FALLBACK_ORDER)
                        if s in available_sources()]
    if not order:
        raise KeyError("没有任何已配置 Key 的数据源，无法联网查询。")

    tried = []
    for src in order:
        tried.append(src)
        try:
            if src == "tianditu":
                r = td_geocode(keyword, city=city)
                if r and r[2] >= 60:
                    return {"lon": r[0], "lat": r[1], "source": "tianditu",
                            "detail": f"score={r[2]} level={r[3]}", "tried": tried}
            elif src == "amap":
                r = amap_geocode(f"{city}{area}{keyword}", city=city)
                if not r:
                    r = amap_poi(keyword, city=city)
                    if r:
                        return {"lon": r[0], "lat": r[1], "source": "amap",
                                "detail": f"poi={r[2]} {r[3]}", "tried": tried}
                else:
                    return {"lon": r[0], "lat": r[1], "source": "amap",
                            "detail": f"level={r[2]} {r[3]}", "tried": tried}
            elif src == "baidu":
                r = baidu_place(keyword, area or city, expect_area=area or None,
                                expect_town=town or None)
                if r:
                    return {"lon": r[0], "lat": r[1], "source": "baidu",
                            "detail": f"{r[2]} {r[3]}", "tried": tried}
            elif src == "qq":
                rs = qq_place(keyword, area or city)
                if rs:
                    return {"lon": rs[0]["lon"], "lat": rs[0]["lat"], "source": "qq",
                            "detail": rs[0]["name"], "tried": tried}
        except QuotaExceeded as e:
            print(f"[降级] {src} 配额耗尽，切换下一源：{e}", flush=True)
            continue
        except PermissionError_ as e:
            print(f"[降级] {src} 权限异常：{e}", flush=True)
            continue
    return None


# ---------------------------------------------------------------- 多点候选仲裁

_NORM_RE = re.compile(r"[\s（）()·\-—，,。.]")


def _norm_txt(s: str) -> str:
    return _NORM_RE.sub("", s or "")


def _coverage(name: str, query: str) -> float:
    """名称覆盖率 = 与查询词重合字数 / 查询词长度。

    用覆盖率而不用相似度比例：全称会被长字符串稀释后反超短名，导致选错
    （实测：天地图"梧州市藤县平福乡巷蓬村兰秀组"比例值反超高德"…巷蓬村兰秀"）。
    """
    from difflib import SequenceMatcher
    a, q = _norm_txt(name), _norm_txt(query)
    if not a or not q:
        return 0.0
    m = sum(b.size for b in SequenceMatcher(None, a, q).get_matching_blocks())
    return m / len(q)


def select_point(rows: list, query: str = "", task: str = "poi",
                 natural: bool = False, cluster_m: int = 300) -> dict:
    """候选点仲裁 —— **不确定就停手，绝不替用户拍板**（2026-10-20 用户定案 A 方案）。

    参数
      rows      候选命中列表，每项需含 源 / lon / lat / 名称
      query     原始查询词（算名称覆盖率用）
      task      poi | addr | gov（决定簇内源优先级）
      natural   是否为自然村 / 村民小组

    停手判定（任一命中即 confirm_required=True、pick=None）：
      ① 自然村/组 —— 无官方编码坐标，且不排除同名多点（山上/山下）
      ② 最高两簇支持源数并列 —— 禁止用"名称最短/先命中先取"等任意规则破平
      ③ 无簇获得 ≥2 源支持，且候选不是唯一单条

    返回 dict：pick / clusters / candidates / confidence / confirm_required / reason
    """
    out = {"pick": None, "clusters": [], "candidates": [], "confidence": "无",
           "confirm_required": True, "reason": ""}
    if not rows:
        out["reason"] = "无任何源命中"
        return out

    order = {s: i for i, s in enumerate(TASK_ORDER.get(task, FALLBACK_ORDER))}

    # 1) 聚簇
    clusters = []
    for r in rows:
        for c in clusters:
            if haversine_m(c["lon"], c["lat"], r["lon"], r["lat"]) <= cluster_m:
                c["rows"].append(r)
                break
        else:
            clusters.append({"lon": r["lon"], "lat": r["lat"], "rows": [r]})
    for c in clusters:
        c["srcs"] = sorted({x["源"] for x in c["rows"]})
        c["n_src"] = len(c["srcs"])
        c["cov"] = max(_coverage(x.get("名称"), query) for x in c["rows"])
        c["names"] = list(dict.fromkeys(x.get("名称", "") for x in c["rows"]))[:3]

    ranked = sorted(clusters, key=lambda c: (-c["n_src"], -c["cov"]))
    best = ranked[0]
    second = ranked[1] if len(ranked) > 1 else None

    # 2) 停手判定
    reasons = []
    if natural:
        reasons.append("自然村/村民小组：无官方编码与坐标，且不排除同名多点（山上/山下），不可机器决断")
    if second is not None and second["n_src"] == best["n_src"]:
        reasons.append(f"最高两簇支持源数并列（{best['n_src']}:{second['n_src']}），"
                       f"禁止任意破平，须由用户判定")
    if best["n_src"] < 2 and not (len(clusters) == 1 and len(rows) == 1):
        reasons.append(f"最佳簇仅 {best['n_src']} 个源支持，证据不足以定选")

    # 3) 簇内代表行（即使触发停手也算出来，作为"首选参考"给用户看）
    rep = sorted(best["rows"],
                 key=lambda r: (order.get(r["源"], 9),
                                -_coverage(r.get("名称"), query),
                                len(_norm_txt(r.get("名称")))))[0]

    out["preferred"] = rep        # 首选参考（触发停手时仅为参考，不是结论）
    out["clusters"] = [{k: v for k, v in c.items() if k != "rows"} for c in ranked]
    out["candidates"] = [{
        "候选": f"#{i + 1}",
        "经度": round(c["lon"], 6), "纬度": round(c["lat"], 6),
        "支持源数": c["n_src"], "支持源": "/".join(c["srcs"]),
        "名称覆盖率": round(c["cov"], 2),
        "代表名称": (c["names"][0] if c["names"] else "")[:30],
        "距首选米": round(haversine_m(best["lon"], best["lat"], c["lon"], c["lat"])),
    } for i, c in enumerate(ranked)]

    if reasons:
        out["reason"] = "；".join(reasons)
        out["confidence"] = "待确认"
        out["pick"] = None
        return out

    out["pick"] = rep
    out["confirm_required"] = False
    out["confidence"] = "高（≥2 源吻合）" if best["n_src"] >= 2 else "中（单源命中）"
    return out


# ---------------------------------------------------------------- 自然村检索（R1/R2/R4/R5）

#: 易指向地物而非聚落的通名（单独命中不算混淆点，需叠加其它判据）
_GENERIC_TOKENS = ("山", "岭", "顶", "坳", "冲", "垌", "坪", "浸", "岩", "垌")

#: 判定为「命中自然村本体」的返回级别
_BODY_LEVELS = ("村庄", "社区村落", "自然村", "乡村", "村民委员会")


def natural_query_ladder(name: str, city: str = "", area: str = "",
                         town: str = "", village: str = "") -> list:
    """R4 检索词阶梯（2026-10-20 实测：写法决定成败）。

    裸名 → 乡镇+名 → 区县乡镇+名 → 行政村+名。
    实测：带「行政村名+组后缀」会让天地图/百度回退到行政村（兰秀偏 1.6 km），
    去掉后三源均命中（26/119/24 m）。

    返回去重后的检索词列表，由调用方逐级下探。
    """
    cands = []
    for s in (name,
              (town or "") + name,
              (area or "") + (town or "") + name,
              (village or "") + name):
        s = (s or "").strip()
        if s and s not in cands:
            cands.append(s)
    return cands


def is_body_hit(ret_name: str, token: str) -> bool:
    """R1 本体判定：返回名称必须含自然村本名 token。

    不含 → 判为上级回退（如"巷蓬村民委员会""退役军人服务站""巷蓬村"），
    **不给票、不进簇**。
    """
    return bool(token) and _norm_txt(token) in _norm_txt(ret_name or "")


def is_confusion_point(lon: float, lat: float, token: str,
                       village_center: tuple | None = None,
                       level: str = "",
                       same_name_count: int = 1) -> list:
    """R5 混淆点判据：命中任一即需第二源定向复核。

    ① 同名多点  ② 距行政村中心 >3 km  ③ 返回级别非村庄/社区村落
    ④ 名称含地物通名 **且** 已命中 ①②③ 之一（单命中不触发，避免误伤"岭脚村"这类真村名）
    """
    reasons = []
    if same_name_count > 1:
        reasons.append(f"同名多点（本批 {same_name_count} 个同名）")
    if village_center:
        d = haversine_m(lon, lat, village_center[0], village_center[1])
        if d > 3000:
            reasons.append(f"距行政村中心 {d:.0f} m（>3000 m）")
    if level and level not in _BODY_LEVELS:
        reasons.append(f"返回级别为「{level}」，非村庄/社区村落")
    if any(g in (token or "") for g in _GENERIC_TOKENS) and reasons:
        reasons.append(f"名称含地物通名（{token}），需排除山顶点/地物点")
    return reasons


def query_natural(name: str, city: str = "", area: str = "", town: str = "",
                  village: str = "", village_center: tuple | None = None,
                  same_name_count: int = 1, sources: list | None = None,
                  max_ladder: int = 4) -> dict:
    """按 R4 阶梯检索自然村，返回本体命中与混淆点判据。

    返回 dict：{"命中": bool, "lon", "lat", "来源", "检索词", "级别",
                "混淆原因": [...], "阶梯轨迹": [...], "候选": [...]}
    """
    from config import available_sources
    srcs = sources or [s for s in ("tianditu", "amap", "baidu") if s in available_sources()]
    trace = []

    for qi, q in enumerate(natural_query_ladder(name, city, area, town, village)[:max_ladder], 1):
        rows = []
        for src in srcs:
            try:
                if src == "tianditu":
                    r = td_geocode(q, city=city or town or area)
                    if r:
                        rows.append({"源": src, "名称": r[4], "级别": r[3],
                                     "lon": r[0], "lat": r[1], "detail": f"score={r[2]}"})
                elif src == "amap":
                    r = amap_geocode(q, city=city or town or area)
                    if r:
                        rows.append({"源": src, "名称": r[3], "级别": r[2],
                                     "lon": r[0], "lat": r[1], "detail": ""})
                elif src == "baidu":
                    r = baidu_place(q, area or city or town)
                    if r:
                        rows.append({"源": src, "名称": r[2], "级别": "村庄",
                                     "lon": r[0], "lat": r[1], "detail": r[3]})
            except Exception as e:      # noqa: BLE001 单源失败不中断阶梯
                trace.append({"阶梯": qi, "检索词": q, "源": src,
                              "结果": f"ERR {type(e).__name__}"})
                continue

        # R1：只保留命中本体的行
        body = [r for r in rows if is_body_hit(r["名称"], name)]
        dropped = [r for r in rows if r not in body]
        trace.append({"阶梯": qi, "检索词": q, "返回": len(rows),
                      "本体命中": len(body), "剔出回退": len(dropped)})

        if not body:
            continue
        verdict = select_point(body, query=name, task="poi")
        pick = verdict.get("pick")
        if pick is None:
            # 本体命中但内部并列（同名多点）→ 交给上层按 R5 处理
            if verdict["confirm_required"]:
                continue
        if pick is not None:
            reasons = is_confusion_point(pick["lon"], pick["lat"], name,
                                         village_center=village_center,
                                         level=pick.get("级别", ""),
                                         same_name_count=same_name_count)
            return {"命中": True, "lon": pick["lon"], "lat": pick["lat"],
                    "来源": pick["源"], "返回名称": pick.get("名称", ""),
                    "检索词": q, "阶梯": qi, "级别": pick.get("级别", ""),
                    "混淆原因": reasons, "阶梯轨迹": trace,
                    "候选": verdict.get("candidates", [])}

    return {"命中": False, "lon": None, "lat": None, "来源": "", "检索词": "",
            "混淆原因": [], "阶梯轨迹": trace, "候选": []}


# ------------------------------------------------- R7 单源多候选全量展开（2026-10-20）

def multi_search(src: str, keyword: str, city: str = "", area: str = "",
                 town: str = "", count: int = 20, bbox: str | None = None) -> list:
    """R7 统一多结果检索入口 -> list[dict]（统一字段 name/addr/lon/lat/level/source）。

    与 `query_point` 的分工：`query_point` 取"每源第一条"，适合政府驻地这类
    唯一性高的目标；自然村/同名地物必须走本接口，否则会系统性选中离群点。
    """
    if src == "tianditu":
        return td_search(keyword, bbox=bbox or "-180,-90,180,90", count=count)
    if src == "amap":
        return amap_search(keyword, city=city or area or town, count=count)
    if src == "baidu":
        return baidu_search(keyword, area or city or town, count=count)
    if src == "qq":
        rows = []
        try:
            rows = qq_place(keyword, area or city or town, page_size=count) or []
        except (QuotaExceeded, PermissionError_):
            raise
        except Exception:
            return []
        out = []
        for r in rows:
            out.append({"name": r.get("title") or r.get("name") or "",
                        "addr": r.get("address") or "",
                        "lon": r.get("lon"), "lat": r.get("lat"),
                        "level": r.get("category") or "", "source": "qq"})
        return [r for r in out if r["lon"] is not None]
    return []


def query_natural_multi(name: str, city: str = "", area: str = "", town: str = "",
                        village: str = "", village_center: tuple | None = None,
                        sources: list | None = None, max_ladder: int = 4,
                        cluster_m: int = 300, count: int = 20,
                        bbox: str | None = None,
                        expect_area: str = "", expect_town: str = "") -> dict:
    """R7 全源 × 全阶梯 × 全候选的自然村查询（**不确定即停手**）。

    与 `query_natural` 的区别：后者每源每阶梯只取第一条。实测「巷碌村」天地图
    单关键词就返回 2 个本体点（5733 m / 776 m），取第一 = 选中离群点。

    返回 dict：
      {"本体候选": [...], "回退候选": [...], "簇": [...], "推荐": {...}|None,
       "confirm_required": bool, "停手原因": [...], "阶梯轨迹": [...]}

    R7 停手判据（命中任一即 confirm_required=True，绝不自动拍板）：
      ① 存在 ≥2 个本体簇（同名多点，无论跨源票数多少）
      ② 最高两簇的支持源数并列
      ③ 唯一本体簇但距行政村中心 >3 km
    """
    from config import available_sources
    srcs = sources or [s for s in ("tianditu", "amap", "baidu", "qq")
                       if s in available_sources()]
    token = name.replace("村", "").replace("组", "")

    body_rows, drop_rows, trace = [], [], []
    for qi, q in enumerate(natural_query_ladder(name, city, area, town, village)[:max_ladder], 1):
        n_raw = 0
        for src in srcs:
            try:
                rows = multi_search(src, q, city=city, area=area, town=town,
                                    count=count, bbox=bbox)
            except QuotaExceeded as e:
                trace.append({"阶梯": qi, "检索词": q, "源": src, "结果": f"配额 {e}"})
                continue
            except Exception as e:                       # noqa: BLE001
                trace.append({"阶梯": qi, "检索词": q, "源": src,
                              "结果": f"ERR {type(e).__name__}"})
                continue
            n_raw += len(rows)
            for r in rows:
                r = dict(r)
                r["检索词"] = q
                r["阶梯"] = qi
                r["距村委_m"] = (round(haversine_m(r["lon"], r["lat"],
                                                   village_center[0], village_center[1]))
                                 if village_center else None)
                if is_body_hit(r["name"], token):
                    body_rows.append(r)
                else:
                    drop_rows.append(r)
        trace.append({"阶梯": qi, "检索词": q, "原始返回": n_raw,
                      "本体累计": len(body_rows)})

    # 行政区地址过滤：天地图用全国 bbox 时会混进广东/湖南等同名村，必须按区县硬过滤。
    # 乡镇 **不参与过滤** —— 百度的 address 字段常常只到"梧州市藤县"，带不上乡镇；
    # "地址信息缺失"不等于"地址不匹配"，按乡镇硬过滤会把百度整源误剔（实测花围村
    # 因此从 2 源掉到 1 源）。乡镇只用于检索词构造与 R5 混淆判据。
    if expect_area:
        filtered = [r for r in body_rows
                    if expect_area in f"{r.get('addr','')}{r.get('name','')}"]
        if filtered:                       # 过滤后为空说明地址字段不全，不过滤
            body_rows = filtered

    # 聚簇
    clusters, seen = [], []
    for r in body_rows:
        placed = False
        for c in clusters:
            if haversine_m(r["lon"], r["lat"], c[0]["lon"], c[0]["lat"]) <= cluster_m:
                c.append(r)
                placed = True
                break
        if not placed:
            clusters.append([r])
    clusters.sort(key=lambda c: (-len({x["source"] for x in c}), -len(c)))

    cl_out = []
    for i, c in enumerate(clusters, 1):
        ss = sorted({x["source"] for x in c})
        cl_out.append({
            "簇号": i, "lon": c[0]["lon"], "lat": c[0]["lat"],
            "源": ss, "源数": len(ss), "条目数": len(c),
            "距村委_m": c[0]["距村委_m"],
            "代表名": max((x["name"] for x in c), key=len),
            "样本地址": c[0].get("addr", ""),
        })

    # R7 停手判定
    reasons = []
    if len(clusters) >= 2:
        reasons.append(f"同名多点：{len(clusters)} 个本体簇"
                       + "；".join(f"（簇{c['簇号']} {c['距村委_m']} m/{c['源数']}源）"
                                   for c in cl_out[:3]))
    elif len(clusters) == 1 and len(cl_out[0]["源"]) < 2:
        reasons.append(f"仅 {cl_out[0]['源']} 单源命中，无交叉验证")
    if len(clusters) >= 2 and cl_out[0]["源数"] == cl_out[1]["源数"]:
        reasons.append(f"最高两簇支持源数并列（各 {cl_out[0]['源数']} 源），无法由票数破平")
    if len(clusters) == 1 and village_center and (cl_out[0]["距村委_m"] or 0) > 3000:
        reasons.append(f"唯一本体簇距行政村中心 {cl_out[0]['距村委_m']} m（>3000 m）")

    # 参考推荐：多簇时取"距行政村中心最近"的簇，仅作人工确认起点，不当结论
    rec = None
    if cl_out:
        best = min(cl_out, key=lambda c: (c["距村委_m"] if c["距村委_m"] is not None else 9e9))
        rec = {"lon": best["lon"], "lat": best["lat"], "簇号": best["簇号"],
               "依据": "距行政村中心最近（仅供参考，非自动结论）" if len(cl_out) > 1
                       else "唯一本体簇"}

    return {"本体候选": body_rows, "回退候选": drop_rows, "簇": cl_out,
            "推荐": rec, "confirm_required": bool(reasons),
            "停手原因": reasons, "阶梯轨迹": trace}


# ================================================================ 丙方案（2026-10-20）
#  用户定案五条：
#   ① **必填满** —— 用户给的样本点全部要出数据，不许找不到就 50% 收工
#   ② 一个源没有就换下一个，**四个源全部试过**才算真的没有
#   ③ 检索词**切分降级**：「藤县平福乡巷蓬村兰秀组」→ 切出「兰秀」再查
#   ④ 拿到坐标必须**逆向验真**：反查该点是否真归属藤县/平福乡/巷蓬村
#   ⑤ 存疑只做备注，**不裸改图商经纬度**，错了也是图商的错，标来源即可

# ⚠ 只保留**真正的行政/聚落后缀**（兰秀组 → 兰秀）。
# 曾把「冲/垌/屋」也列进来，结果把地名本身切坏了：
#   大冲 → 自然村「大」+ 后缀「冲」→ 裸名阶梯拿「大」去搜，直接毁掉查询；
#   庙垌 → 「庙」；谢李屋 → 「谢李」。
# 「冲」在两广地名里是实质组成部分（大冲＝大山间的冲），不是后缀。
_ADDR_SUFFIX = ("村民小组", "小组", "组", "屯", "队")
# 切后缀的底线：主体名至少 2 字，防「X冲/XX屋」这类被切成一个字
_SUFFIX_MIN_STEM = 2
_TOWN_TAIL = ("街道办事处", "街道", "镇", "乡", "苏木", "民族乡", "管委会")
_VILLAGE_TAIL = ("村民委员会", "村委会", "村", "社区居民委员会", "社区")


def split_address_parts(raw: str, area: str = "", town: str = "",
                        village: str = "") -> dict:
    """把「藤县平福乡巷蓬村兰秀组」切成 区县/乡镇/行政村/自然村/后缀。

    切分是**检索词降级**的前提（用户 2026-10-20 明确要求）：
    全串查不到 → 切出「兰秀」裸名再查 → 拿到坐标后逆向验真归属。

    返回 {"原串","区县","乡镇","行政村","自然村","后缀","剩余"}
    """
    s = _norm_txt(raw or "").strip()
    p = {"原串": s or "", "区县": area or "", "乡镇": town or "",
         "行政村": village or "", "自然村": "", "后缀": "", "剩余": ""}

    # 1) 剥离调用方已给的行政区（含全称尾巴，如「平福乡」/「巷蓬村」）
    body = s
    for key, tails in (("区县", ()), ("乡镇", _TOWN_TAIL), ("行政村", _VILLAGE_TAIL)):
        given = p[key]
        if not given:
            continue
        if body.startswith(given):
            body = body[len(given):]
        elif given in body:
            body = body.replace(given, "", 1)
        else:
            # 用户给的是「平福」省了「乡」，或「巷蓬」省了「村」，按最长前缀匹配
            for t in tails:
                if not given.endswith(t) and (given + t) in body:
                    body = body.replace(given + t, "", 1)
                    break
    p["剩余"] = body

    # 2) 剩余串按 **区县 → 乡镇 → 行政村** 顺序切。
    #    顺序反了会串味：先切「村」会把「平福乡巷蓬村」整段吞进村名（实测踩中，
    #    乡镇被切成"藤县平福乡"）。切法是「尾巴词往前取最多 3 字」而非从串首切，
    #    否则「梧州市藤县…」会切出「梧州市藤县」这种跨两级的东西。
    def _cut(s, tails, max_pre=3, max_len=6):
        for tail in tails:
            i = s.find(tail)
            if i >= 0:
                start = max(0, i - max_pre)
                seg = s[start:i + len(tail)]
                # 段首若残留更高级行政区（"州市藤县"），截到那个尾巴之后
                for hi in ("省", "市", "区", "旗"):
                    j = seg.find(hi)
                    if 0 <= j < len(seg) - 1:
                        seg = seg[j + 1:]
                if len(seg) <= max_len:
                    return seg, s[i + len(tail):]
        return "", s

    if not p["区县"]:
        p["区县"], body = _cut(body, ("县", "区", "旗", "市"))
    if not p["乡镇"]:
        p["乡镇"], body = _cut(body, _TOWN_TAIL)
    # 孤立的「兰秀村」本身就是自然村，不能再切成行政村（实测会把自然村也变成"兰秀村"）。
    # 只有前面还挂着别的内容（长度 >4）时，才认它是行政村名。
    if not p["行政村"] and len(body) > 4:
        p["行政村"], body = _cut(body, ("村民委员会", "村委会", "村", "社区"))

    # 3) 切自然村后缀（组/屯/队/村民小组…）
    #    底线：主体名至少留 2 字。切完只剩一个字的（「大冲」→「大」）宁可不切。
    for suf in _ADDR_SUFFIX:
        if body.endswith(suf) and len(body) - len(suf) >= _SUFFIX_MIN_STEM:
            p["后缀"] = suf
            body = body[: -len(suf)]
            break
    p["自然村"] = body or _norm_txt(raw or "")
    return p


def full_ladder(raw: str, city: str = "", area: str = "", town: str = "",
                village: str = "", natural: str = "") -> list:
    """由原始串生成检索词阶梯，**按命中概率从高到低排列**。

    顺序依据 R4 实测：裸名 + 乡镇前缀最准（兰秀 26/119/24 m）；
    带「行政村名 + 组后缀」会让天地图/百度**回退到行政村**（偏 1.6 km），
    故全串排在最后，且靠 R1 本体判定兜底（回退结果会被剔出，不给票）。

    保留全串与切分版本，是为了满足用户「切开来查」的要求：
    万一前几级都没命中，至少还有原始写法可用，凑满四个源 × 全部阶梯。
    """
    p = split_address_parts(raw, area, town, village)
    nat = natural or p["自然村"] or _norm_txt(raw)
    a, t, v = p["区县"] or area, p["乡镇"] or town, p["行政村"] or village
    out = []

    def add(s):
        s = (s or "").strip()
        if s and s not in out:
            out.append(s)

    # L1 村委会：若这个名字是**行政村**本身（在统计局村级名录里，或以「村」结尾），
    # 「村委会」实体通常比自然村标注更精确、票数更多。
    # 实测容县自良镇·中平村：裸查「中平村」偏 558 m，
    #   改查「中平村委会」后百度直接给出 0 m 的精确点（高德 44 m）。
    # ⚠ 副作用：天地图会把「村委会」搜到山西/湖南去（mapBound 无效），
    #   靠空间闸门 + R1 本体判定把这批外省结果剔掉。
    # 直接**原样拼接**，不许削字：曾把「新村」削成「新」去查「新村委会」。
    # 名字是用户给的，任何自作聪明的改写都是在猜；猜错了就是系统性偏差。
    # ⛔ R24 曾在此处改过「村委会去通名」，已回退（2026-10-21 实测有害）：
    #    想法是"三军村村委会"通名重复，应改成"三军村委会"；
    #    实测三军村没修好（1011 m 不变），却把
    #    新村 52 m → 36 km（"新村"去通名变"新"，又踩回 R16 的坑）、
    #    中平村 38 m → 773 m 打崩了。
    #    教训：**村名里的"村"不一定是通名**（"新村"的"新"是专名、"村"也是名字的一部分），
    #    机械去尾等于削字，R16 早就立过这条规矩，这次是明知故犯。
    #    村委会阶梯维持**原样拼接**。
    for suf in ("村委会", "村民委员会"):
        add((t or "") + nat + suf)
    add(nat + "村委会")

    add((t or "") + nat)                    # 乡镇 + 自然村（R4 实测最准）
    add((a or "") + (t or "") + nat)        # 区县 + 乡镇 + 自然村
    add(nat)                                # 裸名
    add((v or "") + nat)                    # 行政村 + 自然村（可能回退，靠 R1 剔）
    add((a or "") + (t or "") + (v or "") + nat + p["后缀"])  # 全串（含后缀）
    add(_norm_txt(raw))                     # 原始串
    return out


# ------------------------------------------------- 黄金样本库（用户实测值入库）

#: 用户实测/确认过的坐标，是**唯一独立于图商的外部锚点**。
#: 用户核一次 → 永久入库 → 下次直接在库里命中，不必再赌图商。
_GOLDEN_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "golden_natural.csv")
_GOLDEN_FIELDS = ["省", "市", "区县", "乡镇", "行政村", "自然村",
                  "经度_WGS84", "纬度_WGS84", "GCJ02经度", "GCJ02纬度",
                  "BD09经度", "BD09纬度", "来源", "确认日期", "备注"]


def load_golden(path: str | Path | None = None) -> list:
    """读黄金样本库；文件不存在返回空列表（不报错，功能可选）。"""
    p = path or _GOLDEN_PATH
    if not os.path.exists(p):
        return []
    import csv
    for enc in ("utf-8-sig", "gbk", "gb18030", "utf-8"):
        try:
            with open(p, encoding=enc, newline="") as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
    return []


def match_golden(nat: str, area: str = "", town: str = "", village: str = "",
                 path: str | Path | None = None) -> dict | None:
    """在黄金库里找同名同归属的记录。

    必须**自然村名 + 行政村**（有乡镇则再加乡镇，有区县则再加区县）同时匹配才算命中，
    否则同名自然村会跨村/跨县串味——这正是巷碌/普济要防的那种错。

    2026-10-30：area 此前只作形参、不参与比对，跨县同名会误命中，已补齐。
    """
    key = _norm_txt(nat)
    for r in load_golden(path):
        if _norm_txt(r.get("自然村", "")) != key:
            continue
        if village and _norm_txt(r.get("行政村", "")) \
                and _norm_txt(r.get("行政村", "")) != _norm_txt(village):
            continue
        if town and _norm_txt(r.get("乡镇", "")) \
                and _norm_txt(r.get("乡镇", "")) != _norm_txt(town):
            continue
        # 2026-10-30 补：area（区县）此前形参存在却不参与比对，导致跨县同名自然村
        # 串味（实测 match_golden("兰秀村", area="苍梧县") 误命中藤县那条）。
        # 沿用 village/town 同款宽松口径：仅当调用方给出区县、且库里有区县时才校验。
        if area and _norm_txt(r.get("区县", "")) \
                and _norm_txt(r.get("区县", "")) != _norm_txt(area):
            continue
        try:
            lon, lat = float(r["经度_WGS84"]), float(r["纬度_WGS84"])
        except (KeyError, ValueError, TypeError):
            continue
        return {"自然村": r.get("自然村", ""), "lon": lon, "lat": lat,
                "来源": r.get("来源", ""), "备注": r.get("备注", ""),
                "确认日期": r.get("确认日期", "")}
    return None


def save_golden(rows: list, path: str | Path | None = None,
                mode: str = "append") -> int:
    """写入黄金库。append 模式按（自然村,行政村,乡镇）去重后追加。"""
    import csv
    p = path or _GOLDEN_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    old = load_golden(p) if os.path.exists(p) else []
    if mode == "append":
        seen = {(_norm_txt(r.get("自然村", "")), _norm_txt(r.get("行政村", "")),
                 _norm_txt(r.get("乡镇", ""))) for r in old}
        for r in rows:
            k = (_norm_txt(r.get("自然村", "")), _norm_txt(r.get("行政村", "")),
                 _norm_txt(r.get("乡镇", "")))
            if k not in seen:
                old.append(r)
                seen.add(k)
    else:
        old = rows
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=_GOLDEN_FIELDS)
        w.writeheader()
        for r in old:
            w.writerow({k: r.get(k, "") for k in _GOLDEN_FIELDS})
    return len(old)


# ------------------------------------------------- 逆向验真（拿到坐标反查归属）

def td_regeo(lon: float, lat: float) -> dict:
    """天地图逆地理（入参 WGS84；CGCS2000 与 WGS84 差异厘米级，可直接传）。

    ⚠ 两个踩过的坑（2026-10-20 修正）：
      ① 逆地理必须用 `postStr={'lon':x,'lat':y}&type=geocode`，
         写成 `ds={...}` 会被服务端当成**正向**编码 → HTTP 400「keyWord 参数未填写」。
         曾因 `_get_retry` 对 400 照样重试，每次白烧 6 s 退避休眠。
      ② 天地图**没有 village 字段**，村名在 addressComponent.poi 里。
         旧代码读 `village` 恒为空 → 天地图的村级验真从来没生效过。

    返回 {"区县","乡镇","村","地址","省码","市码","区县码","乡镇码"} 或 {}
    —— 行政编码是逆向纠偏的硬依据（用户第 1 条：先抓各级行政编码）。
    """
    limiter("tianditu").wait()
    ds = urllib.parse.quote(json.dumps({"lon": lon, "lat": lat}))
    url = (f"https://api.tianditu.gov.cn/geocoder?postStr={ds}"
           f"&type=geocode&tk={get_key('tianditu')}")
    try:
        d = json.loads(_get_retry(url))
    except Exception:
        return {}
    r = d.get("result") or {}
    c = r.get("addressComponent") or {}

    def _code(k):
        v = str(c.get(k) or "")
        return v[3:] if v.startswith("156") and len(v) > 6 else v

    return {"区县": c.get("county") or "", "乡镇": c.get("town") or "",
            "村": c.get("poi") or "", "地址": r.get("formatted_address") or "",
            "省码": _code("province_code"), "市码": _code("city_code"),
            "区县码": _code("county_code"), "乡镇码": _code("town_code")}


def baidu_regeo(lon: float, lat: float) -> dict:
    """百度逆地理（入参 WGS84，内部转 BD-09）。"""
    blon, blat = wgs84_to_bd09(lon, lat)
    url = ("https://api.map.baidu.com/reverse_geocoding/v3/?"
           f"location={blat:.6f},{blon:.6f}&output=json&ak={get_key('baidu')}")
    limiter("baidu").wait()
    try:
        d = json.loads(_get_retry(url))
    except Exception:
        return {}
    if d.get("status") != 0:
        return {}
    res = d.get("result") or {}
    c = res.get("addressComponent") or {}
    return {"区县": c.get("district") or "", "乡镇": c.get("town") or "",
            "村": c.get("village") or "", "地址": res.get("formatted_address") or ""}


def geocode_town_anchor(town: str, area: str = "", city: str = "",
                        sources: list | None = None,
                        parallel: bool = True) -> dict:
    """乡镇锚点：把「上一层级」变成一个**坐标**，供下游当空间参照。

    用途（用户 2026-10-20 第 1 条的落地手段）：
      查自然村前先定准乡镇在哪，才能
        ① 把搜索结果框在乡镇周边（否则图商会给你一堆外省同名村）
        ② 没有行政村中心时，用乡镇中心当距离排序的参考原点

    ⚠ 天地图 v2/search 的 `mapBound` **实测无效**——搜「沙江村」照样返回
      江西/湖南/福建的同名村；`specify` 参数直接 400。
      所以空间约束必须在**客户端**做，不能指望图商。

    取共识：三源各自正向编码，取坐标中位数（对单点离群稳健），
    并回报三源离散度——离散太大说明这个乡镇名有歧义，锚点不可靠。
    """
    if not town:
        return {}
    from config import available_sources
    srcs = sources or [s for s in ("amap", "tianditu", "baidu")
                       if s in available_sources()]
    kws = [f"{area}{town}" if area else town,
           f"{city}{area}{town}" if (city and area) else (area or "") + town]

    def _one(src):
        for kw in kws:
            try:
                rs = multi_search(src, kw, city=city, area=area, town=town, count=5)
            except (QuotaExceeded, PermissionError_):
                raise
            except Exception:                                # noqa: BLE001
                continue
            for r in rs:
                nm = r.get("name") or ""
                if town.replace("街道", "").replace("镇", "").replace("乡", "") in nm \
                        or nm in (town, f"{area}{town}"):
                    return src, (r["lon"], r["lat"], nm)
            if rs:
                return src, (rs[0]["lon"], rs[0]["lat"], rs[0].get("name", ""))
        return src, None

    got = {}
    if parallel and len(srcs) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(srcs)) as ex:
            for s, v in ex.map(_one, srcs):
                got[s] = v
    else:
        for s in srcs:
            got[s] = _one(s)[1]

    pts = [(v[0], v[1]) for v in got.values() if v]
    if not pts:
        return {}
    lons = sorted(p[0] for p in pts)
    lats = sorted(p[1] for p in pts)
    clon = lons[len(lons) // 2]
    clat = lats[len(lats) // 2]
    spread = max(haversine_m(p[0], p[1], clon, clat) for p in pts) if len(pts) > 1 else 0
    return {"lon": clon, "lat": clat, "spread_m": round(spread),
            "源数": len(pts),
            "各源": {s: {"lon": v[0], "lat": v[1], "名": v[2]}
                     for s, v in got.items() if v}}


def regeo_multi(lon: float, lat: float, sources: list | None = None,
                parallel: bool = True) -> dict:
    """三源逆地理 -> {源: {"区县","乡镇","村","地址","区县码","乡镇码",...}}。

    腾讯逆地理不返回村级，不入列。
    三源并发（一家一个线程，互不干扰），结果按固定源序归并 → 与串行逐位一致。
    """
    from config import available_sources
    srcs = sources or [s for s in ("amap", "tianditu", "baidu") if s in available_sources()]

    def _fetch(s):
        try:
            if s == "amap":
                r = amap_regeo(lon, lat)
                if isinstance(r, dict) and r:
                    return {"区县": r.get("区县") or "", "乡镇": r.get("乡镇") or "",
                            "村": r.get("村") or "", "地址": r.get("地址") or "",
                            "区县码": r.get("区县码") or "", "乡镇码": r.get("乡镇码") or ""}
            elif s == "tianditu":
                return td_regeo(lon, lat) or None
            elif s == "baidu":
                return baidu_regeo(lon, lat) or None
        except (QuotaExceeded, PermissionError_):
            raise
        except Exception:                                   # noqa: BLE001
            return None
        return None

    got = {}
    if parallel and len(srcs) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(srcs)) as ex:
            for s, r in zip(srcs, ex.map(_fetch, srcs)):
                got[s] = r
    else:
        for s in srcs:
            got[s] = _fetch(s)

    return {s: r for s, r in got.items() if r}


def addr_precision(addr: str, name: str = "", ea: str = "", et: str = "",
                   nat: str = "") -> int:
    """地址精度分：正向查询给出的地址写到哪一级，就说明这个点被标注到多细。

    实测（2026-10-20）——这是区分同名自然村最有效的一个信号：
      足荣镇·多乐村 正确点：tianditu「…德保县**足荣镇**多乐村」     → 精度 3
                   错误点：baidu  「百色市德保县云梯--荣华公路」    → 精度 1（只到区县，还是条路）
      桥圩镇·根竹岭 正确点：tianditu「…港南区**桥圩镇**根竹岭屯」   → 精度 3
                    错误点：baidu  「贵港市港南区」                 → 精度 1
    图商把点标到乡镇级的，才是真的知道这个村在哪。

    R24（2026-10-21）：**光到乡镇还不够，必须能分到村这一级。**
    实测宾阳中华镇·三军村：
      高德  地址「…宾阳县中华镇三军村」→ 写到村，正确（距标准 208 m）
      天地图 地址「…宾阳县中华镇」    → 只到镇，却是错点（距标准 1012 m）
    两者同得 3 分，票数又站在天地图那边（2:1），于是选了错的。
    补上第 4 级后：高德 4 > 天地图 3，内容判据压过票数。
    """
    a_addr = addr or ""
    a = f"{a_addr}{name or ''}"
    if not a.strip():
        return 0
    if et and et in a:
        # ⛔ 曾在此加过「地址含自然村名 → 4 分」，实测有害，已回退（2026-10-21）。
        #    「目标名 in 地址」对**任何同名候选都成立**——远处那个村也叫这名字，
        #    所以这一条根本没有区分度，只是把远处的同名村抬到和正确点同分，
        #    后面的票数/距离就把错的选上去了。实测：
        #      桥圩镇·新村  52 m → 36 km    容县·中平村 38 m → 773 m
        #    想让内容判据压过票数是对的（R23 精神），但**选错了判据**。
        return 3
    if ea and ea in a:
        return 2
    return 1


def name_fit(cand: str, target: str) -> int:
    """候选名与目标名的贴合度：**自然村本体应该就叫那个名字**。

    实测足荣镇·那亮：
      正确簇 代表名「那亮」                                    → 3（精确同名）
      错误簇 代表名「人社社保就业服务窗口(足荣镇那亮村级公共服务中心)」→ 1（只是提到它）
    错误簇 POI 有 28 条、正确簇只有 2 条——**条目数是反的信号**，
    围着村委的服务设施多，恰恰说明那儿是行政中心而不是自然村本体。
    """
    if not cand or not target:
        return 0
    if cand == target:
        return 3
    for t in ("村民委员会", "村委会", "村", "屯", "组", "寨", "队", "屋"):
        if cand == target + t:
            return 2
    if target in cand:
        return 1
    return 0


def verify_admin_multi(lon: float, lat: float, expect_area: str = "",
                       expect_town: str = "", expect_village: str = "",
                       sources: list | None = None,
                       pos_addr: str = "") -> dict:
    """逆向验真：**正向拿到坐标后，反查该点是否真归属指定行政区**。

    这是唯一能发现「名字对、归属错」的手段（用户 2026-10-20 明确要求）。
    例：平南县的兰秀村 → 反查区县为「平南县」≠ 期望「藤县」→ 判不通过、直接剔。

    三源各自独立判定，任一股**明确不符**即判不通过；
    若三源都无返回，判"证据不足"（不通过也不合格，如实说明）。
    """
    regs = regeo_multi(lon, lat, sources=sources)
    if not regs:
        return {"核验": "无返回", "通过": False, "判定依据": [],
                "各源": {}, "原因": ["三源逆地理均无返回，无法验真"]}

    ok_list, bad_list, detail = [], [], {}
    for src, r in regs.items():
        a, t, v = r.get("区县", ""), r.get("乡镇", ""), r.get("村", "")
        bad = []
        if expect_area and a and expect_area not in a and a not in expect_area:
            bad.append(f"区县「{a}」≠ 期望「{expect_area}」")
        if expect_town and t and expect_town not in t and t not in expect_town:
            bad.append(f"乡镇「{t}」≠ 期望「{expect_town}」")
        if expect_village and v and expect_village not in v and v not in expect_village:
            bad.append(f"村「{v}」≠ 期望「{expect_village}」")
        detail[src] = {"区县": a, "乡镇": t, "村": v, "地址": r.get("地址", ""),
                       "区县码": r.get("区县码", ""), "乡镇码": r.get("乡镇码", ""),
                       "不符": bad}
        if bad:
            bad_list.append(f"[{src}] " + "；".join(bad))
        elif a or t:
            ok_list.append(src)

    # 正向地址证据：正向查询时图商亲手写下的地址（如「…桥圩镇根竹岭屯」）
    # 与逆地理是两条独立的证据链，必须一并计入，不能只听逆地理的。
    if pos_addr:
        p = addr_precision(pos_addr, ea=expect_area, et=expect_town)
        if p >= 3:
            ok_list.append("正向地址")
        elif p == 2 and not bad_list:
            ok_list.append("正向地址(到区县)")

    if not ok_list and not bad_list:
        return {"核验": "证据不足", "通过": False, "判定依据": [],
                "各源": detail, "原因": ["三源均未提供区县/乡镇级归属信息"]}

    # 判定改为**票数制**而非「一票否决」。
    # 旧逻辑 `passed = ok_list and not bad_list`：只要有一家说不符就全否。
    # 实测桥圩镇·根竹岭：唯一正确的簇（正向地址明写「桥圩镇根竹岭屯」）
    # 仅因百度一家反查报「瓦塘镇」就被判不通过，结果拱手让给 15 km 外的错簇。
    nb, no = len(bad_list), len(ok_list)
    if nb >= 2:
        passed = False
    elif nb == 1 and no == 0:
        passed = False          # 唯一证据还是反的，不敢用
    else:
        passed = True
    return {"核验": "通过" if passed else "不通过", "通过": passed,
            "支持源": ok_list, "反对源": [b.split("]")[0][1:] for b in bad_list],
            "判定依据": bad_list or [f"{no} 源归属一致"],
            "各源": detail, "原因": bad_list}


def query_natural_full(raw: str, city: str = "", area: str = "", town: str = "",
                       village: str = "", village_center: tuple | None = None,
                       sources: list | None = None, bbox: str | None = None,
                       max_ladder: int = 9, count: int = 20,
                       do_verify: bool = True, cluster_m: int = 300,
                       use_golden: bool = True, early_stop: bool = False,
                       parallel: bool = True, per_src_workers: int = _DEF_PER_SRC,
                       verify_topk: int = 6, town_anchor: tuple | None = None,
                       spatial_gate_m: int = 30000) -> dict:
    # early_stop：2026-10-20 A/B 实测——9 点只快 19%，却把巷碌村改判 5802 m。
    #   拿 19% 提速换 5.8 km 改判不划算，**默认关闭**，仅留作 A/B 对照开关。
    # parallel：改成并发才是正解——4 家图商互不干扰，可并行；
    #   同时用 per_src_workers 限制「同一家」的并发数，避免触发各家的 QPS 限流。
    #   并发只改**收集顺序**，不改收集内容；收集后按 (阶梯, 源序) 排序再聚簇，
    #   因此结果与串行逐位一致（已由 A/B 对照证明）。
    """丙方案单条入口：**必出结果**，四个源全部试过才算真的没找到。

    流程：切分 → 阶梯 → **四源 × 全阶梯遍历** → R1 本体过滤 → 正向地址粗筛
    （剔外县同名）→ 聚簇排序 → **逆向验真**（反查是否真归属藤县/平福乡/巷蓬村）。

    与 `query_natural_multi` 的差别：后者只在三个源上跑、且只按固定阶梯；
    本函数把「找不到就切开来查」和「拿到坐标反查归属」都做进去了。

    返回 dict（**必然返回，命中=False 时也带完整轨迹供用户判断**）：
      {"名称","命中","lon","lat","来源","生效检索词","阶梯","支持源",
       "逆向验真","簇","暂选","存疑","轨迹","候选","剔出","建议"}
    """
    from config import available_sources
    srcs = sources or [s for s in ("tianditu", "amap", "baidu", "qq")
                       if s in available_sources()]
    if not srcs:
        raise KeyError("没有任何已配置 Key 的数据源，无法联网查询。")

    parts = split_address_parts(raw, area, town, village)
    nat = parts["自然村"] or _norm_txt(raw)
    token = nat.replace("村", "").replace("组", "")
    ea = parts["区县"] or area or ""
    et = parts["乡镇"] or town or ""
    ev = parts["行政村"] or village or ""

    # 参考原点：优先行政村中心（用户给的村委/实测点）；
    # 没有则退到**乡镇锚点**——否则 距村委_m 全是 None，
    # 簇排序里那句「同票按距村委升序」会退化成「同票随机」，距离判据直接失效。
    if not village_center and et and town_anchor is None:
        town_anchor_t = geocode_town_anchor(et, area=ea, city=city)
        if town_anchor_t:
            town_anchor = (town_anchor_t["lon"], town_anchor_t["lat"])
    origin = village_center or town_anchor

    # 黄金库优先：用户实测过的值直接命中，不必再赌图商标注。
    # 这是唯一能治「图商标注本身相反」的办法（巷碌/普济即此类）。
    if use_golden:
        g = match_golden(nat, ea, et, ev)
        if g:
            dv = (round(haversine_m(g["lon"], g["lat"],
                                    village_center[0], village_center[1]))
                  if village_center else None)
            return {"名称": nat, "命中": True, "lon": g["lon"], "lat": g["lat"],
                    "来源": f"黄金样本·{g['来源'] or '用户确认'}",
                    "生效检索词": "（黄金库直接命中，未查图商）", "阶梯": 0,
                    "支持源": ["黄金库"],
                    "逆向验真": {"核验": "跳过（用户已确认）", "通过": True},
                    "簇": [{"簇号": 1, "lon": g["lon"], "lat": g["lat"],
                            "源": ["黄金库"], "源数": 1, "条目数": 1,
                            "距村委_m": dv, "代表名": nat, "地址": g["备注"] or ""}],
                    "暂选": 1, "存疑": [], "轨迹": [], "候选": [], "剔出": [],
                    "建议": "", "命中方式": "黄金库"}

    ladder = full_ladder(raw, city, ea, et, ev, natural=nat)
    trace, body_rows, drop_rows = [], [], []

    ladder = ladder[:max_ladder]

    def _one(src: str, q: str, qi: int) -> tuple:
        """查 (源, 检索词) 一次，返回 (trace, body, drop)。不抛异常。"""
        try:
            rows = multi_search(src, q, city=city, area=ea, town=et,
                                count=count, bbox=bbox)
        except QuotaExceeded as e:
            tripped = mark_source_fail(src, daily=getattr(e, "daily", False))
            return ({"阶梯": qi, "检索词": q, "源": src,
                     "结果": f"配额 {e}" + ("｜已熔断该源" if tripped else "")}, [], [])
        except Exception as e:                              # noqa: BLE001
            return ({"阶梯": qi, "检索词": q, "源": src,
                     "结果": f"ERR {type(e).__name__}"}, [], [])
        b, d = [], []
        for r in rows:
            rr = dict(r)
            rr["检索词"], rr["阶梯"] = q, qi
            rr["距村委_m"] = (round(haversine_m(r["lon"], r["lat"],
                                                origin[0], origin[1]))
                              if origin else None)
            (b if is_body_hit(r["name"], token) else d).append(rr)
        mark_source_ok(src)
        return ({"阶梯": qi, "检索词": q, "源": src,
                 "原始": len(rows), "本体": len(b)}, b, d)

    # 并发上限钳制：每源最高 _MAX_PER_SRC（10，全局 40）。
    # 超了必然撞图商限流/封 Key，所以在代码里夹死。
    per_src_workers = max(1, min(int(per_src_workers or 1), _MAX_PER_SRC))

    # 任务清单按 (阶梯, 源序) 预先展开——顺序与串行时完全一致
    tasks = [(qi, q, src)
             for qi, q in enumerate(ladder, 1) for src in srcs]

    results = {}
    if parallel and len(tasks) > 1:
        # 并发只跨图商、不猛捶单家：每家最多 per_src_workers 个并发
        from concurrent.futures import ThreadPoolExecutor
        import threading
        sems = {s: threading.Semaphore(per_src_workers) for s in srcs}

        def _run(t):
            qi, q, src = t
            with sems[src]:
                return t, _one(src, q, qi)

        with ThreadPoolExecutor(max_workers=len(srcs) * per_src_workers) as ex:
            for t, r in ex.map(_run, tasks):
                results[t] = r
    else:
        for t in tasks:
            results[t] = _one(t[2], t[1], t[0])

    # 按任务原序归并 → 并发与串行逐位一致
    for t in tasks:
        tr, b, d = results[t]
        trace.append(tr)
        body_rows.extend(b)
        drop_rows.extend(d)

    # 早停（默认关闭）：A/B 实测提速 19% 却改判 5802 m，不划算
    if early_stop:
        by_q = {}
        for r in body_rows:
            by_q.setdefault(r["检索词"], set()).add(r["source"])
        cut = None
        for qi, q in enumerate(ladder, 1):
            if qi > 1 and len(by_q.get(ladder[qi - 2], ())) >= 2:
                cut = qi
                break
        if cut:
            keep_q = set(ladder[:cut - 1])
            body_rows = [r for r in body_rows if r["检索词"] in keep_q]
            trace.append({"阶梯": cut, "检索词": ladder[cut - 1], "源": "*",
                          "结果": "早停（上一级已 ≥2 源本体命中）"})

    # ---- 空间闸门：把候选框死在乡镇周边
    # 为什么必须在客户端做：天地图 mapBound 实测无效（搜「沙江村」照样返回
    # 江西/湖南/福建的同名村），specify 直接 400；高德/百度也只有 city/region
    # 粗粒度限定。指望图商挡，就等着被外省同名村淹没（容县「大冲」曾 31 簇、
    # 柳江「三元」40 簇里混着江西三元）。
    # 半径取 30 km 是**粗筛**：只负责砍掉跨市跨省的垃圾，不负责区分邻镇同名
    # ——邻镇同名交给后面的簇级验真，两层各管一头。
    if town_anchor and spatial_gate_m > 0:
        before = len(body_rows)
        body_rows = [r for r in body_rows
                     if haversine_m(r["lon"], r["lat"],
                                    town_anchor[0], town_anchor[1]) <= spatial_gate_m]
        n_drop = before - len(body_rows)
        if n_drop:
            trace.append({"阶梯": 0, "检索词": "*", "源": "*",
                          "结果": f"空间闸门剔出 {n_drop} 个（距乡镇锚点 "
                                  f">{spatial_gate_m/1000:.0f} km）"})

    # 正向地址粗筛：地址里明确是外县/外乡镇的同名点，直接剔（用户第 3 条）
    kept, cross = [], []
    for r in body_rows:
        a = f"{r.get('addr','')}{r.get('name','')}"
        if ea and ea in a:
            kept.append(r)
        elif et and et in a:
            kept.append(r)
        elif a.strip():
            cross.append(r)
        else:
            kept.append(r)          # 地址缺失 ≠ 地址错
    if kept:
        body_rows = kept

    # 聚簇 + 排序（票数降序，票数相同按距村委升序）
    clusters = []
    for r in body_rows:
        for c in clusters:
            if haversine_m(r["lon"], r["lat"], c[0]["lon"], c[0]["lat"]) <= cluster_m:
                c.append(r)
                break
        else:
            clusters.append([r])
    cl = []
    for i, c in enumerate(clusters, 1):
        ss = sorted({x["source"] for x in c})
        # 地址精度取簇内**最好**的一条：只要有一家把点标到了乡镇级，
        # 就说明这个位置是被确认过的（多乐村/根竹岭的成败全在这条上）
        prec = max(addr_precision(x.get("addr", ""), x.get("name", ""), ea, et,
                                  nat=nat)
                   for x in c)
        # 名称贴合度取簇内最好的一条：簇里只要有一条就叫这个名字，就算命中本体
        fit = max(name_fit(x.get("name", ""), nat) for x in c)
        # ⛔ R23「簇代表取最佳行」实测有害，已回退（2026-10-21）：容县·中平村 38 m → 557 m。
        #    原因：地址精度/名称贴合描述的是**标注质量**，不是**坐标质量**；
        #    簇内"说得最细"的那条，位置未必最准。
        #    维持 c[0]，并把"分数靠 A 拿、坐标交 B"这个错位作为**已知缺陷**记录在案
        #    （宾阳·三军村即此缺陷的受害者：高德 208 m 被天地图 1012 m 顶掉）。
        cl.append({"簇号": i, "lon": c[0]["lon"], "lat": c[0]["lat"],
                   "源": ss, "源数": len(ss), "条目数": len(c),
                   "距村委_m": c[0]["距村委_m"], "地址精度": prec,
                   "名称贴合": fit,
                   "代表名": min((x["name"] for x in c), key=len),
                   "地址": c[0].get("addr", "")})
    # 排序：地址精度 → **名称贴合** → 票数 → 距离。
    # ① 地址精度：图商把点标到乡镇级的才是真知道位置的（多乐村/根竹岭靠它翻盘）
    # ② 名称贴合：自然村本体就叫那个名字，不是"提到它"的设施（那亮靠它翻盘）
    # ③ 票数：跨源交叉验证
    # ④ 距离：**只当末位微弱参考**。自然村离镇政府普遍 8~10 km，
    #    两个候选差 276 m 纯属噪声（多乐村就是被这 276 m 带偏的）。
    cl.sort(key=lambda c: (-c["地址精度"], -c["名称贴合"], -c["源数"],
                           c["距村委_m"] if c["距村委_m"] is not None else 9e9))

    if not cl:
        return {"名称": nat, "命中": False, "lon": None, "lat": None,
                "来源": "", "生效检索词": "", "阶梯": 0, "支持源": [],
                "逆向验真": {}, "簇": [], "暂选": None,
                "存疑": ["四源 × 全阶梯均未命中本体"], "轨迹": trace,
                "候选": body_rows, "剔出": drop_rows + cross,
                "建议": ("已试遍四个源与 "
                         f"{len(ladder[:max_ladder])} 级检索词仍无本体命中；"
                         "请核对该自然村名称用字，或提供邻近已知点的坐标作为锚点")}

    # ---- 簇级验真：本模块**最大的漏洞**曾在这里
    # 旧逻辑：先按票数选第一名，再只对这第一名做逆向验真；
    #         验真不通过也只是加个「存疑」标注，**照样输出那个错点**。
    # 实测宾阳中华·沙江村：票数最高的簇是邻镇宾州镇的同名村（距目标 19 km），
    #         三源逆地理都判「乡镇≠中华镇」，却仍被输出。
    # 新逻辑：对前 K 个候选簇**逐个**验真，验不过的直接淘汰，
    #         从通过验真的簇里再按票数选。验真是选点的前置条件，不是事后备注。
    verd = {}
    promoted = None
    if do_verify:
        K = max(1, min(verify_topk, len(cl)))

        def _ver(c):
            try:
                return verify_admin_multi(c["lon"], c["lat"],
                                          expect_area=ea, expect_town=et,
                                          expect_village=ev,
                                          pos_addr=c.get("地址", ""))
            except (QuotaExceeded, PermissionError_):
                return {"核验": "配额受限，未验", "通过": True}
            except Exception as e:                          # noqa: BLE001
                return {"核验": f"验真异常 {type(e).__name__}", "通过": True}

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(K, 6)) as ex:
            vs = list(ex.map(_ver, cl[:K]))
        for c, v in zip(cl[:K], vs):
            c["验真"] = v.get("核验", "")
            c["验真通过"] = bool(v.get("通过"))
            c["验真依据"] = v.get("判定依据", [])
        for c in cl[K:]:
            c["验真"] = "未验（排名靠后）"
            c["验真通过"] = False

        passed = [c for c in cl if c.get("验真通过")]
        if passed:
            # 通过验真的簇之间，仍按 地址精度 → 名称贴合 → 票数 → 距离 排序
            passed.sort(key=lambda c: (-c["地址精度"], -c["名称贴合"], -c["源数"],
                                       c["距村委_m"] if c["距村委_m"] is not None else 9e9))
            if passed[0] is not cl[0]:
                promoted = cl[0]
            best = passed[0]
            verd = {"核验": best["验真"], "通过": True,
                    "判定依据": best.get("验真依据") or []}
        else:
            # 全军覆没时，退到**反对票最少**的簇，而不是票数最高的簇。
            # 旧逻辑退回 cl[0]（票数第一），等于把被三源否掉的错簇又捡回来
            # （桥圩镇·根竹岭就是这么被 15 km 外的湛江镇同名村顶掉的）。
            def _bad_n(c):
                ys = c.get("验真依据") or []
                return len(ys) if c.get("验真") == "不通过" else 0
            cand = sorted(cl, key=lambda c: (_bad_n(c), -c["地址精度"],
                                             -c.get("名称贴合", 0), -c["源数"]))
            best = cand[0]
            verd = {"核验": best.get("验真") or "不通过", "通过": False,
                    "判定依据": best.get("验真依据") or []}
    else:
        best = cl[0]

    # ---- R21 输入提示兜底：**place 查不到时改走在线地图那一路**
    # 四源 place（POI 库）全军覆没时，不代表这个村不存在——
    # 它很可能是自然村"地名注记"，只在综合检索里。
    # 高德 inputtips 就是在线地图搜索框用的那一路（实测沙江村 0 m 命中）。
    if do_verify and ((not verd.get("通过")) or (not cl)):
        # R21 补充：**inputtips 必须用裸村名**，加行政区划前缀反而查不到。
        # 实测（city 固定、只换 kw）："沙江村"→10 条命中 /
        # "中华镇沙江村"→0 条 / "宾阳县中华镇沙江村"→0 条。
        # 所以裸名排第一，带前缀的写法只当补充。
        # 且不再"有候选就 break"：裸名命中但验真不过时，继续试更具体的写法。
        picked = None
        for kw in (nat, f"{et}{nat}", f"{ea}{et}{nat}"):
            if not kw.strip():
                continue
            try:
                got = amap_tips(kw, city=city, count=10)
            except (QuotaExceeded, PermissionError_):
                break
            except Exception:                                # noqa: BLE001
                continue
            rows = []
            for r in got:
                rr = dict(r)
                rr["检索词"], rr["阶梯"] = kw, 99
                rr["距村委_m"] = (round(haversine_m(rr["lon"], rr["lat"],
                                                    origin[0], origin[1]))
                                 if origin else None)
                # 空间闸门同样生效：不许跑到别的乡镇去
                if town_anchor and spatial_gate_m > 0 and (
                        haversine_m(rr["lon"], rr["lat"],
                                    town_anchor[0], town_anchor[1]) > spatial_gate_m):
                    continue
                if is_body_hit(rr["name"], token):
                    rows.append(rr)
            if not rows:
                continue
            # 名称贴合优先（自然村本体就叫那个名字）→ 距原点近的优先
            rows.sort(key=lambda x: (-name_fit(x["name"], nat),
                                     x["距村委_m"] if x["距村委_m"] is not None else 9e9))
            tp = rows[0]
            tverd = verify_admin_multi(tp["lon"], tp["lat"], expect_area=ea,
                                       expect_town=et, expect_village=ev,
                                       pos_addr=tp.get("address", ""))
            if tverd.get("通过"):
                picked = (tp, tverd, kw)
                break
            trace.append({"阶梯": 99, "检索词": kw, "源": "amap_tips",
                          "结果": f"有候选但验真{tverd.get('核验')}，改试下一个检索词"})
        if picked:
            tp, tverd, kw = picked
            cl.insert(0, {"簇号": 0, "lon": tp["lon"], "lat": tp["lat"],
                          "源": ["amap_tips"], "源数": 1, "条目数": 1,
                          "距村委_m": tp["距村委_m"],
                          "地址精度": addr_precision(tp.get("address", ""),
                                                    tp["name"], ea, et,
                                                    nat=nat),
                          "名称贴合": name_fit(tp["name"], nat),
                          "代表名": tp["name"], "地址": tp.get("address", ""),
                          "验真": tverd.get("核验"), "验真通过": True,
                          "验真依据": tverd.get("判定依据") or []})
            best = cl[0]
            verd = {"核验": tverd.get("核验"), "通过": True,
                    "判定依据": tverd.get("判定依据") or []}
            trace.append({"阶梯": 99, "检索词": kw,
                          "源": "amap_tips", "结果": "输入提示兜底命中"})

    susp = []
    if promoted is not None:
        susp.append(
            f"簇级验真改判：票数最高的簇（{promoted['源数']} 源，"
            f"距村委 {promoted['距村委_m']} m）逆向验真不通过，"
            f"已改用通过验真的簇（{best['源数']} 源，"
            f"距村委 {best['距村委_m']} m）")
    if len(cl) >= 2:
        far = max((c["距村委_m"] or 0) for c in cl)
        near = min((c["距村委_m"] or 0) for c in cl)
        if far - near > 2000:
            susp.append(f"同名多点：{len(cl)} 个本体簇"
                        f"（最近 {near:.0f} m / 最远 {far:.0f} m）")
    if best["源数"] < 2:
        susp.append(f"仅 {best['源']} 单源命中，无交叉验证")
    if verd.get("核验") == "不通过":
        susp.append("逆向验真不通过：" + "；".join(verd.get("判定依据", [])))
    elif verd.get("核验") == "证据不足":
        susp.append("逆向验真证据不足（三源未返回归属信息）")

    return {"名称": nat, "命中": True, "lon": best["lon"], "lat": best["lat"],
            "来源": "/".join(best["源"]), "生效检索词": cl and _eff_kw(body_rows, best) or "",
            "阶梯": min((x["阶梯"] for x in body_rows
                         if abs(x["lon"] - best["lon"]) < 1e-9), default=0),
            "支持源": best["源"], "逆向验真": verd, "簇": cl,
            "暂选": best["簇号"], "存疑": susp, "轨迹": trace,
            "候选": body_rows, "剔出": drop_rows + cross, "建议": ""}


def _eff_kw(body_rows: list, best: dict) -> str:
    for x in body_rows:
        if abs(x["lon"] - best["lon"]) < 1e-9 and abs(x["lat"] - best["lat"]) < 1e-9:
            return x["检索词"]
    return ""


def _robust_upper(vals: list, floor_m: float = 3000.0) -> float:
    """同批分布的稳健上界 = 中位数 + 3×MAD×1.4826，且不低于 floor_m。

    用户 2026-10-20 第 1 条：判据要用**同批自然村自身的分布**当尺子——
    「其他村都没超 3 km，突然来个 5733 m 的，那肯定存疑」。
    硬编码 3 km 在山区/平原都会错，用中位数 + MAD 才能自适应。
    """
    vs = sorted(v for v in vals if v is not None)
    if not vs:
        return floor_m
    n = len(vs)
    mid = vs[n // 2] if n % 2 else (vs[n // 2 - 1] + vs[n // 2]) / 2
    dev = sorted(abs(v - mid) for v in vs)
    mad = dev[n // 2] if n % 2 else (dev[n // 2 - 1] + dev[n // 2]) / 2
    return max(mid + 3 * 1.4826 * mad, floor_m)


def batch_natural_full(names: list, city: str = "", area: str = "", town: str = "",
                       village: str = "", village_center: tuple | None = None,
                       sources: list | None = None, bbox: str | None = None,
                       **kw) -> list:
    """批量：**必填满**——每个输入名字都要出一行，命中与否都给交代。

    两遍扫描：
      第一遍 逐条四源 × 全阶梯取候选簇；
      第二遍 用同批自然村的**实际距离分布**重判候选（用户第 1 条），
             剔掉离群簇并重选，而不是拿固定 3 km 硬卡。
    """
    from config import available_sources
    srcs = sources or [s for s in ("tianditu", "amap", "baidu", "qq")
                       if s in available_sources()]

    # 乡镇锚点：**整批只解析一次**再下发给每条。
    # 44 个点逐个去问「XX镇在哪」会白白多 44×3 次查询；
    # 而乡镇锚点是空间闸门的基准，必须先于自然村搜索拿到（用户第 1 条）。
    anchor = kw.pop("town_anchor", None)
    if anchor is None and town:
        a = geocode_town_anchor(town, area=area, city=city)
        if a:
            anchor = (a["lon"], a["lat"])

    # 第一遍：取候选
    raws = []
    for nm in names:
        raws.append({"输入": nm,
                     "r": query_natural_full(nm, city=city, area=area, town=town,
                                             village=village,
                                             village_center=village_center,
                                             sources=srcs, bbox=bbox,
                                             town_anchor=anchor, **kw)})

    # 批中心：用户常常只给到「镇」，没有行政村中心。此时用**本批自然村坐标的
    # 中位数**当参考原点——中位数对离群点稳健，个别偏远村不会把原点带跑，
    # 而平均值会被 40 km 外的同名点直接拽偏。
    # 注意 raws 是 [{"输入":..., "r":<结果>}] 两层结构，取结果必须走 it["r"]
    if not village_center:
        # 批中心只拿**验真通过**的点来算：一个 15 km 外的错簇就能把中位数拖偏，
        # 而它是用来当尺子量别人的——尺子自己歪了，量出来的全是冤案。
        pts = [(it["r"]["lon"], it["r"]["lat"]) for it in raws
               if it["r"]["命中"] and it["r"]["lon"] is not None
               and (it["r"].get("逆向验真") or {}).get("通过")]
        if not pts:
            pts = [(it["r"]["lon"], it["r"]["lat"]) for it in raws
                   if it["r"]["命中"] and it["r"]["lon"] is not None]
        if pts:
            lo = sorted(p[0] for p in pts)
            la = sorted(p[1] for p in pts)
            cand = (lo[len(pts) // 2], la[len(pts) // 2])
            # 批中心被离群点带跑时（比如一半候选在外省），它自己就不可信了；
            # 这时宁可退回乡镇锚点。
            if anchor is None or haversine_m(cand[0], cand[1], *anchor) <= 30000:
                village_center = cand
            else:
                village_center = anchor
    if village_center:
        for it in raws:
            for c in it["r"].get("簇") or []:
                c["距村委_m"] = round(haversine_m(c["lon"], c["lat"],
                                                  village_center[0], village_center[1]))
            for x in it["r"].get("候选", []):
                x["距村委_m"] = round(haversine_m(x["lon"], x["lat"],
                                                  village_center[0], village_center[1]))

    # 第二遍：同批分布自适应
    near_vals = []
    for it in raws:
        cl = it["r"].get("簇") or []
        ds = [c["距村委_m"] for c in cl if c["距村委_m"] is not None]
        if ds:
            near_vals.append(min(ds))
    upper = _robust_upper(near_vals)

    out = []
    for it in raws:
        r = it["r"]
        cl = list(r.get("簇") or [])
        if cl and village_center:
            good = [c for c in cl
                    if c["距村委_m"] is None or c["距村委_m"] <= upper]
            dropped = [c for c in cl if c not in good]
            if good:
                cl = good
                for c in dropped:
                    r["存疑"].append(
                        f"候选簇{c['簇号']}距村委 {c['距村委_m']} m "
                        f"> 同批自适应上界 {upper:.0f} m，判为离群已剔")
            # 剔完离群后**必须重排**：距离口径已换成批中心，沿用旧顺序等于没剔。
            # 且验真通过是硬门槛——不能因为剔离群就把验真通过的换成不通过的
            # （桥圩镇·尖岭就是这么被 5 km 外的八塘街道同名村顶掉的）。
            pool = [c for c in cl if c.get("验真通过")] or cl
            pool.sort(key=lambda c: (-c.get("地址精度", 0), -c.get("名称贴合", 0),
                                     -c["源数"],
                                     c["距村委_m"] if c["距村委_m"] is not None else 9e9))
            best = pool[0]
            r["lon"], r["lat"] = best["lon"], best["lat"]
            r["来源"] = "/".join(best["源"])
            r["支持源"] = best["源"]
            r["生效检索词"] = _eff_kw(r.get("候选", []), best)
            r["簇"] = cl
            r["暂选"] = best["簇号"]
            r["同批上界_m"] = round(upper)
            if len(cl) >= 2:
                far = max((c["距村委_m"] or 0) for c in cl)
                near = min((c["距村委_m"] or 0) for c in cl)
                if far - near > 2000:
                    r["存疑"].append(
                        f"仍存 {len(cl)} 个候选簇（{near:.0f}~{far:.0f} m），"
                        f"已按距村委最近选取")
        r["输入"] = it["输入"]
        out.append(r)
    return out


def self_test():
    """能力层自检：验证坐标算法与连通性"""
    print("=" * 56)
    print("地理数据采集器 · 能力层自检")
    print("=" * 56)

    # 1. 纠偏算法黄金向量（往返误差应为 0）
    print("\n[1] 坐标算法往返校验")
    for lon, lat in [(108.366543, 22.817765), (110.299121, 25.274215), (109.413, 24.325)]:
        glon, glat = wgs84_to_gcj02(lon, lat)
        rlon, rlat = gcj02_to_wgs84(glon, glat)
        err = haversine_m(lon, lat, rlon, rlat)
        flag = "OK" if err < 0.1 else "FAIL"
        print(f"    WGS84({lon},{lat}) -> GCJ02 -> 回算 误差 {err:.4f} m  [{flag}]")

    # 2. BD-09 往返
    print("\n[2] BD-09 往返校验")
    lon, lat = 108.366543, 22.817765
    blon, blat = wgs84_to_bd09(lon, lat)
    rlon, rlat = bd09_to_wgs84(blon, blat)
    err = haversine_m(lon, lat, rlon, rlat)
    print(f"    往返误差 {err:.4f} m  [{'OK' if err < 0.1 else 'FAIL'}]")

    # 3. 距离校验（南宁市政府 -> 柳州 约 190km 量级）
    print("\n[3] 距离计算校验")
    d = haversine_m(108.3622, 22.8199, 109.413, 24.325)
    print(f"    南宁 -> 柳州 距离 {d/1000:.1f} km  [{'OK' if 180 < d/1000 < 220 else 'CHECK'}]")

    # 4. 数据源连通性
    print("\n[4] 数据源配置与连通性")
    from config import available_sources, has_key, mask_key
    srcs = available_sources()
    if not srcs:
        print("    未配置任何 Key（可用坐标转换/区划查询，但无法联网采集）")
    for s in FALLBACK_ORDER:
        if has_key(s):
            print(f"    {s}: 已配置 Key={mask_key(load_config()['keys'][s])}")
        else:
            print(f"    {s}: 未配置")

    print("\n[5] 天地图实连测试")
    if has_key("tianditu"):
        try:
            r = td_geocode("南宁市人民政府")
            if r:
                print(f"    返回 ({r[0]}, {r[1]}) score={r[2]} level={r[3]}  [OK]")
            else:
                print("    未返回结果  [CHECK]")
        except QuotaExceeded as e:
            print(f"    配额限制：{e}")
        except Exception as e:
            print(f"    异常：{e}")
    else:
        print("    跳过（未配置天地图 Key）")

    print("\n" + "=" * 56)


# ---------------------------------------------------------------- 坐标系侦测

_ADDR_TOWN = re.compile(r"[一-龥]{2,8}(?:镇|乡|街道)")
_ADDR_ROAD = re.compile(r"[一-龥]{2,8}(?:路|街|大道|巷|弄|胡同)")
_ADDR_NUM = re.compile(r"\d+\s*(?:号|栋|幢|号楼|号房)")


def _strip_admin(s: str) -> str:
    """剥掉地址开头的行政区划前缀（省 → 市 → 区县 → 乡镇）。

    不剥会怎样（2026-10-23 实测踩中）：路名正则 `[一-龥]{2,8}(?:路|街|大道)`
    是贪婪的，会把「良庆区玉洞大道」整段吞成 `市良庆区玉洞大道` 这个 token，
    在逆地理地址里永远找不到 —— **与 R-P9 区县正则吞字是同一类错误**。
    """
    s = re.sub(r"^[一-龥]{2,10}(?:壮族自治区|回族自治区|维吾尔自治区|自治区|省)", "", s)
    s = re.sub(r"^[一-龥]{2,6}市", "", s)
    s = re.sub(r"^[一-龥]{2,4}(?:区|县|旗)", "", s)
    s = re.sub(r"^[一-龥]{2,8}(?:街道|镇|乡)", "", s)
    return s


def addr_tokens(addr: str) -> list:
    """从用户地址里抽出可用于比对的细粒度 token（路名 / 门牌）。

    为什么必须是**细粒度**：GCJ-02 与 WGS84 的偏移只有 300~600 m，
    在城区足以张冠李戴，但**区县级别往往仍然相同** —— 用区县比对区分不了两种假设。
    """
    if not addr:
        return []
    s = _strip_admin(addr).replace(" ", "").replace("　", "")
    out, seen = [], set()
    for pat in (_ADDR_ROAD, _ADDR_NUM):
        for m in pat.finditer(s):
            t = m.group(0)
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out


def detect_crs(points, addrs=None, area="", sample_n=10,
               seed=20261023, verbose=False) -> dict:
    """坐标系侦测：双假设反向验证（需求文档 §5.4）。

    原理（**可以自动侦测，但不可自动决断**）：
      假设 A「坐标是 WGS84」 → 直接逆地理（内部转 GCJ-02 查询）
      假设 B「坐标是 GCJ-02」 → 先转 WGS84 再逆地理（内部转回 GCJ-02，等于按原值查）
    两次查询的位置相差约 500 m（GCJ-02 偏移量），
    **谁的逆地理地址与用户给的地址/区县更吻合，谁就是真实坐标系**。

    points: [(lon, lat), ...]
    addrs : 与 points 对应的用户地址（有则最准）
    area  : 无地址列时的退化判据（所属区县名）
    sample_n: 抽样条数（控配额；每条 2 次逆地理）

    返回：{"判定","置信度","样本数","胜出A","胜出B","平均命中率A","平均命中率B",
           "样本明细","建议"}
    """
    import random as _rnd

    pts = list(points or [])
    if not pts:
        return {"判定": "无法判定", "原因": "无输入坐标"}

    idxs = list(range(len(pts)))
    if len(idxs) > sample_n:
        idxs = sorted(_rnd.Random(seed).sample(idxs, sample_n))

    win_a = win_b = n = 0
    sum_a = sum_b = 0.0
    detail = []

    for i in idxs:
        try:
            lon, lat = float(pts[i][0]), float(pts[i][1])
        except (TypeError, ValueError, IndexError):
            continue
        uaddr = (addrs[i] if addrs and i < len(addrs) else "") or ""
        toks = addr_tokens(uaddr)

        try:
            rg_a = amap_regeo(lon, lat)
            wlon, wlat = gcj02_to_wgs84(lon, lat)
            rg_b = amap_regeo(wlon, wlat)
        except Exception as e:                      # noqa: BLE001
            detail.append({"序号": i + 1, "结果": f"ERR {type(e).__name__}"})
            continue

        if not rg_a and not rg_b:
            detail.append({"序号": i + 1, "结果": "两次逆地理均无返回"})
            continue

        # 比对前统一去空格：用户地址常写「33 号」，逆地理返回「33号」，
        # 不去空格会永远不匹配（2026-10-23 实测踩中）。
        ta = ((rg_a.get("地址") or "") + (rg_a.get("格式化") or "")).replace(" ", "")
        tb = ((rg_b.get("地址") or "") + (rg_b.get("格式化") or "")).replace(" ", "")
        aa = (rg_a.get("区县") or "")
        ab = (rg_b.get("区县") or "")

        if toks:
            na = sum(1 for t in toks if t in ta)
            nb = sum(1 for t in toks if t in tb)
            sa, sb = na / len(toks), nb / len(toks)
            basis = f"地址 token {len(toks)} 个"
        elif area:
            sa = 1.0 if area in aa else 0.0
            sb = 1.0 if area in ab else 0.0
            basis = f"区县「{area}」比对"
        else:
            detail.append({"序号": i + 1,
                           "结果": "无地址列且无所属区县，无法比对"})
            continue

        n += 1
        sum_a += sa
        sum_b += sb
        if sa > sb:
            win_a += 1
        elif sb > sa:
            win_b += 1
        detail.append({"序号": i + 1, "判据": basis,
                       "命中率A_WGS84假设": round(sa, 2),
                       "命中率B_GCJ02假设": round(sb, 2),
                       "A地址": ta[:40], "B地址": tb[:40]})

    if n == 0:
        return {"判定": "无法判定", "原因": "全部样本无法比对（需地址列或所属区县）",
                "样本明细": detail}

    avg_a, avg_b = sum_a / n, sum_b / n
    if win_a > win_b:
        verdict = "WGS84"
    elif win_b > win_a:
        verdict = "GCJ-02"
    else:
        verdict = "无法判定（两假设打平）"

    gap = abs(avg_a - avg_b)
    if verdict.startswith("无法"):
        conf = "低"
    elif gap >= 0.30 and n >= 5:
        conf = "高"
    elif gap >= 0.15:
        conf = "中"
    else:
        conf = "低"

    out = {
        "判定": verdict, "置信度": conf, "样本数": n,
        "胜出A_WGS84假设": win_a, "胜出B_GCJ02假设": win_b,
        "平均命中率A": round(avg_a, 3), "平均命中率B": round(avg_b, 3),
        "样本明细": detail,
        "建议": ("可自动侦测，**不可自动决断**。请将本结果交用户拍板后再决定是否转换；"
                 "误判代价是整表偏移约 500 m。"),
    }
    if verbose:
        print(f"坐标系侦测：判定={verdict} 置信度={conf} "
              f"（A胜{win_a} / B胜{win_b}，命中率 {avg_a:.2f} vs {avg_b:.2f}）")
    return out


if __name__ == "__main__":
    self_test()
