#!/usr/bin/env python3
# 修剪树枝作业 - 自建录入后端 (纯标准库, 无第三方依赖)
# 飞书多维表格作为数据库, 本服务持有 tenant_access_token 做写入/读取。
import os, sys, json, datetime, base64, secrets, re, time
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


def list_all(table, limit=500):
    """分页拉取全部记录（飞书 page_size 上限 100，超出需翻页）。"""
    t = get_token()
    out, token = [], None
    while len(out) < limit:
        sz = min(100, limit - len(out))
        url = f"/bitable/v1/apps/{APP_TOKEN}/tables/{table}/records?page_size={sz}"
        if token:
            url += "&page_token=" + token
        s, o = api("GET", url, token=t)
        if s // 100 != 2:
            sys.stderr.write(f"[list_all] {table} GET {s}: {str(o)[:300]}\n"); sys.stderr.flush()
        items = o.get("data", {}).get("items", [])
        out.extend(items)
        token = o.get("data", {}).get("page_token")
        if not token or not items:
            break
    return out


def create_record(table, fields):
    t = get_token()
    s, o = api("POST", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table}/records",
               {"fields": to_ids(table, fields)}, token=t)
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
        with urllib.request.urlopen(req, timeout=60) as r:
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
            q = urllib.parse.parse_qs(u.query).get("q", [""])[0].strip()
            id2name = {v: k for k, v in fmap(MASTER).items()}
            out = []
            seen = {}  # 电杆编号 -> 在 out 中的下标，用于去重
            for r in list_all(MASTER):
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
                item = {"record_id": r["record_id"], "pole_no": no,
                        "desc": fv.get("位置描述", ""), "loc": (loc.get("location") if isinstance(loc, dict) else loc),
                        "lng": lng, "lat": lat,
                        "area": fv.get("供电所全称", ""), "status": fv.get("电杆状态", "")}
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
            self._send(200, {"items": out})
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
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
