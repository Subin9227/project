"""환경변수·모델·경로를 한 곳에서 로드하는 설정 모듈.

기존 discord_claude_chat_app.py의 10~16줄이 여기로 이사왔다.
다른 파일들은 os.environ을 직접 읽지 않고 전부 이 모듈에서 값을 가져간다.
(설정을 한 곳에 모아두면 나중에 값이 바뀌어도 여기만 고치면 된다.)
"""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# 봇이 어느 서버에서 돌든 '오늘'의 기준은 한국 시간으로 고정한다.
# (EC2 컨테이너는 UTC라 이걸 안 쓰면 KST 0~9시에 어제 날짜로 기록된다.)
# agent·bot·notion_tools 셋이 다 쓰므로 config에 둔다 — agent에 두면 순환 import.
KST = ZoneInfo("Asia/Seoul")

# .env 파일을 읽어서 환경변수로 올린다. (프로젝트 루트의 .env)
load_dotenv()

# --- 필수 토큰 (없으면 KeyError로 즉시 멈춤 = 실수 조기 발견) ---
DISCORD_BOT_TOKEN: str = os.environ["DISCORD_BOT_TOKEN"]
ANTHROPIC_API_KEY: str = os.environ["ANTHROPIC_API_KEY"]
NOTION_TOKEN: str = os.environ["NOTION_TOKEN"]

# --- 선택값 (없으면 기본값 사용) ---
CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5")

# 한 번의 답변에서 Claude가 생성할 최대 토큰 수. (기존 봇의 max_tokens=800 유지)
MAX_TOKENS: int = int(os.getenv("MAX_TOKENS", "800"))

# --- 로컬 모델 서빙 (vLLM) — 실험용 스위치 ---
# VLLM_BASE_URL이 있을 때만 agent가 Claude 대신 이 서버를 쓴다.
# .env에 안 넣으면 기존 Claude 경로 그대로 = 봇 동작 무변화.
VLLM_BASE_URL: str | None = os.getenv("VLLM_BASE_URL") or None
# vLLM 서버를 --api-key로 띄웠을 때 쓸 열쇠. 터널로 인터넷에 열리므로 반드시 건다.
VLLM_API_KEY: str = os.getenv("VLLM_API_KEY", "not-needed")
VLLM_MODEL: str = os.getenv("VLLM_MODEL", "Qwen/Qwen3.5-4B")

# --- 경로 ---
# 이 파일(secretary/config.py) 기준으로 프로젝트 루트를 계산한다.
BASE_DIR: Path = Path(__file__).resolve().parent.parent
DATA_DIR: Path = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)  # data/ 폴더가 없으면 자동 생성

# 대화기억(체크포인트)을 저장할 SQLite 파일 경로.
MEMORY_DB_PATH: Path = DATA_DIR / "memory.sqlite"

# 대화기억을 며칠치 남길지. 봇을 켤 때 이보다 오래된 스레드를 지운다.
# ⚠️ 0이면 아예 안 지운다. 테스트 기간에는 사용자들의 대화를 하나하나 봐야 해서
#    0으로 둔다 — 지우면 무슨 일이 있었는지 되짚을 방법이 없다.
#    (공개 운영으로 넘어가면 다시 7 같은 값으로 되돌릴 것. 남의 대화를 무한정
#     들고 있는 건 그 자체가 위험이다)
MEMORY_KEEP_DAYS: int = int(os.getenv("MEMORY_KEEP_DAYS", "7"))

# 사용자별 설정·자격증명 저장소. memory.sqlite와 일부러 파일을 나눈다 —
# 그쪽은 bot.py가 시작할 때 통째로 DELETE/VACUUM 하고, 스키마 주인도 LangGraph다.
USERS_DB_PATH: Path = DATA_DIR / "users.sqlite"

# --- 소유자 / 자격증명 암호화 (#9 Phase 3) ---
# OWNER_ID: 봇 주인의 디스코드 사용자 ID. 등록 없이도 쓸 수 있고 /allow를 부를 수 있다.
#   (이게 없으면 등록자 외에는 아무도 못 쓰는데, 주인 본인부터 막혀버린다)
OWNER_ID: str | None = os.getenv("OWNER_ID") or None

# CRED_KEY: 남의 자격증명을 암호화할 Fernet 키. `Fernet.generate_key()`로 만든다.
# ⚠️ 없으면 등록 기능이 통째로 꺼진다. 평문으로 남의 비밀번호를 저장하는 일은 없다.
# ⚠️ 이건 공격자를 막는 장치가 아니라 `cat users.sqlite`를 막는 장치다.
#    이 서버에 셸이 뚫리면 .env의 키도 같이 털린다 — 등록 화면에서 그렇게 고지한다.
CRED_KEY: str | None = os.getenv("CRED_KEY") or None

