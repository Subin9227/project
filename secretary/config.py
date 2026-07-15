"""환경변수·모델·경로를 한 곳에서 로드하는 설정 모듈.

기존 discord_claude_chat_app.py의 10~16줄이 여기로 이사왔다.
다른 파일들은 os.environ을 직접 읽지 않고 전부 이 모듈에서 값을 가져간다.
(설정을 한 곳에 모아두면 나중에 값이 바뀌어도 여기만 고치면 된다.)
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

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

# --- 경로 ---
# 이 파일(secretary/config.py) 기준으로 프로젝트 루트를 계산한다.
BASE_DIR: Path = Path(__file__).resolve().parent.parent
DATA_DIR: Path = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)  # data/ 폴더가 없으면 자동 생성

# 대화기억(체크포인트)을 저장할 SQLite 파일 경로.
MEMORY_DB_PATH: Path = DATA_DIR / "memory.sqlite"

# --- 노션 (데일리루틴 사진 인증용) ---
# "오프라인 데일리 루틴" 데이터소스 ID. (개인 워크스페이스, 하루 1행)
# 워크스페이스를 새로 만들면 바뀔 수 있으므로 여기서 한 곳에서 관리한다.
NOTION_ROUTINE_DS_ID: str = os.getenv(
    "NOTION_ROUTINE_DS_ID", "df10ffe9-306d-834e-bae9-0717212de385"
)

# 노션 REST API를 직접 호출할 때 쓰는 상수.
# (MCP 서버엔 파일 업로드/이미지 블록 도구가 없어서 직접 호출한다.)
NOTION_API_BASE: str = "https://api.notion.com/v1"
NOTION_VERSION: str = os.getenv("NOTION_VERSION", "2025-09-03")

# 무료 플랜 파일 업로드 상한 (5MB). 이보다 크면 압축한다.
NOTION_MAX_UPLOAD_BYTES: int = 5 * 1024 * 1024

# langchain-anthropic(ChatAnthropic)은 ANTHROPIC_API_KEY 환경변수를 자동으로 읽는다.
# load_dotenv()가 이미 올려놨으므로 별도 전달은 필요 없다.
