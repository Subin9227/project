#!/bin/bash
# 공주비서 프로세스 관측 스크립트 (과제 트랙 Phase 2)
#
#   사용법:  scripts/inspect_process.sh A-idle
#            scripts/inspect_process.sh B-working
#            scripts/inspect_process.sh C-after
#
# 봇을 '바깥에서 구경만' 한다. 봇 코드는 이 파일을 import하지도 실행하지도 않으며,
# 여기 쓰인 명령은 전부 읽기 전용이라 봇을 멈추거나 느리게 하지 않는다.
#
# 왜 스크립트인가:
#   1) 노션 도구를 호출할 때 뜨는 npx 자식 프로세스는 몇 초 만에 사라진다.
#      손으로 명령 네 개를 치면 늦어서 '생성됐다'는 증거를 놓친다.
#   2) A/B/C 세 시점을 '똑같은 명령'으로 찍어야 비교표가 성립한다.
#
# ⚠️ 보안: reports/ 는 .gitignore에 없어서 공개 레포에 커밋된다.
#    그래서 `ps -E`(환경변수 덤프)는 절대 쓰지 않는다 — 썼다면 DISCORD_BOT_TOKEN,
#    ANTHROPIC_API_KEY, NOTION_TOKEN이 그대로 보고서에 박혀 깃허브에 올라간다.
#    리눅스 자료의 /proc/<pid>/environ 도 같은 이유로 금지.

set -uo pipefail

LABEL="${1:-snapshot}"
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$BASE_DIR/reports/raw"
OUT="$OUT_DIR/${LABEL}.txt"
mkdir -p "$OUT_DIR"

# ── 봇 PID 찾기 ──────────────────────────────────────────────────
# `pgrep -f`는 명령줄 '전체'에서 문자열을 찾는다. 그래서 봇을 실행한 쉘도 같이
# 걸린다 — 그 쉘의 명령줄 안에 "python run.py"라는 글자가 들어있기 때문이다.
# (실측으로 확인한 함정. 껍데기 쉘까지 세면 '봇이 2개'로 오판한다.)
#   → 후보를 뽑은 뒤, 실행 파일 이름(comm)이 실제로 python인 것만 남긴다.
BOT_PIDS=""
for cand in $(pgrep -f "run\.py" || true); do
  comm=$(ps -o comm= -p "$cand" 2>/dev/null)
  case "$comm" in
    *python*) BOT_PIDS="$BOT_PIDS$cand
";;
  esac
done
BOT_PIDS=$(echo "$BOT_PIDS" | grep . || true)
BOT_COUNT=$(echo "$BOT_PIDS" | grep -c . || true)

if [ "$BOT_COUNT" -eq 0 ]; then
  echo "❌ 봇이 실행 중이 아닙니다. 먼저 다른 터미널에서 .venv/bin/python run.py 를 띄우세요."
  exit 1
fi
if [ "$BOT_COUNT" -gt 1 ]; then
  echo "⚠️  봇 프로세스가 $BOT_COUNT 개 잡혔습니다. 유령이 섞여 측정이 오염됩니다:"
  echo "$BOT_PIDS"
  echo "    → 낡은 것을 정리한 뒤 다시 실행하세요."
  exit 1
fi
PID="$BOT_PIDS"

