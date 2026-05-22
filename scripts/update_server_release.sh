#!/usr/bin/env bash
# Copyright (c) 2026 Peking University & Beijing Siliconheart Technology Co., Ltd.
# XEmail is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#          http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

set -euo pipefail

# 一键更新 XEmail（仅更新代码，不覆盖 data/ 用户数据）
#
# 默认约定（可通过参数覆盖）：
# - 应用目录：/opt/XEmail
# - 发布包：自动在以下位置取“最新包”
#   - /opt
#   - /opt/XEmail
#   - /opt/XEmail/dist
#   支持常见命名：
#   - xemail-release-*.tar.gz
#   - XEmail-release-*.tar.gz
#   - *release*.tar.gz
# - 服务名：xemail（若 systemd 存在该服务则自动重启）
#
# 用法：
#   bash update_server_release.sh
#   bash update_server_release.sh /opt/xemail-release-20260517_182150.tar.gz
#   APP_DIR=/opt/XEmail SERVICE_NAME=xemail bash update_server_release.sh /opt/pkg.tar.gz

APP_DIR="${APP_DIR:-/opt/XEmail}"
SERVICE_NAME="${SERVICE_NAME:-xemail}"
PACKAGE_PATH="${1:-}"

if [[ ! -d "$APP_DIR" ]]; then
  echo "错误：应用目录不存在：$APP_DIR" >&2
  exit 1
fi

if [[ -z "$PACKAGE_PATH" ]]; then
  # 自动寻找最新发布包（覆盖常见命名与目录）。
  shopt -s nullglob
  candidates=(
    /opt/xemail-release-*.tar.gz
    /opt/XEmail-release-*.tar.gz
    /opt/*release*.tar.gz
    /opt/XEmail/xemail-release-*.tar.gz
    /opt/XEmail/XEmail-release-*.tar.gz
    /opt/XEmail/*release*.tar.gz
    /opt/XEmail/dist/xemail-release-*.tar.gz
    /opt/XEmail/dist/XEmail-release-*.tar.gz
    /opt/XEmail/dist/*release*.tar.gz
  )
  shopt -u nullglob

  if ((${#candidates[@]} > 0)); then
    PACKAGE_PATH="$(ls -1t "${candidates[@]}" 2>/dev/null | head -n 1 || true)"
  fi
fi

if [[ -z "$PACKAGE_PATH" || ! -f "$PACKAGE_PATH" ]]; then
  echo "错误：未找到发布包。请传入包路径，或把发布包放到 /opt（或 /opt/XEmail/dist）" >&2
  exit 1
fi

echo "==> 将使用发布包: $PACKAGE_PATH"

NOW="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="/opt/XEmail_backup_${NOW}"
DATA_BACKUP_DIR="/opt/XEmail_data_backup_${NOW}"
TMP_DIR="/tmp/xemail_update_${NOW}"

echo "==> [1/6] 备份当前代码到: $BACKUP_DIR"
cp -a "$APP_DIR" "$BACKUP_DIR"

if [[ -d "$APP_DIR/data" ]]; then
  echo "==> [2/6] 备份 data/ 到: $DATA_BACKUP_DIR"
  cp -a "$APP_DIR/data" "$DATA_BACKUP_DIR"
else
  echo "==> [2/6] 跳过 data/ 备份（目录不存在）"
fi

echo "==> [3/6] 解压发布包: $PACKAGE_PATH"
mkdir -p "$TMP_DIR/unpack"
tar -xzf "$PACKAGE_PATH" -C "$TMP_DIR/unpack"

echo "==> [4/6] 同步代码（保留 data/ 与 .env，--delete 会清掉服务器上发布包里没有的代码文件）"
rsync -av --delete \
  --exclude "data/" \
  --exclude ".env" \
  --exclude ".venv/" \
  --exclude ".claude/" \
  --exclude "__MACOSX/" \
  --exclude "._*" \
  --exclude ".DS_Store" \
  "$TMP_DIR/unpack"/ "$APP_DIR"/

echo "==> [5/6] 安装依赖（如存在虚拟环境）"
if [[ -d "$APP_DIR/.venv" ]]; then
  # shellcheck disable=SC1091
  source "$APP_DIR/.venv/bin/activate"
  pip install -r "$APP_DIR/requirements.txt"
else
  echo "警告：未检测到 $APP_DIR/.venv，已跳过 pip install"
fi

echo "==> [6/6] 重启服务"
if command -v systemctl >/dev/null 2>&1; then
  if systemctl list-unit-files | awk '{print $1}' | grep -qx "${SERVICE_NAME}.service"; then
    sudo systemctl restart "${SERVICE_NAME}.service"
    sudo systemctl status "${SERVICE_NAME}.service" --no-pager
  else
    echo "警告：未找到 ${SERVICE_NAME}.service，已跳过自动重启。"
    echo "请手动重启你的进程管理器（systemd/supervisor/pm2/docker 等）。"
  fi
else
  echo "警告：系统无 systemctl，已跳过自动重启。"
  echo "请手动重启你的进程管理器。"
fi

echo "==> 清理临时目录"
rm -rf "$TMP_DIR"

echo
echo "更新完成。"
echo "代码备份：$BACKUP_DIR"
if [[ -d "$DATA_BACKUP_DIR" ]]; then
  echo "数据备份：$DATA_BACKUP_DIR"
fi
