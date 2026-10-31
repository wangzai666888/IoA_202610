#!/usr/bin/env bash
set -u

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

if [ "$#" -eq 0 ]; then
    echo "用法：$0 python3 scripts/实验脚本.py" >&2
    exit 2
fi

run_id=$(date -u +'%Y%m%dT%H%M%SZ')
start_utc=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
out="experiment_logs/$run_id"
mkdir -p "$out"

printf 'start_utc=%s\ncommand=%s\n' \
    "$start_utc" "$*" > "$out/meta.txt"

"$@" 2>&1 | tee "$out/result.log"
status=${PIPESTATUS[0]}

printf 'end_utc=%s\nexit_code=%s\n' \
    "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$status" >> "$out/meta.txt"

containers=(
    ioa-server
    autogpt-client
    open-interpreter-client
    react-client
    autogpt-agent
    open-interpreter-agent
    react-tool-agent
)

for name in "${containers[@]}"; do
    if docker container inspect "$name" >/dev/null 2>&1; then
        docker logs --since "$start_utc" --timestamps "$name" \
            > "$out/$name.log" 2>&1
    else
        printf '%s\n' "$name" >> "$out/unavailable_containers.txt"
    fi
done

for name in "${containers[@]}"; do
    if [ -s "$out/$name.log" ]; then
        sed -E "s/^([^[:space:]]+)[[:space:]]/\1 [$name] /" \
            "$out/$name.log"
    fi
done | LC_ALL=C sort > "$out/timeline_utc.log"

echo "实验日志目录：$out"
echo "合并时间线：$out/timeline_utc.log"
exit "$status"
