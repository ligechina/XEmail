# XEmail 服务器更新说明（不使用 Git，保留用户数据）

适用环境：

- 服务器地址：`123.57.60.164`
- 项目目录：`/opt/XEmail`
- 目标：**仅推送代码更新**，不把本地 `data/` 用户数据（邮件、账号、密钥、附件）传到服务器，也不覆盖服务器上已有的 `data/`

> 设计前提：发布包的打包脚本 (`scripts/package_release.sh`) 主动排除 `data/`、`.env`、`.venv/`、`.claude/`、`.git/` 等敏感/本地目录，并在打包末尾做一次"泄漏检查"——如果检测到上述路径混进了 tar 包，会立即报错并删掉残包。服务器侧的更新脚本再用 `rsync --exclude` 兜一道保险。

---

## 1. 本地打包（在本地电脑终端执行）

```bash
cd /path/to/XEmail   # 替换为你本地的项目根目录

# 生成新的发布包；包名格式：dist/xemail-release-YYYYMMDD_HHMMSS.tar.gz
bash scripts/package_release.sh
```

脚本会在末尾打印发布包路径，并预览前 30 条文件，方便肉眼检查。

### 1.1 打包后预检（强烈推荐）

把包传出去前，主动验证一次包里**没有**任何用户数据 / 本地状态：

```bash
PKG=$(ls -1t dist/xemail-release-*.tar.gz | head -n 1)
echo "$PKG"

# 这条命令必须输出空。任何输出都意味着包里混进了私有路径，应停止上传。
tar -tzf "$PKG" | grep -E '^\./(data/|\.env$|\.venv/|\.claude/|\.git/)' || echo "OK: no private paths"
```

打包脚本内部已经做了同样的检查，正常情况下不会让有问题的包落到 `dist/`；这里只是双保险。

---

## 2. 上传发布包与更新脚本（本地终端）

```bash
PKG=$(ls -1t dist/xemail-release-*.tar.gz | head -n 1)

# 把发布包放到 /opt（更新脚本会自动找到那里的最新包）
scp "$PKG" root@123.57.60.164:/opt/

# 同时把更新脚本也传一份（这一步在每次发版前都做，确保使用最新版的更新脚本）
scp scripts/update_server_release.sh root@123.57.60.164:/opt/XEmail/
```

---

## 3. 服务器执行更新（在服务器终端执行）

```bash
ssh root@123.57.60.164
chmod +x /opt/XEmail/update_server_release.sh

# 推荐：手动指定本次要部署的包路径，避免误用旧包
bash /opt/XEmail/update_server_release.sh /opt/xemail-release-YYYYMMDD_HHMMSS.tar.gz
```

如果懒得抄文件名，可以省略参数让脚本自动取 `/opt` 下的最新包：

```bash
bash /opt/XEmail/update_server_release.sh
```

更新脚本会按顺序做这几件事：

1. 备份 `/opt/XEmail` → `/opt/XEmail_backup_<时间戳>`
2. 单独备份 `/opt/XEmail/data` → `/opt/XEmail_data_backup_<时间戳>`
3. 解压发布包到临时目录
4. `rsync --delete` 把新代码同步到 `/opt/XEmail`，**排除** `data/`、`.env`、`.venv/`、`.claude/`
5. 若存在 `.venv`，执行 `pip install -r requirements.txt`
6. 若存在 `xemail.service`，`systemctl restart`；否则提示需要手动重启

> **关于 `--delete`**：rsync 会把 `/opt/XEmail` 里**新包没有**的文件删掉（已排除的目录除外）。所以请不要把临时笔记 / 一次性脚本直接放在 `/opt/XEmail` 根目录——会在下次更新时丢失。`data/`、`.venv/`、`.env`、`.claude/` 在任何情况下都不会被动到。

---

## 4. 手动重启服务（当前部署方式必做）

目前服务器还没配 `xemail.service`，第 3 节脚本的最后一步会跳过自动重启并打印提示。需要手动：

```bash
pkill -9 -f "uvicorn app.main:app" || true
cd /opt/XEmail
source .venv/bin/activate
nohup uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 \
  > /opt/XEmail/app.log 2>&1 &
```

---

## 5. 更新后验证

```bash
# 健康检查（无需登录）
curl -s http://127.0.0.1:8000/health

# 看最近 50 行启动日志，有无 traceback
tail -n 50 /opt/XEmail/app.log

# 确认 data/ 没被动过：账号 / 邮件 / 附件文件大小应当与更新前一致
ls -la /opt/XEmail/data/ | head
```

浏览器侧请强制刷新一次：`Cmd + Shift + R`（清前端 JS 缓存）。

---

## 6. 常见问题

### 6.1 "有些功能更新了，有些没更新"

通常是这三种之一：

- 旧的 uvicorn 进程没退干净 → 重新执行第 4 节
- 浏览器缓存了旧前端 → 强制刷新
- 启动目录不是 `/opt/XEmail`（例如用户用了别的工作目录） → `pwd` 确认后再 `cd`

### 6.2 脚本提示"未找到发布包"

`scp` 没把包传到 `/opt`，或者文件名带了多余空格。建议直接传完整路径：

```bash
bash /opt/XEmail/update_server_release.sh /opt/<完整包名>.tar.gz
```

### 6.3 想确认服务器上的 data/ 确实没被改过

更新前后各跑一次 `stat`：

```bash
stat -c '%y %s %n' /opt/XEmail/data/*.json
```

更新前后输出应完全一致（mtime + size 都不变）。

---

## 7. 出问题后的回滚

更新脚本每次都会留两份备份，回滚就是两条命令：

```bash
# 假设要回滚到 2026-05-19 凌晨那次部署
BK=/opt/XEmail_backup_20260519_xxxxxx

# 1) 停服务
pkill -9 -f "uvicorn app.main:app" || true

# 2) 恢复代码（data/ 不动，因为更新时本来就没被覆盖）
rsync -av --delete \
  --exclude "data/" --exclude ".venv/" \
  "$BK"/ /opt/XEmail/

# 3) 重启
cd /opt/XEmail && source .venv/bin/activate
nohup uvicorn app.main:app --host 0.0.0.0 --port 8000 > /opt/XEmail/app.log 2>&1 &
```

---

## 附录：本地开发启动

与部署无关，仅供新机器初次配置参考：

```bash
cd /path/to/XEmail   # 替换为你本地的项目根目录

# 首次或依赖变化时执行
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 启动服务（开发模式带 --reload）
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

启动后访问 `http://127.0.0.1:8000`。
