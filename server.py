#!/usr/bin/env python3
# 修剪树枝作业 - 自建录入后端 (纯标准库, 无第三方依赖)
# 飞书多维表格作为数据库, 本服务持有 tenant_access_token 做写入/读取。
import os, sys, json, datetime, base64, secrets, re, time, gzip
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

# 和风天气（台风预警图层）：仅后端持有 KEY，前端经本服务代理访问
QWEATHER_HOST = os.environ.get("QWEATHER_HOST", "mu7jpk58te.re.qweatherapi.com")
QWEATHER_KEY = os.environ.get("QWEATHER_KEY", "")
_TYPHOON_CACHE = {"ts": 0.0, "data": None}

_token = None
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


def _num(x):
    try:
        return float(x)
    except Exception:
        return None


def qweather_get(path, params=None):
    """访问和风天气 API（自定义主机 + X-QW-Api-Key 鉴权）。和风默认 Gzip 压缩，需解压。"""
    qs = urllib.parse.urlencode(params or {})
    url = "https://" + QWEATHER_HOST + path + (("?" + qs) if qs else "")
    req = urllib.request.Request(url, method="GET")
    req.add_header("X-QW-Api-Key", QWEATHER_KEY)
    req.add_header("Accept-Encoding", "gzip")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, _decode_qw(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, _decode_qw(e)
        except Exception:
            return e.code, {"code": str(e.code)}
    except Exception as e:
        return 0, {"code": "0", "error": str(e)}


def _decode_qw(resp):
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode())


def get_token():
    global _token
    if _token:
        return _token
    s, o = api("POST", "/auth/v3/tenant_access_token/internal",
               {"app_id": APP_ID, "app_secret": APP_SECRET})
    _token = o.get("tenant_access_token")
    if not _token:
        raise RuntimeError("获取 token 失败: " + str(o))
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
        """代理和风天气台风数据：活跃台风列表→各自眼位/路径/风圈，10分钟缓存。"""
        if not QWEATHER_KEY:
            self._send(200, {"ok": False, "reason": "no_key",
                             "msg": "服务端未配置和风天气 API KEY（QWEATHER_KEY）"})
            return
        now = time.time()
        if _TYPHOON_CACHE["data"] is not None and now - _TYPHOON_CACHE["ts"] < 600:
            self._send(200, _TYPHOON_CACHE["data"])
            return
        year = datetime.date.today().year
        s, o = qweather_get("/v7/tropical/storm-list", {"basin": "NP", "year": year})
        active = []
        if o.get("code") == "200":
            for st in o.get("storm", []):
                if str(st.get("isActive")) in ("1", "2"):
                    active.append(st)
        storms = []
        for st in active[:5]:
            sid = st.get("id")
            item = {"id": sid, "name": st.get("name"), "track": [], "forecast": [], "now": None}
            s2, o2 = qweather_get("/v7/tropical/storm-track", {"stormid": sid})
            if o2.get("code") == "200":
                nw = o2.get("now") or {}
                item["now"] = {"lat": _num(nw.get("lat")), "lng": _num(nw.get("lon")),
                               "type": nw.get("type"), "pressure": nw.get("pressure"),
                               "windSpeed": nw.get("windSpeed"), "windRadius30": nw.get("windRadius30")}
                for p in o2.get("track", []):
                    item["track"].append({"lat": _num(p.get("lat")), "lng": _num(p.get("lon")), "time": p.get("time")})
            s3, o3 = qweather_get("/v7/tropical/storm-forecast", {"stormid": sid})
            if o3.get("code") == "200":
                for p in o3.get("forecast", []):
                    item["forecast"].append({"lat": _num(p.get("lat")), "lng": _num(p.get("lon")), "fxTime": p.get("fxTime")})
            storms.append(item)
        data = {"ok": True, "year": year, "storms": storms,
                "updateTime": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
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
        # 以下均需登录
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
        self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"[修剪作业录入] serving on http://0.0.0.0:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
