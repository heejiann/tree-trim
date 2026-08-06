# 部署到 Render 免费档（公网访问指南）

适用场景：前期业务展示、一天用一两次。**永久免费**。
唯一缺点：连续 15 分钟无人访问，服务会"休眠"；下次打开需等待约 30 秒唤醒。白天连续作业基本无感。

---

## 部署后架构

```
浏览器（同事手机/电脑）
   │  https://你的网址.onrender.com
   ▼
Render 云上的 server.py（公网运行，持飞书凭证）
   │  用环境变量里的飞书 App ID / Secret 调开放接口
   ▼
飞书多维表格（真正的数据库，存电杆与作业记录）
```

飞书凭证只存在 Render 的「环境变量」里，**绝不在代码里**，所以上传 GitHub 也不会泄露。

---

## 第 1 步：注册两个免费账号（需你本人操作）

| 平台 | 地址 | 说明 |
|---|---|---|
| GitHub | https://github.com | 用来存代码，注册免费 |
| Render | https://render.com | 用来跑服务，用 GitHub 直接登录，免费档通常无需绑卡 |

> 这两步需要手机/邮箱验证，**我（AI）无法代你注册**，必须你本人来。

---

## 第 2 步：把代码传到 GitHub

准备代码：解压此前下载的 `web_app_deploy.zip`（已自动排除 `.env`，凭证不会泄露）。

**方式 A（不会 git 也能做）：**
1. GitHub 网页新建仓库（名字随意，如 `tree-trim`，Public/Private 均可）
2. 进入仓库 → 把解压出来的**所有文件**直接拖到上传区 → 写个提交信息提交

**方式 B（会用 git）：**
```
cd web_app_deploy
git init
git add .
git commit -m "init"
git remote add origin <你的仓库地址>
git push -u origin main
```

> 注意：压缩包里**没有 `.env`**，飞书密钥不会上传，安全。

---

## 第 3 步：在 Render 新建 Web Service

1. Render Dashboard → **New → Web Service**
2. 连接你的 GitHub 仓库（首次会请求授权，允许即可）
3. 配置：
   - **Name**：`tree-trim`（随意）
   - **Runtime**：Python 3（自动识别，无需改）
   - **Region**：选离你近的（如 Singapore）
   - **Branch**：`main`（或你提交的分支）
   - **Build Command**：**留空**（只用 Python 标准库，无依赖）
   - **Start Command**：`python server.py`
   - **Instance Type**：**Free**（免费档）
4. 先别急着部署，先填环境变量（下一步）。

---

## 第 4 步：填写环境变量（关键！）

在 Render 的 **Environment** 区域，逐个添加：

| 变量名 | 值 | 说明 |
|---|---|---|
| `FEISHU_APP_ID` | 你本地 `.env` 里的 `FEISHU_APP_ID` | 打开电脑上 `web_app/.env` 复制，勿外泄 |
| `FEISHU_APP_SECRET` | 你本地 `.env` 里的 `FEISHU_APP_SECRET` | 同上 |
| `FEISHU_APP_TOKEN` | `FSiabEYY6ae0Gss7BD6cfLHNnrh` | 已默认，照填即可 |
| `LOGIN_USER` | `admin` | 登录账号 |
| `LOGIN_PASS` | **改成一个强密码** | ⚠️ 公网谁都能试，别用 `senyo2026` |
| `PORT` | （不填） | Render 自动注入，`server.py` 会自动读取 |

> 前两项的值在你电脑的 `web_app/.env` 文件里，直接复制粘贴过来即可。

---

## 第 5 步：部署

点 **Create Web Service**（或 Deploy）。等待 1~2 分钟，日志出现 `serving on` 即成功。
Render 会分配一个网址，形如 `https://tree-trim.onrender.com`，复制到浏览器打开即可。

---

## 使用与维护

- **免费档休眠**：15 分钟无访问后休眠，首次打开慢约 30 秒，属正常。
- **飞书数据**：不用动，数据库在飞书云上。
- **`.env`**：永远只在你本地，绝不上传。
- **改登录密码**：修改 Render 环境变量 `LOGIN_PASS` → 手动触发一次 Redeploy。

---

## 后期升档（业务起来后）

- 在 Render 把 **Instance Type** 从 Free 升级到付费档 → 去休眠、7×24 在线。
- 或迁移到 **Railway Hobby（$5/月，含 $5 资源额度，常驻不休眠）**。
- 代码无需改动，只改部署平台与环境变量即可。

---

## 安全提醒

- 任何情况下不要把 `.env` 提交到 GitHub 或发给他人。
- 飞书 `App Secret` 一旦泄露，立即到飞书开放平台重置。
