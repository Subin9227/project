#!/bin/bash
# 봇의 자식 프로세스가 '태어났다 죽는 순간'을 포착하는 감시자 (과제 트랙 Phase 2)
#
#   사용법:  scripts/watch_children.sh 60      # 60초 동안 감시
#
# 왜 필요한가:
#   노션 MCP 도구를 호출하면 npx가 자식 프로세스를 띄우는데, 일이 끝나면 곧바로
#   사라진다. inspect_process.sh를 손으로 실행해서는 그 사이에 이미 죽어 있어
#   두 번이나 놓쳤다. 사람이 명령을 치는 속도(수 초)보다 자식의 수명이 짧기 때문.
#   → 0.2초마다 들여다보는 감시 루프로 '생성 → 소멸' 전 구간을 기록한다.
#
# 이 스크립트도 읽기 전용이다. 프로세스 목록을 조회만 하고 아무것도 죽이지 않는다.

set -uo pipefail

DURATION="${1:-60}"
INTERVAL=0.2
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$BASE_DIR/reports/raw/B-children-timeline.txt"
mkdir -p "$(dirname "$OUT")"

# 봇 PID 찾기 (inspect_process.sh와 같은 방식 — 껍데기 쉘을 걸러낸다)
BOT_PID=""
for cand in $(pgrep -f "run\.py" || true); do
  case "$(ps -o comm= -p "$cand" 2>/dev/null)" in
    *python*) BOT_PID="$cand" ;;
  esac
done
[ -z "$BOT_PID" ] && { echo "❌ 봇이 실행 중이 아닙니다."; exit 1; }

{
echo "================================================================"
echo " 자식 프로세스 생성·소멸 타임라인"
echo " 감시 시작 : $(date '+%Y-%m-%d %H:%M:%S')  (${DURATION}초 동안, ${INTERVAL}초 간격)"
echo " 봇 PID    : $BOT_PID"
echo "================================================================"
echo
echo "시각          이벤트   PID    부모   경과   명령"
echo "----------------------------------------------------------------"

END=$(( $(date +%s) + DURATION ))
PREV=""          # 직전 순간에 살아있던 자손 PID 목록
FIRST_SEEN=""    # 처음 발견한 시각 기록용

while [ "$(date +%s)" -lt "$END" ]; do
  # 자식(PPID=봇) + 손자(PPID=자식) 까지 한 번에 수집
  NOW=$(ps -eo pid,ppid,etime,command | awk -v p="$BOT_PID" '
      $2==p { print $1; kids[$1]=1 }
      { line[$1]=$0 }
      END { for (k in kids) for (i in line) { split(line[i],f," "); if (f[2]==k) print f[1] } }
  ' | sort -u)

  # 새로 생긴 것 = 지금은 있는데 직전엔 없던 것
  for pid in $NOW; do
    if ! echo "$PREV" | grep -qx "$pid"; then
      info=$(ps -o pid=,ppid=,etime=,command= -p "$pid" 2>/dev/null | cut -c1-110)
      printf "%s  🟢 생성  %s\n" "$(date '+%H:%M:%S')" "$info"
    fi
  done
  # 사라진 것 = 직전엔 있었는데 지금은 없는 것
  for pid in $PREV; do
    if ! echo "$NOW" | grep -qx "$pid"; then
      printf "%s  🔴 소멸  PID %s\n" "$(date '+%H:%M:%S')" "$pid"
    fi
  done

  PREV="$NOW"
  sleep "$INTERVAL"
done

echo "----------------------------------------------------------------"
echo "감시 종료 : $(date '+%Y-%m-%d %H:%M:%S')"
echo "(생성/소멸 줄이 하나도 없으면, 감시 중에 도구 호출이 없었던 것)"
} 2>&1 | tee "$OUT"

echo
echo "✅ 저장됨: reports/raw/B-children-timeline.txt"
