# -*- coding: utf-8 -*-
"""配置加载模块

设计原则（合规红线）：
  1. 本模块及整个仓库不内置任何 API Key
  2. Key 存放优先级：环境变量 > config.local.json > 空（提示用户配置）
  3. 日志与异常信息中 Key 一律脱敏

环境变量名：
  GDC_TD_KEY     天地图
  GDC_AMAP_KEY   高德
  GDC_BAIDU_KEY  百度
  GDC_QQ_KEY     腾讯
"""
import os
import json
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_LOCAL = ROOT / "config.local.json"

_ENV_KEYS = {
    "tianditu": "GDC_TD_KEY",
    "amap": "GDC_AMAP_KEY",
    "baidu": "GDC_BAIDU_KEY",
    "qq": "GDC_QQ_KEY",
}

_DEFAULT = {
    "keys": {"tianditu": "", "amap": "", "baidu": "", "qq": ""},
    "runtime": {
        # 各源日配额预算（可按需调整，超限即安全停机）
        "daily_cap": {"tianditu": 7000, "amap": 5000, "baidu": 5000, "qq": 5000},
        # QPS 限速：实际并发应取 QPS * 0.8
        "qps": {"tianditu": 8, "amap": 5, "baidu": 5, "qq": 5},
        "checkpoint_every": 50,   # 每 N 条落盘一次断点
        "timeout": 15,            # 单请求超时（秒）
        "retries": 3,             # 重试次数
        "backoff_base": 1.5,      # 退避基数
    },
    "output": {
        "default_crs": "WGS84",   # WGS84(工程) | GCJ02(发布)
    },
    "qc": {
        "cross_check_pass_m": 300,     # 交叉验真通过阈值
        "cross_check_suspect_m": 1000, # 存疑阈值（超过引第三源仲裁）
        "gov_dist_p95_km": 15,         # 距所属政府距离 P95 上限
        "sample_rate_init": 0.05,      # 初始抽样率
        "sample_rate_min": 0.02,
        "sample_rate_max": 0.15,
    },
}

_cache = None


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(reload: bool = False) -> dict:
    """加载配置（带缓存）。

    返回合并后的配置字典；Key 为空的源在调用时会给出明确提示。
    """
    global _cache
    if _cache is not None and not reload:
        return _cache

    cfg = json.loads(json.dumps(_DEFAULT))  # 深拷贝

    # 1) 本地配置文件
    if CONFIG_LOCAL.exists():
        try:
            with open(CONFIG_LOCAL, encoding="utf-8") as f:
                cfg = _deep_merge(cfg, json.load(f))
        except Exception as e:
            raise RuntimeError(
                f"配置文件解析失败：{CONFIG_LOCAL} —— {e}\n"
                f"请检查 JSON 格式，或删除该文件后重新初始化。"
            ) from e

    # 2) 环境变量覆盖（优先级最高）
    for name, env in _ENV_KEYS.items():
        val = os.environ.get(env)
        if val:
            cfg["keys"][name] = val.strip()

    _cache = cfg
    return cfg


def get_key(source: str) -> str:
    """取指定源的 Key；未配置时抛出明确的中文提示。"""
    cfg = load_config()
    key = (cfg["keys"].get(source) or "").strip()
    if not key:
        env = _ENV_KEYS.get(source, "")
        raise KeyError(
            f"未配置 {source} 的 API Key。\n"
            f"两种配置方式（任选其一）：\n"
            f"  方式一（推荐）：在本项目根目录创建 config.local.json，内容如：\n"
            f'      {{"keys": {{"{source}": "你的Key"}}}}\n'
            f"  方式二：设置环境变量 {env}=你的Key\n"
            f"注意：{source} 需使用【Web 服务 / 服务端】类型的 Key。"
        )
    return key


def mask_key(key: str) -> str:
    """Key 脱敏，用于日志输出。"""
    if not key:
        return "(空)"
    if len(key) <= 8:
        return key[:2] + "****"
    return key[:4] + "****" + key[-2:]


def has_key(source: str) -> bool:
    cfg = load_config()
    return bool((cfg["keys"].get(source) or "").strip())


