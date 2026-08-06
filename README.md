# 修剪树枝作业 · 自建录入应用 (web_app)

飞书多维表格继续当数据库（电杆主数据库 + 修剪作业记录 + 公式预警 + 地图分布图全部保留），
本目录是一个**自建 H5 录入应用**，套在「飞书工作台自建应用」里，员工在飞书内点开即可填写，
UI 完全自定义、移动端友好，且能做成「搜杆 → 建杆 → 记作业」一体化流程。

## 架构
```
员工(飞书 H5 / 手机浏览器)
   │  fetch
   ▼
web_app/server.py  (轻量后端, 持有 tenant_access_token)
   │  飞书开放 API
   ▼
飞书多维表格 (电杆主数据库 + 修剪作业记录)
```

## 本地运行（先看效果，不需要飞书壳）
```bash
cd web_app
python3 server.py
# 浏览器打开 http://localhost:8000，用账号登录
```
凭证（飞书 + 登录账号）已写在 `web_app/.env`，本机测试直接用；部署到公网请改用平台环境变量。
手机同局域网可用 `http://<你电脑IP>:8000` 访问体验。

## 登录
- 系统有登录页（SENYO logo + “修剪树枝管理系统”），账号密码登录。
- 默认账号：`admin` / 密码：`senyo2026`（见 `.env` 的 `LOGIN_USER` / `LOGIN_PASS`，**部署后务必修改**）。
- 当前为单账号、不分角色：登录后即可使用「地图管理」与「作业记录」两个板块。
- 会话用内存 session（HttpOnly Cookie），服务重启需重新登录。

## 两个板块
1. **地图管理**：高德底图加载全部电杆标注，顶部按编号搜索过滤；点击标注弹出详情（编号/供电所/状态/描述），
   列出该杆全部作业记录，并提供「高德导航」「百度导航」跳转（自动把 GPS 坐标转成对应地图坐标）。
2. **作业记录**：原「搜杆 → 建杆 → 记作业」录入端，支持三级供电所目录、地图选点、照片上传。

## 接口
- `POST /api/login`           登录（{user, pass}）→ Set-Cookie
- `GET  /api/me`             返回当前登录用户（未登录 401）
- `POST /api/logout`          退出
- `GET  /api/poles?q=关键词`  搜电杆（按编号模糊匹配，需登录）
- `GET  /api/poles/all`       全部电杆（含 GPS 经纬度、供电所、状态，用于地图标注，需登录）
- `GET  /api/poles/<rid>/jobs` 某电杆的全部作业记录（需登录）
- `POST /api/poles`           新建电杆  body: {pole_no, lng, lat, desc, area}
- `POST /api/jobs`            新建作业  body: {pole_record_id, ticket, job_date, worker, tree, risk, branch, sign, note, photos?}（photos: {before,after,station,attach: [{name, data(base64)}]}）

## 部署到公网（让同事/班组随时访问）

本项目已自带 `Procfile` / `requirements.txt` / `runtime.txt`，后端监听 `0.0.0.0:$PORT`，
飞书凭证全部从环境变量读取，**无需改代码**即可部署。

### 方式一：Railway（推荐，免费额度够几人用）
1. 注册 railway.app（免费档约 $5/月额度，可能需绑卡验证，通常不扣费）。
2. 装 CLI：`npm i -g @railway/cli`，然后 `railway login`（浏览器授权一次）。
3. 在本目录执行 `railway init` → 选 Empty Project → `railway up` 直接把本地目录传上去。
   （也可把本目录推到 GitHub 私有仓库，在 Railway 控制台连仓库部署。）
4. 控制台 → Variables 添加：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_APP_TOKEN`（可选，代码有默认值）。
   `PORT` 由 Railway 自动注入，不用管。
5. 部署完拿到 `xxx.up.railway.app` 域名。

### 方式二：Render（免费 Web Service，闲置会休眠）
1. 注册 render.com，新建 Web Service，连 GitHub 仓库（需先把本目录推到 GitHub）。
2. Build Command 留空 / `pip install -r requirements.txt`，Start Command：`python server.py`。
3. 同上加环境变量 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_APP_TOKEN`。
4. 拿到 `xxx.onrender.com` 域名（免费档闲置后首次访问约等 30s 冷启动）。

### 接到飞书工作台（可选，同事在飞书里打开）
1. 飞书开放平台 → 你的自建应用 → 应用功能 → 网页 → H5 主页 URL 填上面域名。
2. 安全设置 → H5 可信域名 → 加上该域名。
3. 权限管理 → 申请 `bitable:app` 等；应用可见范围勾作业班组；发布审核。
4. ⚠️ 若飞书应用开了「IP 白名单 / 可信 IP」策略，需把部署服务器的公网出口 IP 加入，否则飞书 API 调用会被拒（出口 IP 可在部署平台查到）。

### 内网 / 临时共享（免部署）
- 同办公室连同一 WiFi，同事用你电脑内网 IP `http://192.168.x.x:8000` 访问（需你电脑常开、服务在跑）。
- 或用 `ngrok http 8000` 生成临时公网链接（免费档链接会变、依赖本机常开）。

## 已知限制 / 后续
- 照片上传（修剪前/后/站班会/附件）已完成：后端走飞书 `drive/v1/medias/upload_all` 写附件字段，前端可拍照/选照。
- 「作业人员」当前为手填；若接飞书 JSSDK 免登，可自动带出登录人姓名（用户已选该方向）。
- 主库「下次预计修剪/台风预警」依赖「最近修剪日期」，目前手填；界面加 lookup/rollup 可自动随作业更新（API 白名单不含 19，需在飞书界面加）。
