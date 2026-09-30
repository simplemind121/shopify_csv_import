#!/bin/bash
# ============================================================
# shopify_csv_import 自动化测试（Odoo 19 真实数据库 + Odoo 测试框架）
#
# 两种运行方式：
#   1) Docker（默认，本地和 GitHub Actions 都用这个）：
#        ./dev/run_tests.sh
#      会起一个临时的 postgres:16 + odoo:19.0 容器，跑完自动删除。
#
#   2) 本机 Odoo 源码（拉不到 Docker Hub 镜像时用）：
#        ODOO_SRC=/path/to/odoo PYTHON=/path/to/venv/bin/python \
#        DB_HOST=127.0.0.1 DB_PORT=5432 DB_USER=odoo DB_PASSWORD=odoo \
#        ./dev/run_tests.sh
#
# 可选环境变量：
#   MEDIA_PICKER_DIR  真实 media_picker 模块目录（默认用 dev/stub_addons/media_picker 测试替身）
#   ODOO_IMAGE        Docker 模式的 Odoo 镜像（默认 odoo:19.0）
#   TEST_DB           测试库名（默认 sci_test_<随机>，跑完删除）
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODULE=shopify_csv_import
MEDIA_PICKER_DIR="${MEDIA_PICKER_DIR:-$ROOT/dev/stub_addons/media_picker}"
ODOO_IMAGE="${ODOO_IMAGE:-odoo:19.0}"
TEST_DB="${TEST_DB:-sci_test_$RANDOM}"
LOG_FILE="${LOG_FILE:-$ROOT/dev/test.log}"

# 把被测模块和 media_picker 放进同一个临时 addons 目录
ADDONS_TMP=$(mktemp -d)
cleanup() {
  rm -rf "$ADDONS_TMP"
  if [ -n "${NET:-}" ]; then
    docker rm -f "${NET}-db" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
cp -a "$ROOT/$MODULE" "$ADDONS_TMP/$MODULE"
cp -a "$MEDIA_PICKER_DIR" "$ADDONS_TMP/media_picker"
find "$ADDONS_TMP" -name '__pycache__' -type d -prune -exec rm -rf {} +

ODOO_ARGS=(-d "$TEST_DB" -i "$MODULE" --test-tags "/$MODULE" --without-demo
           --stop-after-init --no-http --log-level=test)

set +e
if [ -n "${ODOO_SRC:-}" ]; then
  PYTHON="${PYTHON:-python3}"
  echo "==> 使用本机 Odoo 源码：$ODOO_SRC"
  PYTHONPATH="$ODOO_SRC" "$PYTHON" "$ODOO_SRC/setup/odoo" \
    --addons-path="$ODOO_SRC/odoo/addons,$ADDONS_TMP" \
    --db_host="${DB_HOST:-127.0.0.1}" --db_port="${DB_PORT:-5432}" \
    --db_user="${DB_USER:-odoo}" --db_password="${DB_PASSWORD:-odoo}" \
    "${ODOO_ARGS[@]}" 2>&1 | tee "$LOG_FILE"
  RC=${PIPESTATUS[0]}
  if [ -n "${DROP_DB_CMD:-}" ]; then eval "$DROP_DB_CMD $TEST_DB" || true; fi
else
  echo "==> 使用 Docker 镜像：$ODOO_IMAGE"
  NET="sci-test-$RANDOM"
  docker network create "$NET" >/dev/null
  docker run -d --name "${NET}-db" --network "$NET" \
    -e POSTGRES_USER=odoo -e POSTGRES_PASSWORD=odoo -e POSTGRES_DB=postgres \
    postgres:16 >/dev/null
  for _ in $(seq 1 30); do
    docker exec "${NET}-db" pg_isready -U odoo >/dev/null 2>&1 && break
    sleep 2
  done
  # 官方镜像的 entrypoint 会用 HOST/USER/PASSWORD 环境变量拼 --db_* 参数并追加在
  # 命令行最后（会覆盖我们自己传的），所以数据库连接只能走环境变量
  docker run --rm --network "$NET" \
    -e HOST="${NET}-db" -e PORT=5432 -e USER=odoo -e PASSWORD=odoo \
    -v "$ADDONS_TMP:/mnt/extra-addons:ro" \
    "$ODOO_IMAGE" odoo \
    --addons-path=/mnt/extra-addons,/usr/lib/python3/dist-packages/odoo/addons \
    "${ODOO_ARGS[@]}" 2>&1 | tee "$LOG_FILE"
  RC=${PIPESTATUS[0]}
fi
set -e

SUMMARY=$(grep -E "odoo.tests.result: .* of [0-9]+ tests" "$LOG_FILE" | tail -n1 || true)
echo
echo "==> ${SUMMARY:-没有找到测试结果汇总行}"
if [ "$RC" != "0" ] || [ -z "$SUMMARY" ] || ! echo "$SUMMARY" | grep -q " 0 failed, 0 error(s)"; then
  echo "[FAIL] 测试未通过（退出码 $RC），完整日志：$LOG_FILE" >&2
  exit 1
fi
echo "[OK] 全部测试通过"