def available_sources() -> list:
    """返回已配置 Key 的源列表（用于降级链排序）。"""
    return [s for s in ("tianditu", "amap", "baidu", "qq") if has_key(s)]


# ---------------------------------------------------------------- 网络层（代理自适应）

#: 缓存 (opener, 模式说明)，避免每次请求都探测代理
_OPENER = None


def _env_proxy() -> str:
    for name in ("GDC_PROXY", "https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        v = (os.environ.get(name) or "").strip()
        if v and not v.lower().startswith("socks"):
            return v
    return ""


def _proxy_alive(url: str, timeout: float = 1.5) -> bool:
    """探测代理端口是否有服务在监听（TCP 连接，不发 HTTP）。"""
    try:
        from urllib.parse import urlparse
        import socket
        u = urlparse(url if "://" in url else "http://" + url)
        host, port = u.hostname, u.port or 8080
        if not host:
            return False
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def http_opener(direct: bool | None = None, verbose: bool = False):
    """返回 urllib opener，并自动处理「失效代理导致全站超时」这一常见坑。

    选择顺序：
      1. 显式 direct=True → 强制直连
      2. 环境变量 GDC_PROXY / https_proxy / http_proxy → 先 TCP 探测，活的才用
      3. 探测失败或无代理配置 → 直连

    为什么必须探测：很多环境（含本机当前环境）注入了 `http_proxy=127.0.0.1:7960`
    但该端口并无服务，urllib 会默默走代理并卡到超时，表现和「禁网」一模一样。
    """
    global _OPENER
    if _OPENER is not None and direct is None:
        return _OPENER

    import ssl
    import urllib.request

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    proxy = "" if direct else _env_proxy()
    mode = "直连"
    if proxy:
        if _proxy_alive(proxy):
            mode = f"代理 {proxy}"
            handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        else:
            mode = f"直连（配置的代理 {proxy} 无响应，已自动跳过）"
            handler = urllib.request.ProxyHandler({})
    else:
        handler = urllib.request.ProxyHandler({})

    opener = urllib.request.build_opener(
        handler, urllib.request.HTTPSHandler(context=ctx)
    )
    if verbose:
        print(f"  [net] {mode}")
    if direct is None:
        _OPENER = opener
    return opener


def net_diagnose(verbose: bool = True) -> dict:
    """网络体检：代理是否可用、目标接口是否可达。排障第一步先跑这个。"""
    import time

    res = {"proxy_env": _env_proxy(), "proxy_alive": None, "mode": "", "targets": {}}
    if res["proxy_env"]:
        res["proxy_alive"] = _proxy_alive(res["proxy_env"])
    opener = http_opener(verbose=False)
    res["mode"] = ("代理 " + res["proxy_env"]) if res["proxy_alive"] else "直连"

    tests = {
        "baidu": "https://www.baidu.com",
        "mca": "https://dmfw.mca.gov.cn/",
        "stats": "https://www.stats.gov.cn/",
        "tianditu": "https://api.tianditu.gov.cn/",
    }
    for name, url in tests.items():
        t = time.time()
        try:
            r = opener.open(url, timeout=10)
            res["targets"][name] = f"HTTP {r.status} ({time.time() - t:.2f}s)"
        except urllib.error.HTTPError as e:
            # 能返回 HTTP 状态码就说明网络通（根路径无参数报 4xx 属正常）
            res["targets"][name] = f"可达 HTTP {e.code} ({time.time() - t:.2f}s)"
        except Exception as e:      # noqa: BLE001
            res["targets"][name] = f"FAIL {type(e).__name__}"

    if verbose:
        print("网络体检")
        print("-" * 50)
        print(f"  代理变量 : {res['proxy_env'] or '(未设置)'}")
        print(f"  端口探测 : {'可用' if res['proxy_alive'] else ('不可用 → 已跳过' if res['proxy_env'] else '(无需)')}")
        print(f"  实际出网 : {res['mode']}")
        for k, v in res["targets"].items():
            print(f"  {k:9s}: {v}")
        print(f"  Key 配置 : {available_sources() or '无（请先配置 Key）'}")
    return res


if __name__ == "__main__":
    import sys
    net_diagnose()
    sys.exit(0)
