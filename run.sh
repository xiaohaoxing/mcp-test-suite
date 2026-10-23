#!/usr/bin/env bash
# 一条命令跑完某个服务 / 全部服务。
#
#   ./run.sh db                 # service-a 全套
#   ./run.sh elk --layer guard  # 只跑 elk 的 guard 层（其余参数原样透传给 mcp_suite）
#   ./run.sh all                # 四个服务依次跑，任一失败则整体退出码非 0
#   ./run.sh db --base-url http://127.0.0.1:18091
#
# 令牌从各仓库自己的 .env 读（--env-file），不写进任何入库文件。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

service="${1:-}"
shift || true
if [[ -z "$service" ]]; then
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
fi

run_one() {
  local name="$1" dir="$2" consumer="$3" token_env="$4"
  shift 4
  echo "=== $name"
  python3 -m mcp_suite run \
    --suite "$dir/mcp-test-suite.yaml" \
    --env-file "$dir/.env" \
    --token-env "$token_env" \
    --consumer "$consumer" \
    --admin-token-env MCP_ADMIN_TOKEN \
    "$@"
}

case "$service" in
  db)          run_one service-a          ../service-a          ${CONSUMER:-}    ${TOKEN_ENV:-SERVICE_A_TOKENS} "$@" ;;
  elk)         run_one service-b         ../service-b         client          ${TOKEN_ENV:-SERVICE_B_TOKENS} "$@" ;;
  jenkins)     run_one service-c     ../service-c     demo-readonly   ${TOKEN_ENV:-SERVICE_C_TOKENS} "$@" ;;
  sw)          run_one service-d ../service-d localdev     ${TOKEN_ENV:-SERVICE_D_TOKENS} "$@" ;;
  all|baseline-all)
    fail=0
    for t in "service-a:../service-a:${CONSUMER:-}:${TOKEN_ENV:-SERVICE_A_TOKENS}" \
             "service-b:../service-b:client:${TOKEN_ENV:-SERVICE_B_TOKENS}" \
             "service-c:../service-c:demo-readonly:${TOKEN_ENV:-SERVICE_C_TOKENS}" \
             "service-d:../service-d:localdev:${TOKEN_ENV:-SERVICE_D_TOKENS}"; do
      IFS=: read -r name dir consumer token_env <<<"$t"
      run_one "$name" "$dir" "$consumer" "$token_env" "$@" || fail=1
      echo
    done
    echo "汇总退出码：$fail（0=全过 / 1=有失败）"
    exit "$fail"
    ;;
  baseline)
    # 基线四层：liveness / auth / guard / admin
    shift_args=("--layer" "liveness,auth,guard,admin" "$@")
    exec "$0" all "${shift_args[@]}"
    ;;
  *)
    echo "未知目标：$service（可选 db / elk / jenkins / sw / all / baseline）" >&2
    exit 2
    ;;
esac
