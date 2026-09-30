#!/usr/bin/env python3
# 修剪树枝作业 - 自建录入后端 (纯标准库, 无第三方依赖)
# 飞书多维表格作为数据库, 本服务持有 tenant_access_token 做写入/读取。
import os, sys, json, csv, datetime, base64, secrets, re, time, threading
import urllib.request, urllib.error, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def _load_env(path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v

_load_env()  # 本地测试：从 .env 读取飞书凭证（不依赖进程环境变量）

APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
APP_TOKEN = os.environ.get("FEISHU_APP_TOKEN", "FSiabEYY6ae0Gss7BD6cfLHNnrh")
MASTER = os.environ.get("TABLE_MASTER", "tblJ8fUv7M6Qczwm")   # 电杆主数据库
JOB = os.environ.get("TABLE_JOB", "tblRNU3FSXidthDh")          # 修剪作业记录
SURVEY = os.environ.get("TABLE_SURVEY", "tblBToYTHkasOirS")     # 勘察记录
PORT = int(os.environ.get("PORT", "8000"))
HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "https://open.feishu.cn/open-apis"

LOGIN_USER = os.environ.get("LOGIN_USER", "admin")
LOGIN_PASS = os.environ.get("LOGIN_PASS", "admin123")
SESSIONS = {}  # session_id -> 用户名（内存会话，重启后失效，需重新登录）

# 中央气象台台风网（台风预警图层）：免费、免 key、国内直连，替代付费的和风天气
CMA_TYPHOON_LIST = "http://typhoon.nmc.cn/weatherservice/typhoon/jsons/list_default"
CMA_TYPHOON_VIEW = "http://typhoon.nmc.cn/weatherservice/typhoon/jsons/view_{sid}"
# 浙江水利厅（兜底源）：免费、免 key、纯 JSON，字段比 CMA 更清晰；注意官方拼写 TyhoonActivity 少个 p
ZJ_TYPHOON_LIST = "https://typhoon.slt.zj.gov.cn/Api/TyhoonActivity"
ZJ_TYPHOON_VIEW = "https://typhoon.slt.zj.gov.cn/Api/TyphoonInfo/{tfid}"
_CMA_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
_TYPHOON_CACHE = {"ts": 0.0, "data": None}

_token = None
_token_exp = 0.0   # token 过期的单调时钟时间点（见 get_token）
_fmap = {}


def api(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"error": str(e)}
    except Exception as e:
        # 网络异常（超时 / 代理不可达 / DNS 等）一律降级为失败元组，避免向上抛异常拖垮导出
        return 0, {"error": str(e)}


def _num(x):
    try:
        return float(x)
    except Exception:
        return None


def _cma_jsonp(url, timeout=20):
    """请求中央气象台台风接口（返回 JSONP 包装，剥壳成 dict）。免费、免 key、免鉴权。"""
    req = urllib.request.Request(url, method="GET", headers=_CMA_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "ignore")
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("CMA 返回格式异常: " + raw[:120])
    return json.loads(raw[i:j + 1])


def _zj_json(url, timeout=20):
    """请求浙江水利厅台风接口（纯 JSON，可能返回 dict 或 list）。免费、免 key、免鉴权。"""
    req = urllib.request.Request(url, method="GET", headers=_CMA_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def _cma_typhoons():
    """中央气象台（主源）：活跃台风列表→各自眼位/路径/风圈/预报。返回 storms 列表。"""
    storms = []
    lst = _cma_jsonp(CMA_TYPHOON_LIST)
    for t in lst.get("typhoonList", []):
        if not isinstance(t, list) or len(t) < 8:
            continue
        sid, en, cn, status = t[0], t[1], t[2], t[7]
        if status != "start":  # 只取活跃台风
            continue
        try:
            d = _cma_jsonp(CMA_TYPHOON_VIEW.format(sid=sid))
        except Exception:
            continue
        tf = d.get("typhoon")
        # 实测结构：{"typhoon":[元信息..., "start", [路径点数组], [其他]]}
        points = tf[8] if isinstance(tf, list) and len(tf) >= 9 and isinstance(tf[8], list) else []
        if not points:
            continue
        track, last = [], None
        for p in points:
            if not isinstance(p, list) or len(p) < 8:
                continue
            lat, lng = _num(p[5]), _num(p[4])
            if lat is not None and lng is not None:
                track.append({"lat": lat, "lng": lng,
                              "time": p[1] if isinstance(p[1], str) else ""})
            last = p
        if not track or not isinstance(last, list):
            continue
        # 风圈：取 30KTS（7 级风圈），顺序 NE/SE/SW/NW
        wind30 = None
        radii = last[10] if len(last) > 10 else None
        if isinstance(radii, list):
            for r in radii:
                if isinstance(r, list) and len(r) >= 5 and str(r[0]).upper() == "30KTS":
                    wind30 = {"neRadius": _num(r[1]), "seRadius": _num(r[2]),
                              "swRadius": _num(r[3]), "nwRadius": _num(r[4])}
                    break
        # 预报：BABJ（中央气象台）
        forecast = []
        fc = last[11] if len(last) > 11 else None
        if isinstance(fc, dict):
            for arr in fc.values():
                if not isinstance(arr, list):
                    continue
                for fp in arr:
                    if isinstance(fp, list) and len(fp) >= 4:
                        flat, flng = _num(fp[3]), _num(fp[2])
                        if flat is not None and flng is not None:
                            forecast.append({"lat": flat, "lng": flng,
                                             "fxTime": str(fp[1]) if len(fp) > 1 else ""})
        now_info = {"lat": _num(last[5]), "lng": _num(last[4]),
                    "type": last[3] if len(last) > 3 else "",
                    "pressure": _num(last[6]) if len(last) > 6 else None,
                    "windSpeed": _num(last[7]) if len(last) > 7 else None,
                    "windRadius30": wind30}
        storms.append({"id": str(sid), "name": cn or en or str(sid),
                       "track": track, "forecast": forecast, "now": now_info})
    return storms


def _zj_typhoons():
    """浙江水利厅（兜底源）：活跃台风列表→各自眼位/路径/风圈/预报。返回 storms 列表。"""
    storms = []
    lst = _zj_json(ZJ_TYPHOON_LIST)
    if not isinstance(lst, list):
        return storms
    for t in lst:
        tfid = t.get("tfid")
        name = t.get("name") or t.get("enname") or str(tfid)
        try:
            d = _zj_json(ZJ_TYPHOON_VIEW.format(tfid=tfid))
        except Exception:
            continue
        points = d.get("points") or []
        track = []
        for p in points:
            lat, lng = _num(p.get("lat")), _num(p.get("lng"))
            if lat is not None and lng is not None:
                track.append({"lat": lat, "lng": lng, "time": p.get("time") or ""})
        if not track:
            continue
        last = points[-1]
        # 7 级风圈：radius7 形如 "280|220|200|380"，实测顺序为 东北|东南|西北|西南
        # （第3、4位是西北、西南，与 CMA 的 NE/SE/SW/NW 顺序对调），需换位映射
        wind30 = None
        r7 = last.get("radius7")
        if isinstance(r7, str) and r7:
            parts = r7.split("|")
            if len(parts) >= 4:
                wind30 = {"neRadius": _num(parts[0]), "seRadius": _num(parts[1]),
                          "swRadius": _num(parts[3]), "nwRadius": _num(parts[2])}
        # 预报：优先"中国"（中央气象台）机构
        forecast = []
        for fc in last.get("forecast") or []:
            if not isinstance(fc, dict):
                continue
            if fc.get("tm") == "中国":
                for fp in fc.get("forecastpoints") or []:
                    flat, flng = _num(fp.get("lat")), _num(fp.get("lng"))
                    if flat is not None and flng is not None:
                        forecast.append({"lat": flat, "lng": flng, "fxTime": fp.get("time") or ""})
                break
        now_info = {"lat": _num(last.get("lat")), "lng": _num(last.get("lng")),
                    "type": last.get("strong") or "",
                    "pressure": _num(last.get("pressure")),
                    "windSpeed": _num(last.get("speed")),
                    "windRadius30": wind30}
        storms.append({"id": str(tfid), "name": name,
                       "track": track, "forecast": forecast, "now": now_info})
    return storms


def get_token():
    """获取 tenant_access_token，带过期续期。

    飞书 token 有效期约 2 小时（响应里的 expire，秒）。旧实现只判 `if _token` 就永久复用，
    导致常驻进程（launchd 本地服务 / 8010 验证服 / Render 长时间不休眠的实例）启动约 2 小时后
    所有飞书接口都返回 400 / code 99991663「Invalid access token」——重启才能恢复。
    这里改成按 expire 提前 10 分钟自动续期。
    """
    global _token, _token_exp
    now = time.monotonic()
    if _token and now < _token_exp:
        return _token
    s, o = api("POST", "/auth/v3/tenant_access_token/internal",
               {"app_id": APP_ID, "app_secret": APP_SECRET})
    tok = o.get("tenant_access_token")
    if not tok:
        # 续期失败时不要清掉旧 token 之外的任何状态；直接报错让上层看到真实原因
        raise RuntimeError("获取 token 失败: " + str(o))
    _token = tok
    try:
        expire = int(o.get("expire") or 7200)
    except Exception:
        expire = 7200
    _token_exp = now + max(expire - 600, 60)   # 提前 10 分钟续期，下限 60 秒
    return _token


def fmap(table):
    if table in _fmap:
        return _fmap[table]
    t = get_token()
    s, o = api("GET", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table}/fields", token=t)
    m = {}
    for f in o.get("data", {}).get("items", []):
        m[f["field_name"]] = f["field_id"]
    _fmap[table] = m
    return m


def to_ids(table, d):
    # 飞书 records 写入接口以“字段名”为 key，无需转 id
    return d


def list_records(table, size=100):
    t = get_token()
    s, o = api("GET", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table}/records?page_size={size}", token=t)
    return o.get("data", {}).get("items", [])


_list_cache = {}   # table -> (取数时间, items)
_STALE = set()     # 被写操作标记「待刷新」的表
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
_PAGE_SIZE = 500   # 实测飞书 bitable records 单页上限就是 500
_REFRESHING = set()
_CACHE_LOCK = threading.Lock()


def _cache_file(table):
    return os.path.join(_CACHE_DIR, "list_%s.json" % table)


def _load_disk(table):
    """读磁盘缓存，返回 (ts, items) 或 None。"""
    try:
        with open(_cache_file(table), encoding="utf-8") as f:
            b = json.load(f)
        items = b.get("items")
        if isinstance(items, list) and items:
            return float(b.get("ts") or 0), items
    except Exception:
        pass
    return None


def _save_disk(table, ts, items):
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = _cache_file(table) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": ts, "items": items}, f, ensure_ascii=False)
        os.replace(tmp, _cache_file(table))     # 原子替换，避免读到半截文件
    except Exception as e:
        sys.stderr.write(f"[cache] 写缓存失败 {table}: {e}\n")


def _fetch_all(table):
    """真·全量拉取（500/页）。电杆主表 4547 条约 10 次请求。"""
    t = get_token()
    out, token = [], None
    while True:
        url = f"/bitable/v1/apps/{APP_TOKEN}/tables/{table}/records?page_size={_PAGE_SIZE}"
        if token:
            url += "&page_token=" + token
        s, o = api("GET", url, token=t)
        if s // 100 != 2:
            sys.stderr.write(f"[list_all] {table} GET {s}: {str(o)[:300]}\n"); sys.stderr.flush()
            break
        items = o.get("data", {}).get("items", [])
        out.extend(items)
        token = o.get("data", {}).get("page_token")
        if not token or not items:
            break
    return out


def refresh_async(table):
    """后台静默刷新缓存（同一张表同时只跑一个线程）。"""
    with _CACHE_LOCK:
        if table in _REFRESHING:
            return False
        _REFRESHING.add(table)

    def work():
        try:
            items = _fetch_all(table)
            if items:
                ts = time.time()
                with _CACHE_LOCK:
                    _list_cache[table] = (ts, items)
                _save_disk(table, ts, items)
        except Exception as e:
            sys.stderr.write(f"[refresh] {table}: {e}\n")
        finally:
            with _CACHE_LOCK:
                _REFRESHING.discard(table)

    threading.Thread(target=work, daemon=True).start()
    return True


def list_all(table, limit=None, ttl=600):
    """分页拉取记录。limit=None 表示全量。

    性能说明（2026-09-29 实测）：飞书单页请求本身就要 1.4~5s，与代理无关；
    旧实现 page_size=100 → 电杆表 4547 条要串行翻 46 页 ≈ 90~230s，
    TTL 一到、点一下级联下拉就要卡一分钟。现改三层：
      ① 内存缓存（TTL 600s）
      ② 磁盘缓存 .cache/list_<table>.json —— 跨进程重启依然可用
      ③ 兜底才真全量拉（page_size=500 → 10 页）
    命中磁盘但已过期/被写入标记为脏时：**先返回旧数据 + 后台静默刷新**，
    请求永远不会阻塞在飞书网络上。
    """
    now = time.time()
    if limit is not None:
        # 小量拉取（如最近 N 条）不值得缓存，保持单页直取
        t = get_token()
        s, o = api("GET", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table}/records?page_size={max(1, min(_PAGE_SIZE, limit))}", token=t)
        if s // 100 != 2:
            return []
        return o.get("data", {}).get("items", [])[:limit]

    c = _list_cache.get(table)
    if c and now - c[0] < ttl and table not in _STALE:
        return c[1]

    if not c:
        d = _load_disk(table)
        if d:
            with _CACHE_LOCK:
                _list_cache[table] = d
            if now - d[0] >= ttl or table in _STALE:
                _STALE.discard(table)
                refresh_async(table)     # 先拿旧的顶上，后台换新
            return d[1]

    if c:
        _STALE.discard(table)
        refresh_async(table)             # 有过期数据：先用它的，后台刷新
        return c[1]

    # 首次：内存、磁盘都没有，只能同步全量（一次性成本）
    items = _fetch_all(table)
    if items:
        ts = time.time()
        with _CACHE_LOCK:
            _list_cache[table] = (ts, items)
        _save_disk(table, ts, items)
        return items
    return []


def patch_cached(table, record_id, fields):
    """把一次写操作就地合并进缓存，避免为看一条改动重拉 10 页。

    飞书返回的 fields key 是 field_id，传入的是 field_name，需转换。
    「位置地图」是 Location 字段（写字符串、读 dict），跳过不打补丁，
    按 loc 字段计算的坐标会走「经度/纬度」文本字段，不受影响。
    """
    if not fields:
        return
    try:
        n2id = fmap(table)
    except Exception:
        return
    conv = {}
    for k, v in fields.items():
        if k == "位置地图":
            continue
        conv[n2id.get(k, k)] = v
    with _CACHE_LOCK:
        c = _list_cache.get(table)
        if not c:
            return
        ts, items = c
        for r in items:
            if r.get("record_id") == record_id:
                r.setdefault("fields", {}).update(conv)
                break
        else:
            _STALE.add(table)    # 没命中（可能是新记录）→ 交给下次后台刷新
            return
        nts = time.time()
        _list_cache[table] = (nts, items)
        _save_disk(table, nts, items)


def append_cached(table, record_id, fields):
    """新增记录后就地追加进缓存。

    否则「刚新增的杆」要等下一次后台全量刷新才出现在下拉里（体感像"没保存成功"）。
    Location 字段跳过：写字符串、读回来是 dict，格式不同。
    """
    if not fields:
        return
    try:
        n2id = fmap(table)
    except Exception:
        n2id = {}
    conv = {n2id.get(k, k): v for k, v in fields.items() if k != "位置地图"}
    with _CACHE_LOCK:
        c = _list_cache.get(table)
        if not c:
            c = _load_disk(table)
            if not c:
                _STALE.add(table)     # 缓存不可用 → 交给后台刷新
                return
        ts, items = c
        items.append({"record_id": record_id, "fields": conv})
        nts = time.time()
        _list_cache[table] = (nts, items)
        _STALE.discard(table)
    _save_disk(table, nts, items)


# ---------- 组织层级：供电局 / 区局 / 供电所 ----------
# 主表「供电所全称」里已经存了完整三级，格式固定为 `供电局 / 区局 / 供电所`：
#   佛山供电局 / 南海供电局 / 丹灶供电所
# 所以级联不需要新建字段，直接把这个字段拆成三级即可。
_ORG_SEP = re.compile(r"\s*[/、>＞]\s*")


def org_split(full):
    """供电所全称 → (供电局, 区局, 供电所)。容忍只写一段/两段的情况。"""
    parts = [x.strip() for x in _ORG_SEP.split(str(full or "").strip()) if x.strip()]
    if not parts:
        return ("", "", "")
    if len(parts) >= 3:
        return (parts[0], parts[1], parts[2])
    if len(parts) == 2:
        # 两段：末段是「所」→ 缺区局；否则按 局/区局 处理
        if parts[1].endswith("供电所"):
            return (parts[0], "", parts[1])
        return (parts[0], parts[1], "")
    p = parts[0]
    if p.endswith("供电所"):        # 只写了所
        return ("", "", p)
    return (p, "", "")              # 只写了局


def org_join(bureau, area, office):
    """三级 → 供电所全称字符串（只保留非空段，用 ' / ' 连接）"""
    return " / ".join([x.strip() for x in (bureau, area, office) if str(x or "").strip()])


# ---------- 新增电杆：编号规则（必须与 build_feishu_import.py 已导入的 4547 条一致） ----------
def _sub_short(sub):
    """丹灶变电站 → 丹灶站"""
    s = (sub or "").strip()
    if s.endswith("变电站"):
        return s[:-3] + "站"
    if s.endswith("站"):
        return s
    return (s + "站") if s else ""


def _line_core(line):
    """线路字段值 701良登线 → 良登（剥前导数字编号，再去尾部「线/#」）"""
    s = re.sub(r"^\d+", "", (line or "").strip())
    return re.sub(r"[线#]+$", "", s).strip()


def _norm_digits(d):
    """杆号输入归一化 → '#7'。接受 7 / #7 / 7号 / 第7杆。"""
    s = re.sub(r"[#＃号第杆\s]", "", str(d or "")).strip()
    if not s or not re.match(r"^[0-9A-Za-z\-/]+$", s):
        return ""
    return "#" + s


def _taqu_ok(t):
    """新增台区名质量闸门：挡掉「竹径台区#13公用台变」这类垃圾（同导入时的口径）。"""
    t = (t or "").strip()
    if not t or len(t) > 14:
        return ""
    if re.search(r"[#＃]|公用|台变|配变|电站|箱变|开关站", t):
        return ""
    return t


def _branch_ok(b):
    """支线名质量闸门：挡掉空值/垃圾输入。允许「三眼桥支线」或直接「三眼桥」。"""
    b = (b or "").strip()
    if not b or len(b) > 16:
        return ""
    if re.search(r"[#＃]|台区|台变|配变|公用|电站|箱变|开关站|10kV", b):
        return ""
    return b


def build_pole_no(sub, line, taqu, digits, branch=""):
    """拼完整编号 → 丹灶站10kV良登线利恒兴支线竹径台区#7杆
    支线段插在线路后台区前（与全库 1500+ 条含支线编号同规则）；无支线则省略。"""
    base = "%s10kV%s线" % (_sub_short(sub), _line_core(line))
    bg = _branch_ok(branch)
    if bg:
        base += bg if bg.endswith("支线") else bg + "支线"
    tg = _taqu_ok(taqu)
    if tg:
        base += tg if tg.endswith("台区") else tg + "台区"
    d = _norm_digits(digits)
    return (base + d + "杆") if d else ""


# ---- 支线候选：编号 → 支线名 映射（来自图纸提取的 poles_master + import 清单） ----
_BRANCH_SRC = os.path.join(
    "/Users/kimho/WorkBuddy/砍树勘察聊天文件录入系统", "poles_master.csv")
_BRANCH_IMP = os.path.join(
    "/Users/kimho/WorkBuddy/砍树勘察聊天文件录入系统", "feishu_poles_import.json")
_NO2BRANCH = None          # 惰性加载：电杆编号 → 支线名
_BRANCH_TS = 0.0


def no2branch_map(force=False):
    """编号→支线映射。图纸支线挂错会重提，源文件变了（1小时内）就自动重载。"""
    global _NO2BRANCH, _BRANCH_TS
    if _NO2BRANCH is not None and not force and time.time() - _BRANCH_TS < 3600:
        return _NO2BRANCH
    m = {}
    try:
        pid2br = {}
        with open(_BRANCH_SRC, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                pid = (r.get("杆位ID") or "").strip()
                br = (r.get("支线") or "").strip()
                if pid and br:
                    pid2br[pid] = br
        if os.path.exists(_BRANCH_IMP):
            with open(_BRANCH_IMP, encoding="utf-8") as f:
                for x in json.load(f):
                    no = (x.get("电杆编号") or "").strip()
                    br = pid2br.get((x.get("_polesid") or "").strip())
                    if no and br:
                        m[no] = br
    except Exception as e:
        sys.stderr.write("no2branch_map failed: %r\n" % e)
    if m:
        _NO2BRANCH, _BRANCH_TS = m, time.time()
    return _NO2BRANCH or {}


def _fget(fields, n2id, name):
    """取字段值：缓存里 key 是 field_id，但兼容直接存 field_name 的情况。"""
    fid = n2id.get(name, name)
    v = fields.get(fid)
    return fields.get(name) if v is None else v


def find_pole_by_no(pole_no):
    """按编号精确查找（strip 比较），返回 record_id 或 None。用于新增时拦重号。"""
    target = (pole_no or "").strip()
    if not target:
        return None
    try:
        n2id = fmap(MASTER)
    except Exception:
        n2id = {}
    for r in list_all(MASTER):
        if str(_fget(r.get("fields") or {}, n2id, "电杆编号") or "").strip() == target:
            return r.get("record_id")
    return None


def inherit_area(sub, line):
    """新增的杆没有「供电所全称」，从同线路已有杆继承一个，避免字段空着。"""
    try:
        n2id = fmap(MASTER)
    except Exception:
        n2id = {}
    for r in list_all(MASTER):
        f = r.get("fields") or {}
        if (str(_fget(f, n2id, "变电站") or "") == sub
                and str(_fget(f, n2id, "线路") or "") == line):
            a = _fget(f, n2id, "供电所全称")
            if a:
                return a
    return ""


def _rid_missing(o):
    """飞书报「record_id 不存在」（1254043 RecordIdNotFound）。

    出现它基本意味着：这条记录在飞书 App/网页里被删掉了，而本机缓存还没跟上。
    要当成「数据已变化」处理（刷缓存 + 友好提示），而不是把一长串飞书原始错误丢给用户。
    """
    s = str(o or "")
    return "1254043" in s or "RecordIdNotFound" in s


def drop_cache(table=None, hard=False):
    """写操作后让记录缓存失效。

    hard=False（默认）：只清内存 + 标记为脏，磁盘留作兜底 → 下次请求先返回旧数据、
                        再后台刷新。写定位这类「改一条」不会让用户又等一次全量拉。
    hard=True：连磁盘一起删（大批量导入后用），下次请求重新全量拉。
    """
    tables = list(_list_cache.keys()) if table is None else [table]
    if table is None:
        with _CACHE_LOCK:
            _list_cache.clear()
        _STALE.update(tables)
    else:
        with _CACHE_LOCK:
            _list_cache.pop(table, None)
        _STALE.add(table)
    if hard:
        for tb in tables:
            try:
                os.remove(_cache_file(tb))
            except Exception:
                pass


def create_record(table, fields):
    t = get_token()
    s, o = api("POST", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table}/records",
               {"fields": to_ids(table, fields)}, token=t)
    if s // 100 == 2 and o.get("code") == 0:
        rid = ((o.get("data") or {}).get("record") or {}).get("record_id")
        if rid:
            append_cached(table, rid, fields)   # 就地追加：新建的杆立刻可在下拉/地图看到
        else:
            drop_cache(table)
    return s, o


def update_record(table, record_id, fields):
    """按 record_id 更新字段（v1 用 PUT /records/{record_id}）。

    成功后就地给缓存打补丁：改一条记录不必重拉整表（10 页 ≈ 15s），
    现场连续给多根杆打定位时，下拉里的「已定位」标记也能立刻正确。
    """
    t = get_token()
    s, o = api("PUT", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table}/records/{record_id}",
               {"fields": to_ids(table, fields)}, token=t)
    if s // 100 == 2 and o.get("code") == 0:
        patch_cached(table, record_id, fields)
    return s, o


def upload_file(table, filename, raw):
    """上传到飞书 drive 媒体接口（多维表格附件专用），返回 file_token（失败返回 None）。"""
    ext = os.path.splitext(filename)[1].lower()
    parent_type = "bitable_image" if ext in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp") else "bitable_file"
    boundary = "----wb" + os.urandom(6).hex()
    b = boundary.encode()
    body = b""
    body += b"--" + b + b"\r\n"
    body += b'Content-Disposition: form-data; name="file_name"\r\n\r\n' + filename.encode() + b"\r\n"
    body += b"--" + b + b"\r\n"
    body += b'Content-Disposition: form-data; name="parent_type"\r\n\r\n' + parent_type.encode() + b"\r\n"
    body += b"--" + b + b"\r\n"
    body += b'Content-Disposition: form-data; name="parent_node"\r\n\r\n' + APP_TOKEN.encode() + b"\r\n"
    body += b"--" + b + b"\r\n"
    body += b'Content-Disposition: form-data; name="size"\r\n\r\n' + str(len(raw)).encode() + b"\r\n"
    body += b"--" + b + b"\r\n"
    body += b'Content-Disposition: form-data; name="file"; filename="' + filename.encode() + b'"\r\n'
    body += b"Content-Type: application/octet-stream\r\n\r\n"
    body += raw + b"\r\n"
    body += b"--" + b + b"--\r\n"
    req = urllib.request.Request(BASE + "/drive/v1/medias/upload_all", data=body, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    req.add_header("Authorization", "Bearer " + get_token())
    try:
        # 强制直连飞书 drive，绕过本地/沙箱 HTTP 代理（多部分上传经代理易失败）
        _op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with _op.open(req, timeout=60) as r:
            o = json.loads(r.read().decode())
        if o.get("code") == 0:
            return o.get("data", {}).get("file_token")
        return None
    except Exception:
        return None


def upload_b64(filename, b64):
    try:
        return upload_file(JOB, filename, base64.b64decode(b64))
    except Exception:
        return None


def photos_to_attach(items):
    out = []
    for it in (items or []):
        if isinstance(it, dict) and it.get("data"):
            tok = upload_b64(it.get("name", "photo.jpg"), it["data"])
            if tok:
                out.append({"file_token": tok})
    return out


def attaches_to_list(val):
    """飞书附件字段读取为 [{file_token,name}] 列表，供前端展示。"""
    out = []
    for x in (val or []):
        if isinstance(x, dict):
            tok = x.get("file_token") or ""
            if not tok and x.get("url"):
                # 极少数情况下只返回临时 url，转交前端直连
                tok = x["url"]
            if tok:
                out.append({"file_token": tok, "name": x.get("name", "")})
    return out


def fetch_attachment(ftok):
    """按 file_token 取飞书附件原始字节（图片/文件均可）。失败返回 None。
    供验收资料生成器嵌入“修剪前/后照片”等使用；与 _photo 共用同一临时下载链路口径。"""
    if not ftok:
        return None
    try:
        t = get_token()
    except Exception:
        return None
    s, o = api("GET", f"/drive/v1/medias/batch_get_tmp_download_url?file_tokens={ftok}", token=t)
    url = None
    if s // 100 == 2:
        items = (o.get("data") or {}).get("tmp_download_urls") or []
        for it in items:
            if it.get("file_token") == ftok:
                url = it.get("tmp_download_url")
                break
        if not url and items:
            url = items[0].get("tmp_download_url")
    if not url:
        return None
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except Exception:
        return None


def today_ms():
    d = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp() * 1000)


def date_to_ms(s):
    try:
        y, m, d = [int(x) for x in s.split("-")]
        return int(datetime.datetime(y, m, d).timestamp() * 1000)
    except Exception:
        return today_ms()


def ms_to_date(ms):
    try:
        return datetime.datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d")
    except Exception:
        return ""


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj=None, html=None):
        self.send_response(code)
        if html is not None:
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())
            return
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode())

    def _do_export(self, data):
        f = data.get("filters", {}) or {}
        selected = data.get("selected", None)  # 勾选的 record_id 列表（可空=全部）
        items = self._acceptance_jobs(
            f.get("start", ""), f.get("end", ""), f.get("city", ""),
            f.get("bureau", ""), f.get("station", ""), f.get("line", ""), f.get("risk", ""))
        if selected:
            wanted = set(selected)
            items = [it for it in items if it.get("record_id") in wanted]
        meta = data.get("meta", {}) or {}
        import subprocess, tempfile
        inj = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                          suffix=".json", prefix="acc_")
        json.dump({"items": items, "meta": meta}, inj, ensure_ascii=False)
        inj.close()
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        outp = os.path.join(tempfile.gettempdir(), "acceptance_%s.zip" % stamp)
        gen = os.path.join(HERE, "acceptance_docx.py")
        py = sys.executable
        if not os.path.exists(gen):
            self._send(500, {"error": "验收生成器缺失"})
            return
        try:
            # 子进程剥离代理环境变量，直连飞书（避免沙箱/本地代理导致附件 CDN 拉取超时拖垮导出）
            _env = os.environ.copy()
            for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                       "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
                _env.pop(_k, None)
            p = subprocess.run([py, gen, "--package", inj.name, outp],
                               capture_output=True, text=True, timeout=240, env=_env)
        except Exception as e:
            self._send(500, {"error": "生成进程异常", "detail": str(e)})
            return
        if p.returncode != 0 or not os.path.isfile(outp):
            self._send(500, {"error": "验收资料生成失败", "detail": (p.stderr or "")[-2000:]})
            return
        with open(outp, "rb") as fh:
            zbuf = fh.read()
        try:
            os.remove(inj.name); os.remove(outp)
        except Exception:
            pass
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        # 注意：http.server 的 send_header 只能编码 latin-1，中文文件名会抛 UnicodeEncodeError。
        # 按 RFC 6266 给 ASCII 回退名 + filename*=UTF-8'' 的真实名，两者都保留中文可读性。
        _fn = "验收资料_%s.zip" % stamp
        self.send_header("Content-Disposition",
                         "attachment; filename=\"acceptance_%s.zip\"; filename*=UTF-8''%s"
                         % (stamp, urllib.parse.quote(_fn)))
        self.send_header("Content-Length", str(len(zbuf)))
        self.end_headers()
        self.wfile.write(zbuf)

    def _acceptance_jobs(self, start, end, city, bureau, station, line, risk):
        """按组合条件筛选作业记录（验收板块用）。返回带电杆信息的作业列表。"""
        id2name_m = {v: k for k, v in fmap(MASTER).items()}
        id2name_j = {v: k for k, v in fmap(JOB).items()}
        # 电杆主数据 -> 编号/所属/线路/描述
        pole = {}
        for r in list_all(MASTER):
            fv = {id2name_m.get(k, k): v for k, v in r.get("fields", {}).items()}
            full = fv.get("供电所全称", "") or ""
            pole[r["record_id"]] = {
                "no": fv.get("电杆编号", ""),
                "area": full,
                "desc": fv.get("位置描述", ""),
                "line": fv.get("线路", fv.get("所属线路", "")),
            }
        out = []
        for r in list_all(JOB):
            fv = {id2name_j.get(k, k): v for k, v in r.get("fields", {}).items()}
            relids = []
            for x in (fv.get("关联电杆") or []):
                if isinstance(x, dict):
                    for kk in ("record_ids", "record_id"):
                        vv = x.get(kk)
                        if isinstance(vv, list):
                            relids.extend(vv)
                        elif vv:
                            relids.append(vv)
                elif isinstance(x, str):
                    relids.append(x)
            pinfo = {}
            for pr in relids:
                if pr in pole:
                    pinfo = pole[pr]
                    break
            full = pinfo.get("area", "")
            # 区域筛选：供电所全称包含所选市局/区局/供电所
            if city and city not in full:
                continue
            if bureau and bureau not in full:
                continue
            if station and station not in full:
                continue
            # 线路模糊：线路名 或 电杆编号 或 描述 含关键字
            if line:
                hay = " ".join([str(pinfo.get("line", "")), str(pinfo.get("no", "")), str(pinfo.get("desc", ""))])
                if line.lower() not in hay.lower():
                    continue
            d = ms_to_date(fv.get("作业日期", 0))
            if start and d and d < start:
                continue
            if end and d and d > end:
                continue
            rk = fv.get("修剪前隐患等级", "")
            if risk and rk != risk:
                continue
            out.append({
                "record_id": r["record_id"],
                "ticket": fv.get("工作票编号", ""),
                "date": d,
                "worker": fv.get("作业人员", ""),
                "tree": fv.get("树木品种", ""),
                "risk": rk,
                "branch": fv.get("剪下树枝量", ""),
                "sign": fv.get("标示牌状态", ""),
                "note": fv.get("备注", ""),
                "pole_no": pinfo.get("no", ""),
                "pole_area": pinfo.get("area", ""),
                "pole_desc": pinfo.get("desc", ""),
                "before": attaches_to_list(fv.get("修剪前照片")),
                "after": attaches_to_list(fv.get("修剪后照片")),
                "station_photos": attaches_to_list(fv.get("站班会情况")),
                "attach": attaches_to_list(fv.get("附件")),
            })
        out.sort(key=lambda x: x["date"] or "", reverse=True)
        return out

    def _path(self):
        # Python http.server 把请求行按 iso-8859-1 解码；若 URL 含直接写的中文，需转回 UTF-8
        return self.path.encode("iso-8859-1").decode("utf-8", "replace")

    def _auth(self):
        c = self.headers.get("Cookie", "")
        m = re.search(r"session=([\w-]+)", c)
        if m and m.group(1) in SESSIONS:
            return SESSIONS[m.group(1)]
        return None

    def _photo(self, ftok):
        """代理飞书附件图片：用 bitable 附件临时下载链接接口换取直链后流式返回。
        该接口无需 drive 额外权限，规避了鉴权/CORS/链接过期问题。"""
        t = get_token()
        s, o = api("GET", f"/drive/v1/medias/batch_get_tmp_download_url?file_tokens={ftok}", token=t)
        url = None
        if s // 100 == 2:
            items = (o.get("data") or {}).get("tmp_download_urls") or []
            for it in items:
                if it.get("file_token") == ftok:
                    url = it.get("tmp_download_url")
                    break
            if not url and items:
                url = items[0].get("tmp_download_url")
        if not url:
            self._send(404, {"error": "photo not found"})
            return
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            ct = r.headers.get("Content-Type") or "image/jpeg"
        except Exception as e:
            self._send(502, {"error": "fetch photo failed: " + str(e)})
            return
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Cache-Control", "public, max-age=300")
        self.end_headers()
        self.wfile.write(data)

    def _typhoon(self):
        """代理台风数据（双源兜底）：中央气象台优先，失败自动回退浙江水利厅，10分钟缓存。"""
        now = time.time()
        if _TYPHOON_CACHE["data"] is not None and now - _TYPHOON_CACHE["ts"] < 600:
            self._send(200, _TYPHOON_CACHE["data"])
            return
        year = datetime.date.today().year
        storms, source, err = [], "", ""
        try:
            storms = _cma_typhoons()
            if not storms:
                raise ValueError("中央气象台无活跃台风")
            source = "cma"
        except Exception as e:
            err = "中央气象台: " + str(e)
            try:
                storms = _zj_typhoons()
                if storms:
                    source = "zhejiang"
                else:
                    err += " | 浙江水利厅: 无活跃台风"
            except Exception as e2:
                err += " | 浙江水利厅: " + str(e2)
        if storms:
            data = {"ok": True, "year": year, "source": source, "storms": storms,
                    "updateTime": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
        else:
            data = {"ok": False, "reason": "upstream",
                    "msg": "台风数据源暂不可用：" + err, "storms": []}
        _TYPHOON_CACHE["ts"] = now
        _TYPHOON_CACHE["data"] = data
        self._send(200, data)

    def do_GET(self):
        u = urllib.parse.urlparse(self._path())
        # 公开：静态资源、首页、健康检查
        if u.path.startswith("/static/"):
            rel = u.path[len("/static/"):].lstrip("/")
            fp = os.path.normpath(os.path.join(HERE, "static", rel))
            base = os.path.normpath(os.path.join(HERE, "static"))
            if os.path.isfile(fp) and fp.startswith(base + os.sep):
                fn = os.path.basename(fp)
                ext = os.path.splitext(fn)[1].lower().lstrip(".")
                ct = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                      "gif": "image/gif", "css": "text/css", "js": "application/javascript",
                      "svg": "image/svg+xml"}.get(ext, "application/octet-stream")
                with open(fp, "rb") as f:
                    self.send_response(200)
                    self.send_header("Content-Type", ct)
                    self.end_headers()
                    self.wfile.write(f.read())
                return
            self._send(404, {"error": "not found"})
            return
        if u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
                self._send(200, html=f.read())
            return
        if u.path == "/api/health":
            self._send(200, {"ok": True})
            return
        # 台风预警图层：公开气象数据，免登录（避免每次部署后所有人都需重新登录）
        if u.path == "/api/weather/typhoon":
            self._typhoon()
            return
        if u.path == "/api/me":
            user = self._auth()
            if not user:
                self._send(401, {"error": "未登录"})
                return
            self._send(200, {"user": user})
            return
        # 以下均需登录；地图数据 /api/poles/all 免登录，避免部署后未登录导致地图空白、误判未更新
        if u.path != "/api/poles/all":
            if not self._auth():
                self._send(401, {"error": "未登录"})
                return
        if u.path == "/api/poles" or u.path == "/api/poles/all":
            # /api/poles/all 供地图页：只返回「已定位」的杆。
            # 导入 DXF 后主表 4500+ 条，全量推到前端会拖垮地图与搜索下拉；
            # 地图本来也只画有坐标的点，未定位的杆等现场加了定位自然出现。
            only_located = (u.path == "/api/poles/all")
            q = urllib.parse.parse_qs(u.query).get("q", [""])[0].strip()
            id2name = {v: k for k, v in fmap(MASTER).items()}
            all_recs = list_all(MASTER)
            out = []
            seen = {}  # 电杆编号 -> 在 out 中的下标，用于去重
            for r in all_recs:
                raw = r.get("fields", {})
                fv = {id2name.get(k, k): v for k, v in raw.items()}
                no = fv.get("电杆编号", "")
                if q and q.lower() not in str(no).lower():
                    continue
                loc = fv.get("位置地图", "")
                lat = lng = None
                # 优先用 经度/纬度 文本字段（WGS84，与选点一致，便于导航转换）
                try:
                    if fv.get("经度") not in (None, ""): lng = float(fv["经度"])
                    if fv.get("纬度") not in (None, ""): lat = float(fv["纬度"])
                except Exception:
                    pass
                # 回退：地理位置字段的 location 字符串（GCJ02）
                if (lat is None or lng is None) and isinstance(loc, dict) and loc.get("location"):
                    try:
                        a, b = loc["location"].split(","); lng = float(a); lat = float(b)
                    except Exception:
                        pass
                if only_located and (lat is None or lng is None):
                    continue
                item = {"record_id": r["record_id"], "pole_no": no,
                        "desc": fv.get("位置描述", ""), "loc": (loc.get("location") if isinstance(loc, dict) else loc),
                        "lng": lng, "lat": lat,
                        "area": fv.get("供电所全称", ""), "status": fv.get("电杆状态", ""),
                        "sub": fv.get("变电站", ""), "line": fv.get("线路", ""), "taqu": fv.get("台区", ""),
                        "source": fv.get("定位来源", "")}
                # 按电杆编号去重：编号相同的多条记录只保留一条（优先带坐标的）
                key = (no or "").strip()
                if key:
                    if key in seen:
                        if out[seen[key]]["lng"] is None and lng is not None:
                            out[seen[key]] = item
                        continue
                    seen[key] = len(out)
                    out.append(item)
                else:
                    out.append(item)
            self._send(200, {"items": out, "total": len(all_recs),
                             "located": sum(1 for x in out if x.get("lng") is not None)})
            return
        # 选杆加定位：级联下拉数据源（供电所→线路→台区→杆号）
        if u.path == "/api/poles/pick":
            qs = urllib.parse.parse_qs(u.query)
            g = lambda k: qs.get(k, [""])[0].strip()
            # 七级级联：供电局 → 区局 → 供电所 → 变电站 → 线路 → 台区 → 电杆
            # org=0 可跳过组织三级（等价于旧的四级行为，供旧调用方兜底）
            use_org = g("org") != "0"
            bureau, area, office = g("bureau"), g("area"), g("office")
            sub, line, taqu, kw = g("sub"), g("line"), g("taqu"), g("q")
            id2name = {v: k for k, v in fmap(MASTER).items()}
            rows = []
            for r in list_all(MASTER):
                fv = {id2name.get(k, k): v for k, v in r.get("fields", {}).items()}
                bu, ar, of = org_split(fv.get("供电所全称"))
                rows.append((r["record_id"], fv, bu, ar, of))
            if use_org:
                # 口径与验收导出板块保持一致：在「供电所全称」里做包含匹配
                if bureau:
                    rows = [x for x in rows if bureau in str(x[1].get("供电所全称") or "")]
                if area:
                    rows = [x for x in rows if area in str(x[1].get("供电所全称") or "")]
                if office:
                    rows = [x for x in rows if office in str(x[1].get("供电所全称") or "")]
            if sub:
                rows = [x for x in rows if x[1].get("变电站", "") == sub]
            if line:
                rows = [x for x in rows if x[1].get("线路", "") == line]
            if taqu:
                rows = [x for x in rows if x[1].get("台区", "") == taqu]
            # 逐级返回：没选到哪一级，就返回下一级的候选
            # 例外：前端要「不限台区、直接看该线全部杆」时带 list=1
            want_poles = (g("list") == "1")
            if use_org and not bureau:
                self._send(200, {"level": "bureau", "items": sorted({x[2] for x in rows if x[2]})})
                return
            if use_org and not area:
                self._send(200, {"level": "area", "items": sorted({x[3] for x in rows if x[3]})})
                return
            if use_org and not office:
                self._send(200, {"level": "office", "items": sorted({x[4] for x in rows if x[4]})})
                return
            if not sub:
                self._send(200, {"level": "sub", "items": sorted({x[1].get("变电站", "") for x in rows if x[1].get("变电站")})})
                return
            if not line:
                self._send(200, {"level": "line", "items": sorted({x[1].get("线路", "") for x in rows if x[1].get("线路")})})
                return
            if not taqu and not want_poles:
                self._send(200, {"level": "taqu", "items": sorted({x[1].get("台区", "") for x in rows if x[1].get("台区")})})
                return
            if kw:
                kl = kw.lower()
                rows = [x for x in rows if kl in str(x[1].get("电杆编号", "")).lower()]
            out = []
            for rid, fv, bu, ar, of in rows:
                lng = lat = None
                try:
                    if fv.get("经度") not in (None, ""): lng = float(fv["经度"])
                    if fv.get("纬度") not in (None, ""): lat = float(fv["纬度"])
                except Exception:
                    pass
                loc = fv.get("位置地图")
                if (lng is None or lat is None) and isinstance(loc, dict) and loc.get("location"):
                    try:
                        a, b = loc["location"].split(","); lng = float(a); lat = float(b)
                    except Exception:
                        pass
                out.append({"record_id": rid, "pole_no": fv.get("电杆编号", ""),
                            "desc": fv.get("位置描述", ""), "lng": lng, "lat": lat,
                            "source": fv.get("定位来源", ""),
                            "office_full": org_join(bu, ar, of)})
            out.sort(key=lambda x: x["pole_no"])
            self._send(200, {"level": "pole", "items": out})
            return
        # 新增电杆：预拼编号（图纸漏登的直线杆/支线杆用）+ 即时查重
        if u.path == "/api/poles/preview-no":
            qs = urllib.parse.parse_qs(u.query)
            g = lambda k: (qs.get(k, [""])[0] or "").strip()
            no = build_pole_no(g("sub"), g("line"), g("taqu"), g("digits"),
                               branch=g("branch"))
            dup = find_pole_by_no(no) if no else None
            self._send(200, {"pole_no": no, "dup": bool(dup), "dup_record_id": dup})
            return
        # 新增电杆用：该线路下已有支线名候选（来自图纸提取，编号→支线映射）
        if u.path == "/api/poles/branches":
            qs = urllib.parse.parse_qs(u.query)
            g = lambda k: (qs.get(k, [""])[0] or "").strip()
            sub, line, taqu = g("sub"), g("line"), g("taqu")
            n2id = fmap(MASTER)
            id2name = {v: k for k, v in n2id.items()}
            m = no2branch_map()
            brs = set()
            for r in list_all(MASTER):
                fv = {id2name.get(k, k): v for k, v in r.get("fields", {}).items()}
                if sub and fv.get("变电站", "") != sub:
                    continue
                if line and fv.get("线路", "") != line:
                    continue
                if taqu and fv.get("台区", "") != taqu:
                    continue
                br = m.get(str(fv.get("电杆编号") or "").strip())
                if br:
                    brs.add(br)
            self._send(200, {"branches": sorted(brs)})
            return
        m = re.match(r"^/api/poles/([\w-]+)/jobs$", u.path)
        if m:
            rid = m.group(1)
            id2name = {v: k for k, v in fmap(JOB).items()}
            out = []
            for r in list_all(JOB):
                fv = {id2name.get(k, k): v for k, v in r.get("fields", {}).items()}
                # 双向关联字段读取为对象列表：{"record_ids":[...],"text":...}
                relids = []
                for x in (fv.get("关联电杆") or []):
                    if isinstance(x, dict):
                        for kk in ("record_ids", "record_id"):
                            vv = x.get(kk)
                            if isinstance(vv, list): relids.extend(vv)
                            elif vv: relids.append(vv)
                    elif isinstance(x, str):
                        relids.append(x)
                if rid in relids:
                    out.append({"record_id": r["record_id"],
                                "ticket": fv.get("工作票编号", ""),
                                "date": ms_to_date(fv.get("作业日期", 0)),
                                "worker": fv.get("作业人员", ""),
                                "tree": fv.get("树木品种", ""),
                                "risk": fv.get("修剪前隐患等级", ""),
                                "sign": fv.get("标示牌状态", ""),
                                "note": fv.get("备注", ""),
                                "before": attaches_to_list(fv.get("修剪前照片")),
                                "after": attaches_to_list(fv.get("修剪后照片")),
                                "station": attaches_to_list(fv.get("站班会情况")),
                                "attach": attaches_to_list(fv.get("附件"))})
            out.sort(key=lambda x: x["date"] or "", reverse=True)
            self._send(200, {"items": out})
            return
        m = re.match(r"^/api/poles/([\w-]+)/surveys$", u.path)
        if m:
            rid = m.group(1)
            id2name = {v: k for k, v in fmap(SURVEY).items()}
            out = []
            for r in list_all(SURVEY):
                fv = {id2name.get(k, k): v for k, v in r.get("fields", {}).items()}
                relids = []
                for x in (fv.get("关联电杆") or []):
                    if isinstance(x, dict):
                        for kk in ("record_ids", "record_id"):
                            vv = x.get(kk)
                            if isinstance(vv, list): relids.extend(vv)
                            elif vv: relids.append(vv)
                    elif isinstance(x, str):
                        relids.append(x)
                if rid in relids:
                    out.append({"record_id": r["record_id"],
                                "date": ms_to_date(fv.get("勘察时间", 0)),
                                "env": fv.get("现场环境", ""),
                                "estimate": fv.get("工程量预估", ""),
                                "method": fv.get("作业方式", ""),
                                "note": fv.get("备注", ""),
                                "photos": attaches_to_list(fv.get("现场照片", []))})
            out.sort(key=lambda x: x["date"] or "", reverse=True)
            self._send(200, {"items": out})
            return
        m = re.match(r"^/api/photo/([\w.\-]+)$", u.path)
        if m:
            self._photo(m.group(1))
            return
        if u.path == "/api/acceptance/preview":
            qs = urllib.parse.parse_qs(u.query)
            g = lambda k: qs.get(k, [""])[0].strip()
            items = self._acceptance_jobs(g("start"), g("end"), g("city"),
                                          g("bureau"), g("station"), g("line"), g("risk"))
            self._send(200, {"items": items, "total": len(items)})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlparse(self._path())
        ln = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(ln) if ln else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except Exception:
            data = {}
        # 登录（公开）
        if u.path == "/api/login":
            user = data.get("user", ""); pas = data.get("pass", "")
            if user == LOGIN_USER and pas == LOGIN_PASS:
                sid = secrets.token_hex(16)
                SESSIONS[sid] = user
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Set-Cookie", "session=%s; HttpOnly; Path=/; Max-Age=86400" % sid)
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "user": user}, ensure_ascii=False).encode())
            else:
                self._send(401, {"error": "账号或密码错误"})
            return
        # 退出（需登录）
        if u.path == "/api/logout":
            c = self.headers.get("Cookie", "")
            mm = re.search(r"session=([\w-]+)", c)
            if mm:
                SESSIONS.pop(mm.group(1), None)
            self._send(200, {"ok": True})
            return
        # 其余均需登录
        if not self._auth():
            self._send(401, {"error": "未登录"})
            return
        if u.path == "/api/poles":
            no = data.get("pole_no", "").strip()
            if not no:
                self._send(400, {"error": "电杆编号必填"})
                return
            lng, lat = data.get("lng", ""), data.get("lat", "")
            fields = {"电杆编号": no, "位置描述": data.get("desc", ""),
                      "供电所全称": data.get("area", ""),
                      "电杆状态": data.get("status", "正常运行"),
                      "定位来源": data.get("source", "现场定位"),
                      "首次录入日期": today_ms()}
            if lng and lat:
                try:
                    lngf, latf = float(lng), float(lat)
                    fields["经度"] = str(lngf)
                    fields["纬度"] = str(latf)
                    fields["位置地图"] = f"{lngf},{latf}"
                except Exception:
                    pass
            s, o = create_record(MASTER, fields)
            if s // 100 != 2 or o.get("code") != 0:
                self._send(500, {"error": o.get("msg") or "写入失败", "detail": o})
                return
            rid = o.get("data", {}).get("record", {}).get("record_id")
            self._send(200, {"record_id": rid, "pole_no": no})
            return
        # 新增电杆定位：给主表里已存在的杆写真实坐标（选杆→加定位，不再手输编号）
        if u.path == "/api/poles/locate":
            rid = str(data.get("record_id", "")).strip()
            if not rid:
                self._send(400, {"error": "请先选择电杆"})
                return
            lng, lat = data.get("lng"), data.get("lat")
            if lng in (None, "") or lat in (None, ""):
                self._send(400, {"error": "请在地图上选点或使用当前位置"})
                return
            try:
                lngf, latf = float(lng), float(lat)
            except Exception:
                self._send(400, {"error": "经纬度格式不正确"})
                return
            if not (-180.0 <= lngf <= 180.0 and -90.0 <= latf <= 90.0):
                self._send(400, {"error": "经纬度超出有效范围"})
                return
            fields = {"经度": str(lngf), "纬度": str(latf),
                      "位置地图": f"{lngf},{latf}",
                      "定位来源": (data.get("source") or "现场定位")}
            if data.get("desc"):
                fields["位置描述"] = str(data["desc"]).strip()
            s, o = update_record(MASTER, rid, fields)
            if s // 100 != 2 or o.get("code") != 0:
                if _rid_missing(o):
                    drop_cache(MASTER)
                    refresh_async(MASTER)
                    self._send(409, {"error": "这根杆在飞书里已被删除，主数据正在重新同步，请刷新后重新选择"})
                    return
                self._send(500, {"error": o.get("msg") or "写入失败", "detail": o})
                return
            self._send(200, {"ok": True, "record_id": rid, "lng": lngf, "lat": latf})
            return
        # 新增电杆 + 同时打定位：补录图纸漏登的直线杆（如闩门线 #7）
        if u.path == "/api/poles/create-locate":
            bureau = str(data.get("bureau", "")).strip()
            area = str(data.get("area", "")).strip()
            office = str(data.get("office", "")).strip()
            sub = str(data.get("sub", "")).strip()
            line = str(data.get("line", "")).strip()
            taqu = str(data.get("taqu", "")).strip()
            branch = str(data.get("branch", "")).strip()
            digits = str(data.get("digits", "")).strip()
            if not sub or not line:
                self._send(400, {"error": "请先选择变电站和线路"})
                return
            try:
                auto = int(data.get("auto", 1))
            except Exception:
                auto = 1
            pole_no = build_pole_no(sub, line, taqu, digits, branch=branch) if auto else ""
            if not pole_no:
                pole_no = str(data.get("pole_no", "")).strip()
            if not pole_no:
                self._send(400, {"error": "请填写杆号，如 7"})
                return
            dup = find_pole_by_no(pole_no)
            if dup:
                refresh_async(MASTER)   # 缓存可能滞后（刚在飞书里删过记录），顺手刷新
                self._send(409, {"error": "该编号已存在：%s，请直接从列表里选它" % pole_no,
                                 "dup": True, "record_id": dup})
                return
            lng, lat = data.get("lng"), data.get("lat")
            if lng in (None, "") or lat in (None, ""):
                self._send(400, {"error": "请在地图上选点或使用当前位置"})
                return
            try:
                lngf, latf = float(lng), float(lat)
            except Exception:
                self._send(400, {"error": "经纬度格式不正确"})
                return
            if not (-180.0 <= lngf <= 180.0 and -90.0 <= latf <= 90.0):
                self._send(400, {"error": "经纬度超出有效范围"})
                return
            fields = {"电杆编号": pole_no, "变电站": sub, "线路": line,
                      "电杆状态": "正常运行",
                      "经度": str(lngf), "纬度": str(latf),
                      "位置地图": "%s,%s" % (lngf, latf),
                      "定位来源": str(data.get("source") or "现场定位·新增电杆"),
                      "首次录入日期": today_ms()}
            tg = _taqu_ok(taqu)
            if tg:
                fields["台区"] = tg
            # 供电所全称：优先用前端选中的组织三级拼装，否则从同线路已有杆继承
            org_full = org_join(bureau, area, office) or inherit_area(sub, line)
            if org_full:
                fields["供电所全称"] = org_full
            if data.get("desc"):
                fields["位置描述"] = str(data["desc"]).strip()
            s2, o2 = create_record(MASTER, fields)
            if s2 // 100 != 2 or o2.get("code") != 0:
                self._send(500, {"error": o2.get("msg") or "新增失败", "detail": o2})
                return
            rid = ((o2.get("data") or {}).get("record") or {}).get("record_id")
            self._send(200, {"ok": True, "created": True, "record_id": rid,
                             "pole_no": pole_no, "lng": lngf, "lat": latf})
            return
        # 手动同步电杆主数据：有人在飞书里直接改/删过数据后用（全量重拉，约 10 秒）
        if u.path == "/api/poles/sync":
            t0 = time.time()
            items = _fetch_all(MASTER)
            if not items:
                self._send(500, {"error": "同步失败：没取到数据，请检查飞书凭证或网络"})
                return
            ts = time.time()
            with _CACHE_LOCK:
                _list_cache[MASTER] = (ts, items)
            _save_disk(MASTER, ts, items)
            _STALE.discard(MASTER)
            self._send(200, {"ok": True, "total": len(items),
                             "seconds": round(time.time() - t0, 1)})
            return
        if u.path == "/api/jobs":
            rid = data.get("pole_record_id", "")
            if not rid:
                self._send(400, {"error": "请先选择或新建电杆"})
                return
            photos = data.get("photos", {}) or {}
            fields = {"工作票编号": data.get("ticket", ""),
                      "关联电杆": [rid],   # 双向关联字段(type21)：字符串数组
                      "作业日期": date_to_ms(data.get("job_date", "")),
                      "作业人员": data.get("worker", ""),
                      "树木品种": data.get("tree", ""),
                      "修剪前隐患等级": data.get("risk", ""),
                      "剪下树枝量": data.get("branch", ""),
                      "标示牌状态": data.get("sign", ""),
                      "备注": data.get("note", "")}
            b = photos_to_attach(photos.get("before"))
            if b: fields["修剪前照片"] = b
            a = photos_to_attach(photos.get("after"))
            if a: fields["修剪后照片"] = a
            st = photos_to_attach(photos.get("station"))
            if st: fields["站班会情况"] = st
            at = photos_to_attach(photos.get("attach"))
            if at: fields["附件"] = at
            s, o = create_record(JOB, fields)
            if s // 100 != 2 or o.get("code") != 0:
                self._send(500, {"error": o.get("msg") or "写入失败", "detail": o})
                return
            self._send(200, {"ok": True})
            return
        if u.path == "/api/surveys":
            rid = data.get("pole_record_id", "")
            if not rid:
                self._send(400, {"error": "请先选择或新建电杆"})
                return
            fields = {"关联电杆": [rid],   # 双向关联字段(type21)：字符串数组
                      "勘察时间": date_to_ms(data.get("survey_date", "")),
                      "现场环境": data.get("env", ""),
                      "工程量预估": data.get("estimate", ""),
                      "作业方式": data.get("method", ""),
                      "备注": data.get("note", "")}
            ph = photos_to_attach(data.get("photos"))
            if ph:
                fields["现场照片"] = ph
            s, o = create_record(SURVEY, fields)
            if s // 100 != 2 or o.get("code") != 0:
                self._send(500, {"error": o.get("msg") or "写入失败", "detail": o})
                return
            self._send(200, {"ok": True})
            return
        if u.path == "/api/acceptance/export":
            try:
                self._do_export(data)
            except Exception as e:
                import traceback as _tb
                self._send(500, {"error": "导出异常", "detail": repr(e), "trace": _tb.format_exc()[-1500:]})
            return
        self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"[修剪作业录入] serving on http://0.0.0.0:{PORT}")
    # 启动即后台预热电杆主表缓存：用户第一次点级联下拉就是毫秒级，
    # 而不是在请求里干等 10 页飞书（约 15s）。预热有磁盘缓存兜底，重启用不上。
    def _warm():
        for tb in (MASTER, SURVEY):
            try:
                n = len(list_all(tb))
                fmap(tb)     # 字段名→id 映射也一起预热，否则重启后首次请求仍要等 ~1s
                print(f"[预热] {tb}: {n} 条")
            except Exception as e:
                sys.stderr.write(f"[预热失败] {tb}: {e}\n")
    threading.Thread(target=_warm, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