# ── 출력 시작 ────────────────────────────────────────────────────
{
echo "================================================================"
echo " 공주비서 프로세스 스냅샷 : $LABEL"
echo " 찍은 시각 : $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo " 봇 PID    : $PID"
echo " 호스트    : $(uname -srm)"
echo "================================================================"

echo
echo "--- [1] 프로세스 기본 정보 ---------------------------------"
echo "# 이 명령은 무엇을 보는가: 봇이 지금 어떤 상태이고 메모리를 얼마나 쓰는지."
echo "#   RSS = 실제 물리 메모리(KB, 진짜 쓰는 양)"
echo "#   VSZ = 가상 메모리(KB, 예약해둔 주소 공간 — 항상 RSS보다 훨씬 큼)"
echo "#   STAT = 상태 (S=대기중/sleeping, R=실행중, Z=좀비)"
echo "#   ELAPSED = 켜진 뒤 흐른 시간"
echo "\$ ps -o pid,ppid,rss,vsz,%cpu,%mem,stat,etime,command -p $PID"
ps -o pid,ppid,rss,vsz,%cpu,%mem,stat,etime,command -p "$PID"

echo
echo "--- [2] 스레드 목록 ----------------------------------------"
echo "# 이 명령은 무엇을 보는가: 프로세스 '안'의 일꾼 가닥이 몇 개인지."
echo "# 관전 포인트: 이 봇은 asyncio라 논리적으론 이벤트루프 1가닥인데,"
echo "#   커널이 보는 스레드는 여러 개다. (프로세스 vs 스레드의 실물 예시)"
echo "\$ ps -M $PID"
ps -M "$PID"
echo "→ 스레드 수(헤더 제외): $(ps -M "$PID" | tail -n +2 | grep -c .)"

echo
echo "--- [3] 자식 프로세스 (PPID = $PID 인 것만) -----------------"
echo "# 이 명령은 무엇을 보는가: 봇이 직접 낳은 자식만 골라낸다."
echo "# ⚠️ 이름('notion-mcp-server')으로 세면 안 된다 — Claude Code 세션들도"
echo "#   같은 이름의 MCP 서버를 띄워두기 때문에 남의 자식이 섞여 든다."
echo "#   그래서 반드시 부모 번호(PPID)로 거른다."
echo "\$ ps -ef | awk '\$3 == $PID'"
CHILDREN=$(ps -eo pid,ppid,stat,etime,rss,command | awk -v p="$PID" 'NR==1 || $2==p')
echo "$CHILDREN"
CHILD_PIDS=$(echo "$CHILDREN" | tail -n +2 | awk '{print $1}')
if [ -z "$CHILD_PIDS" ]; then
  echo "→ 직계 자식 없음"
else
  echo "→ 직계 자식 $(echo "$CHILD_PIDS" | grep -c .) 개. 손자까지 추적:"
  for c in $CHILD_PIDS; do
    ps -eo pid,ppid,stat,etime,rss,command | awk -v p="$c" '$2==p'
  done
fi

echo
echo "--- [4] 열린 네트워크 연결 ---------------------------------"
echo "# 이 명령은 무엇을 보는가: 봇이 지금 어디에 접속해 있는지."
echo "# 관전 포인트: 디스코드 게이트웨이(WSS, 443 ESTABLISHED)가 상시 물려 있다."
echo "#   봇은 '접속을 받는' 서버가 아니라 '접속하러 나가는' 클라이언트라는 증거."
echo "#   (Phase 3에서 /health를 열면 여기에 LISTEN 줄이 생긴다)"
echo "\$ lsof -i -a -p $PID -n -P"
lsof -i -a -p "$PID" -n -P 2>/dev/null || echo "(없음 또는 권한 부족)"

echo
echo "--- [5] 열린 파일 (소켓 제외) ------------------------------"
echo "# 이 명령은 무엇을 보는가: 봇이 붙잡고 있는 파일들."
echo "# 관전 포인트: data/memory.sqlite 와 그 짝꿍 -wal/-shm 파일."
echo "#   (WAL = 대화기억을 쓸 때 쓰는 SQLite의 기록 방식)"
echo "\$ lsof -p $PID | grep -E 'REG|DIR' | grep project"
lsof -p "$PID" 2>/dev/null | grep -E "REG|DIR" | grep "project" || echo "(해당 없음)"

echo
echo "--- [6] 메모리 지도 요약 -----------------------------------"
echo "# 이 명령은 무엇을 보는가: 메모리를 무슨 용도로 얼마나 나눠 쓰는지."
echo "# 리눅스의 pmap / /proc/<pid>/maps 에 해당하는 맥 전용 명령."
echo "#   __TEXT=실행 코드, MALLOC=파이썬이 쓰는 힙, mapped file=디스크에서 올린 것"
echo "\$ vmmap $PID  (요약부만)"
vmmap "$PID" 2>/dev/null | sed -n '/ReadOnly portion/,/^$/p;/Summary/,$p' | head -60 \
  || echo "(vmmap 실패 — 권한 문제일 수 있음)"

echo
echo "================================================================"
echo " 스냅샷 $LABEL 끝"
echo "================================================================"
} 2>&1 | tee "$OUT"

echo
echo "✅ 저장됨: reports/raw/${LABEL}.txt"
