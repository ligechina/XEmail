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

# Build a release tarball from project root.
# Includes docs/ by default; excludes runtime/local-only artifacts.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist"
RELEASE_NAME="${1:-xemail-release-$(date +%Y%m%d_%H%M%S)}"
OUT_PATH="${DIST_DIR}/${RELEASE_NAME}.tar.gz"

mkdir -p "${DIST_DIR}"

echo "==> 打包项目: ${OUT_PATH}"
(
  cd "${ROOT_DIR}"
  COPYFILE_DISABLE=1 tar --exclude="./dist" \
    --exclude="./data" \
    --exclude="./.venv" \
    --exclude="./.git" \
    --exclude="./.claude" \
    --exclude="./.env" \
    --exclude="./.DS_Store" \
    --exclude="./__MACOSX" \
    --exclude="./._*" \
    --exclude="./__pycache__" \
    --exclude="*.pyc" \
    -czf "${OUT_PATH}" .
)

# Sanity check: make sure nothing private leaked in. The script exits
# non-zero if any private-looking path snuck past the excludes — better
# to fail loud here than ship a tarball with user data.
LEAKS=$(tar -tzf "${OUT_PATH}" | grep -E "^\./(data/|\.env$|\.venv/|\.claude/|\.git/)" || true)
if [[ -n "${LEAKS}" ]]; then
  echo "错误：发布包内检测到私有路径，已中止：" >&2
  echo "${LEAKS}" >&2
  rm -f "${OUT_PATH}"
  exit 1
fi

echo "==> 打包完成: ${OUT_PATH}"
echo "==> 内容预览（前 30 条）:"
tar -tzf "${OUT_PATH}" | head -30