# --- 알림 (비서가 먼저 거는 말) ---
# 없으면 알림 기능을 통째로 끈다 (블레이버스·vLLM과 같은 스위치 방식).
#
# ALARM_TARGET에는 **받고 싶은 곳의 ID 하나**만 넣으면 된다. 채널이든 사람이든
# 상관없다 — 디스코드는 둘 다 그냥 숫자라 값만 봐선 구분이 안 되므로, 봇이 켜질 때
# 채널인지 사람인지 직접 물어봐서 판별한다(alarms.resolve_target).
#   채널 ID  → 그 채널에 글을 쓴다 (평소 대화방 = 맥락이 안 끊겨서 이쪽이 낫다)
#   사용자 ID → 그 사람에게 DM
#
# ALARM_MENTION은 선택. 채널로 받을 때 알림음이 울리게 붙일 멘션 대상이다.
# DM으로 받으면 어차피 알림이 오므로 필요 없다.
# ⚠️ 지금은 .env에서 한 사람 것만 읽는다. #9 Phase 3에서 사용자별 설정으로
#    옮길 때 alarms.py는 안 건드리고 이 '읽는 곳'만 갈아끼운다.
#    (그때는 봇이 대화 상대의 ID를 이미 알아서 ALARM_MENTION도 자동으로 채워진다)
ALARM_TARGET: str | None = os.getenv("ALARM_TARGET") or None
ALARM_MENTION: str | None = os.getenv("ALARM_MENTION") or None
WORK_START_TIME: str = os.getenv("WORK_START_TIME", "09:00")
WORK_END_TIME: str = os.getenv("WORK_END_TIME", "18:00")
# "요일 시각". 요일은 월=0. 주간 브리핑을 보낼 때.
WEEKLY_TIME: str = os.getenv("WEEKLY_TIME", "MON 08:00")

# --- 노션 (데일리루틴 사진 인증용) ---
# "오프라인 데일리 루틴 (2)" 데이터소스 ID.
# ("비서 전용 페이지" 안의 깨끗한 전용 DB. 하루 1행, 스키마는 옛 DB와 동일.)
# 워크스페이스를 새로 만들면 바뀔 수 있으므로 여기서 한 곳에서 관리한다.
# (옛 "오프라인 데일리 루틴" = df10ffe9-306d-834e-bae9-0717212de385)
# ⚠️ 기본값을 두지 않는다. 특정인의 워크스페이스 주소가 기본값이면, .env에 이 줄을
#    빠뜨린 사람이 남의 표로 요청을 보내고 권한 오류만 받는다 — 본인 노션은 멀쩡한데
#    원인을 찾을 길이 없다. 비어 있으면 context.require()가 등록 안내를 낸다.
NOTION_ROUTINE_DS_ID: str = os.getenv("NOTION_ROUTINE_DS_ID", "")

# "과제 제출 (2)" 데이터소스 ID. 루틴 DB와 같은 페이지에 들어있는 별개 DB.
# 주차별 1행. 내용(과제 결과·회고)은 사람이 쓰고, 봇은 틀을 깔고 상태만 옮긴다.
NOTION_HOMEWORK_DS_ID: str = os.getenv("NOTION_HOMEWORK_DS_ID", "")

# 노션 REST API를 직접 호출할 때 쓰는 상수.
# (MCP 서버를 걷어내고 필요한 것만 직접 부른다 — #9 Phase 1)
NOTION_API_BASE: str = "https://api.notion.com/v1"
NOTION_VERSION: str = os.getenv("NOTION_VERSION", "2025-09-03")

# 무료 플랜 파일 업로드 상한 (5MB). 이보다 크면 압축한다.
NOTION_MAX_UPLOAD_BYTES: int = 5 * 1024 * 1024

# --- 블레이버스 (비공식 API) ---
# 공식 API가 없어서 웹앱이 쓰는 엔드포인트를 그대로 호출한다.
# 인증은 refresh가 아니라 sign-in 직접 호출로 간다:
#   토큰(JWT)이 1시간짜리라 손으로 갈아끼우는 게 불가능하고, 토큰 안에 발급 당시
#   IP가 박히기 때문에 봇이 도는 서버에서 직접 로그인해야 IP도 맞는다.
# 값이 없으면 블레이버스 도구를 아예 안 붙인다 (VLLM_BASE_URL과 같은 스위치 방식).
BLAYBUS_API_BASE: str = "https://api-v2.blaybus.com"
BLAYBUS_LOGIN_ID: str | None = os.getenv("BLAYBUS_LOGIN_ID") or None
BLAYBUS_PASSWORD: str | None = os.getenv("BLAYBUS_PASSWORD") or None
# 대부분의 경로가 /project/{id}/... 형태다. 사용자는 프로젝트가 하나뿐이라 상수로 둔다.
BLAYBUS_PROJECT_ID: str = os.getenv("BLAYBUS_PROJECT_ID", "4983")

# --- 상태 서버 (/health) — Phase 3 WireShark 캡처용 ---
# 봇은 원래 바깥으로만 나가는 클라이언트라 접속받을 '문'이 없다.
# 캡처할 서버 트래픽을 만들려고 /health 엔드포인트를 연다. (PLAN.md §9)
#
# HEALTH_HOST = 0.0.0.0 : 모든 랜카드로 들어오는 접속을 받는다 → 폰 등 외부 기기가 붙을 수 있다.
#   127.0.0.1로 두면 맥 자기 자신만 접속 가능 → localhost는 랜카드(en0)를 안 지나
#   WireShark 캡처가 안 된다(과제가 '다른 컴퓨터'를 요구하는 이유).
HEALTH_HOST: str = os.getenv("HEALTH_HOST", "0.0.0.0")
HEALTH_PORT: int = int(os.getenv("HEALTH_PORT", "8000"))

# langchain-anthropic(ChatAnthropic)은 ANTHROPIC_API_KEY 환경변수를 자동으로 읽는다.
# load_dotenv()가 이미 올려놨으므로 별도 전달은 필요 없다.
