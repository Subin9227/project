# 공주비서 (Princess Secretary)

디스코드 집사봇. 사용자가 자연어로 말하면 노션 등 기록처를 대신 채워주는 비서.
'닛몰캐쉬 잘자요 아가씨' 말투로 사용자를 "아가씨"라 부른다.

## 실행

- **Python 3.11+ 필수** (3.9로 venv 만들면 최신 패키지 설치 실패)
- ~~Node.js~~ **불필요** — 노션 MCP 서버는 #9 Phase 1에서 걷어냈다 (REST 직접 호출)
- 진입점: `.venv/bin/python run.py`
- `.env` 필수 키 3개: `DISCORD_BOT_TOKEN`, `ANTHROPIC_API_KEY`, `NOTION_TOKEN`
  (`CLAUDE_MODEL` 기본 `claude-sonnet-4-5`)
  (선택: `LANGSMITH_TRACING`·`LANGSMITH_API_KEY`·`LANGSMITH_ENDPOINT`·`LANGSMITH_PROJECT` — 관측용, #3 추가.
   ⚠️ **프로세스 전역이라 남의 대화까지 내 LangSmith로 간다** — 공개배포 전 정리 필요)
  (선택: `BLAYBUS_LOGIN_ID`·`BLAYBUS_PASSWORD` — 주인의 1인 모드용.
   `BLAYBUS_PROJECT_ID` 기본 4983. ⚠️ 비밀번호에 `#`이 있으면 작은따옴표로 감쌀 것)
  (선택: `VLLM_BASE_URL`·`VLLM_API_KEY`·`VLLM_MODEL` — 있으면 Claude 대신 로컬 모델, #12 실험)
  (선택 — 알림 #9 Phase 2: `ALARM_TARGET`(받을 채널/사람 ID 하나), `ALARM_MENTION`,
   `WORK_START_TIME`·`WORK_END_TIME`·`WEEKLY_TIME`)
  (선택 — 멀티유저 #9 Phase 3: `OWNER_ID`(주인 디스코드 ID, 등록 없이 통과),
   `CRED_KEY`(Fernet 키. **없으면 등록 기능 자체가 꺼짐** — 평문 저장은 안 한다).
   만들기: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
- `.env` / `.venv/` / `data/` 는 gitignore (커밋 금지)

## 구조 (secretary/ 패키지)

| 파일 | 역할 |
|---|---|
| `config.py` | env·토큰·경로 로드 + 노션/블레이버스/알림/암호화 상수, `KST` |
| `persona.py` | '아가씨' 집사 시스템 프롬프트 |
| `notion_tools.py` | 노션 REST 직접(httpx). 루틴 도구 4개 + 템플릿 복사 |
| `homework_tools.py` | 노션 '과제 제출' DB 도구 4개 |
| `blaybus_tools.py` | 블레이버스 **비공식 API** 직접 호출(httpx). 도구 9개 |
| `alarms.py` | 정해진 시각에 비서가 먼저 거는 알림 (LLM 안 부름) |
| `context.py` | **"지금 이 요청은 누구 것인가"** — contextvars. 도구들이 여기서 주인을 꺼낸다 |
| `users.py` | `data/users.sqlite` 저장소. Fernet 암호화 |
| `onboarding.py` | 등록 시 검증·탐색 (노션 페이지→DB 발견, 블레이버스 로그인) |
| `commands.py` | 슬래시 커맨드 `/register` `/setup` `/status` `/forget` |
| `webserver.py` | FastAPI `/health`. bot.py의 `asyncio.gather`로 봇과 **한 프로세스** |
| `agent.py` | `create_react_agent` (사람별 모델 + 도구 17개 + SqliteSaver 기억) |
| `bot.py` | 디스코드 게이트웨이. 등록 관문 → 주인 심기 → agent.ainvoke → 답장 |
| `run.py` (루트) | `asyncio.run(main())` 진입점 |
| ~~`tools.py`~~ | **삭제됨** (노션 MCP 로더. #9 Phase 1) |

## 실행 흐름

```
run.py → bot.py main()
   ├─ AsyncSqliteSaver(data/memory.sqlite) 열기
   ├─ _prune_old_threads()  7일 지난 대화기억 삭제 + VACUUM
   ├─ CommandTree에 슬래시 커맨드 4개 등록 (전역 sync)
   └─ alarm_loop 시작 (1분마다, load_schedules()를 매번 새로 읽음)

메시지 오면:
   author.id로 등록 확인 → context.set_current(그 사람)
   → thread_id = "사용자ID:KST날짜"  → 그 사람의 뇌로 만든 agent.ainvoke
   → 도구들이 context.active()에서 그 사람의 토큰·DB·계정을 꺼내 씀
   → finally에서 context.reset()
```

**도구 17개** = 루틴 4(조회/체크/쓰기/사진) + 과제 4 + 블레이버스 9

## 노션 연결 (중요)

- **REST 직접 호출뿐**이다. MCP 서버는 #9 Phase 1에서 걷어냈다.
  이유 ① 스키마 24개가 매 요청에 실려 비쌌고 ② 토큰이 subprocess 실행 시점에 고정돼
  사용자별 노션이 불가능했다 ③ 작은 모델이 24개 중에서 잘 못 골랐다.
- ⚠️ **행 구조를 코드에 박지 않는다.** 행을 만들 때 그 DB의 **기본 템플릿을 복사**한다
  (`_copy_template`). 노션에서 템플릿을 고치면 봇이 따라온다 — 실제로 작업 중
  '오늘 한 일' 헤딩이 하나 늘었고, 하드코딩이었으면 코드를 고쳐야 했다.
- 데일리루틴 데이터소스: `a5a0ffe9-306d-82c0-b5df-07a9145e578b` (주인 기본값)
  ("비서 전용 페이지" 안 "오프라인 데일리 루틴 (2)". config.py `NOTION_ROUTINE_DS_ID`)
  ⚠️ 옛 DB `df10ffe9-306d-834e-bae9-0717212de385`는 #3에서 폐기(참조 금지).
  (하루 1행, 체크박스 6개=코테/도착8시/운동/영어스피킹/어드민나잇/회고, 달성률 수식).
  ⚠️ 헤딩 "어드민 나잇"(공백) ≠ 체크박스 "어드민나잇"(붙임) — `_norm()`이 공백을 지워 같아진다.
  ⚠️ 회고 아래 h3 4개(오늘 한 일/오늘의 특별한 점/셀프 회고: 칭찬·반성).
  **h3 아무 칸이나 채우면 '회고' 체크박스가 켜진다** (소속 h2를 구조에서 찾아냄).
- 과제 제출 데이터소스: `f8a0ffe9-306d-8267-8473-8765b724ec67` (`NOTION_HOMEWORK_DS_ID`)
  ⚠️ '주차'는 **미리 만든 select 옵션(1~16, 방학)만** 받는다. 없는 값은 조회조차 400.
  ⚠️ '승인'·'반려'는 강사가 정하는 값이라 봇이 안 건드린다.
- ⚠️ 페이지 URL의 id는 **database_id**이고 우리가 쓰는 건 **data_source_id**다. 다른 값이라
  `GET /databases/{id}` 로 한 단계 더 물어봐야 한다 (`onboarding.discover_databases`).

## 블레이버스 연결 (중요 — 비공식 API)

- 공식 API·문서 없음. 웹앱이 쓰는 내부 API를 개발자도구로 확보해 그대로 호출한다.
  base: `https://api-v2.blaybus.com` (웹사이트 `www.blaybus.com`과 **다른 호스트**)
- 인증은 `POST /auth/user/sign-in`에 `{"loginId","password"}` → 쿠키 3종이 `Set-Cookie`로.
  httpx `AsyncClient`가 쿠키함을 자동 관리하므로 이후 요청엔 알아서 실린다.
- ⚠️ **쿠키만 보내면 401.** `cs_token` 쿠키 값을 `x-csrf-token` **헤더에도** 실어야 통과
  (double-submit). `cs_token`만 HttpOnly가 없는 이유가 이것 — JS가 읽어 헤더에 넣으라고.
- ⚠️ **쿠키 수명(3일) ≠ 안에 든 JWT 수명(1시간).** 쿠키가 있어도 죽은 토큰일 수 있으므로
  만료를 미리 계산하지 말고 **401을 받고 나서 재로그인**한다. 재시도는 **1회 고정**
  (비번 오류·계정 잠금도 401이라 무한 재시도하면 스스로 계정을 잠근다).
- refresh 대신 sign-in을 고른 이유: `refresh_token`도 3일이라 결국 손이 가고,
  토큰에 발급 IP가 박혀 있어 EC2에선 **그 서버에서 직접 로그인**해야 맞다.
- 엔드포인트 (`{pid}` = 그 사람의 프로젝트 ID)
  | 용도 | 요청 |
  |---|---|
  | 진행 중인 세션 | `GET /task-session/active` (taskId·제목·경과초 포함 → stop이 인자 불필요) |
  | 태스크 목록 | `GET /project/{pid}/task` (이름→ID 변환용) |
  | 시작 / 중지 | `POST /task/{taskId}/session/start` · `/stop` (바디 없음) |
  | 4시간 넘긴 중지 | 같은 `/stop`에 `{"selectedParts":[1,2]}` (거절 응답이 준 index) |
  | 3계층 트리 | `GET /project/{pid}/agenda?**completed=true**&page=1&pageSize=100` |
  | 그날 잰 시간 | `GET /task/calendar/user?from=&to=&userId=` (아젠다·워크·태스크·분을 한 번에) |
  | 내 프로젝트 목록 | `GET /legacy-project/my-project` |
- 아직 **안 쓰는** 엔드포인트 (2026-08-04 전체 cURL 수집. 기능 추가할 때 개발자도구 다시 안 열려고)
  | 용도 | 요청 |
  |---|---|
  | 태스크를 아젠다·워크로 필터 | `GET /project/{pid}/task?agendaId=&workId=` |
  | 아젠다 이름 서버 검색 | `GET /project/{pid}/agenda?search=&completed=false` |
  | 그날 완료 태스크 (userId 불필요) | `GET /task/daily-completed-task/user?date=<ISO>` |
  | 산출물 | `GET /project-output/project/{pid}/output?page=&pageSize=&orderByCreatedAt=` |
  | 워크 단건 · 아젠다 상태 집계 | `GET /project/{pid}/work/{id}` · `/project/{pid}/agenda/status-count` |
  | 멤버 | `GET /project/{pid}/member` · `/legacy-project/project/{pid}/member?type=Manager` |
  | 블레이버스 자체 AI | `GET /project/{pid}/ai/active-jobs` |
  | (무관) 접속 유지 폴링 | `POST /presence/ping` · `/presence/timer` — 봇엔 불필요 |
- ⚠️ 함정 3종 (전부 실제로 당함)
  1. `completed=true`는 "완료된 것만"이 **아니라 "완료된 것도 포함"**이다. 빼면 기본값이
     `false`라 끝난 아젠다가 통째로 안 보인다 (9개 → 4개)
  2. `calendar/user`는 `source`로 걸러야 한다. `completed`만 그날 기록이고,
     `upcoming`(미완료)은 `duration=null`인 채 **아무 날짜 조회에나 딸려 나온다**.
     `duration` 단위는 **분** (240=4시간 = 설명서의 태스크당 상한)
  3. `my-project`만 응답 키가 `data.list`가 아니라 **`data.message`**다
  4. **거절 응답에 답이 들어있다.** 서버는 한국어로 이유를 준다 — `raise_for_status()`로
     본문을 버리면 "400 에러"만 남아 원인을 못 찾는다. `_request()`가 한 곳에서
     `BlaybusError(message, code, data)`로 바꿔 던진다 (2026-08-04)
  5. **태스크당 4시간(240분) 상한.** 넘기면 `/stop`이 `STOP_SPLIT_REQUIRED`와 함께
     **분할안(`parts`)을 계산해서** 준다. 그 `index`만 `{"selectedParts":[1,2]}`로
     되돌려주면 저장된다 — 조각 시간을 우리가 계산하면 서버와 어긋난다.
     ⚠️ 분할하면 태스크가 `'오전 (1/2)'`·`'오전 (2/2)'`로 쪼개진다. **이름 중복의 출처가 이것**
- 사람마다 쿠키함을 따로 둔다(`_clients` dict + `_locks`). 하나를 공유하면 남의 401
  재로그인이 내 쿠키를 덮어쓴다. ⚠️ 잠금 없이 두면 요청 둘이 동시에 로그인한다.
- 검증 도구: `scripts/blaybus_probe.py` — 읽기 전용. `python -m scripts.blaybus_probe '<경로>'`

## 핵심 설계 결정

- 봇 1개(구현) + 내부는 LangGraph `create_react_agent`. **뇌가 사람마다 다르므로 그래프는
  사람별로 캐시**한다(열쇠 = provider·model·base_url·**키**). 키를 열쇠에서 빼면 남의 키로 요청이 나간다
- 웹 대화(`/chat`)는 여전히 **안 함**. 멀티유저는 **#9에서 함** (등록은 디스코드 모달로 —
  `webserver.py`는 인증도 TLS도 없는 평문 HTTP라 거기로 비밀번호를 받는 게 제일 위험)
- 노션: **MCP 폐기, REST 직접만.** 행 구조는 코드가 아니라 **DB 템플릿**이 정한다
- 블레이버스: **공식 API는 없지만 웹앱 내부 API가 있음** → 개발자도구로 요청을 복제해 자동화.
  봇이 직접 시작/종료 (2026-07-31). ⚠️ 비공식이라 예고 없이 깨질 수 있음
- 대화기억: **하루 단위로 끊는다**(thread_id에 KST 날짜). 어제 맥락을 끌고 오면 득보다 실.
  "어제 뭐 했더라"는 대화록이 아니라 **노션을 조회해서** 답할 일이다
- 자격증명: **폴백 금지.** 그 사람 값이 없으면 `.env`(주인) 것으로 때우지 않고 안내를 낸다.
  ⚠️ `or BLAYBUS_LOGIN_ID` 한 줄 때문에 미등록자에게 주인 아젠다가 노출된 적 있다(2계정 테스트)
- 부트캠프 과제 접목: LoRA→PTQ→GGUF 모델을 봇 1차 라우터로 / OS·네트워크·클라우드 과제는 봇에 얹음 (상세 `PLAN.md` §9)

## 멀티유저 (#9 Phase 3~4)

- 봇 프로세스는 **하나**. 사람들은 초대 링크로 자기 서버에 초대하거나 주인 서버에 들어온다.
  코드를 받아 각자 띄우는 게 아니다.
- `/register`(모달, 비밀만) → `/setup`(슬래시 인자, 취향) → `/status` → `/forget`
  ⚠️ 디스코드 모달은 **입력칸 최대 5개**라 자격증명만으로 꽉 찬다. 그래서 둘로 나눴다.
- 저장소 `data/users.sqlite` (memory.sqlite와 **파일 분리** — 그쪽은 시작 시 통째로 지운다)
- ⚠️ 블레이버스는 OAuth가 없어 **재사용 가능한 비밀번호를 들고 있어야** 한다.
  Fernet은 공격자가 아니라 `cat users.sqlite`를 막는 것이다. 서버가 털리면 `.env`의
  `CRED_KEY`도 같이 털린다 → **등록 모달에 그 사실을 한국어로 고지**하고 `/forget`을 둔다.
- ⚠️ `CRED_KEY`가 바뀌면 옛 암호문을 못 푼다. 그때 **그 사람만 건너뛰고 봇은 살아남는다**
  (안 그러면 `all_users()`가 죽고 알림 루프가 죽고 봇이 아예 못 뜬다)
- 요청 주인 전달은 `context.py`의 contextvars. 도구 시그니처를 안 건드린다.
  ponytail: 한 프로세스·한 이벤트루프 전제. 스레드풀로 가면 `RunnableConfig`로 바꿔야 한다
- ⚠️ **공개 채널에서는 답변이 서로 보인다.** 일반 메시지 답장은 ephemeral이 안 돼서 못 막는다
  → `/register` 완료 메시지에서 **DM 사용을 권한다**
- ⏭️ 미해결: LangSmith 추적이 프로세스 전역이라 **남의 대화가 주인 LangSmith로 간다**.
  공개배포 전에 꺼야 한다
- ⏭️ 미해결: 등록 시 **LLM 키를 검증하지 않는다**. 노션·블레이버스는 검증하는데 이것만 빠져서,
  키 칸에 `hello`를 넣어도 저장되고 대화할 때 `AuthenticationError`만 뜬다

## 진행 상황 (작업 로그 — 세션 끝날 때 갱신)

- 2026-07-15  #2 완료: LangGraph 뼈대 + 대화기억 + 노션 MCP + 데일리루틴 사진인증
- 2026-07-21  #3 완료: 첫 실전에서 터진 버그 5종 수정 + 관측/기억 정비
    - 안전벨트 recursion_limit(12)·타임아웃(90s)·예외처리 (bot.py) — 토큰 폭주 차단
    - 매 메시지 KST 오늘 날짜 주입 (agent.py) — 날짜 오배치 수정
    - 사진/체크박스 분리: `attach_routine_photo`에 `check`·`note` 옵션 (notion_tools.py)
    - 새 전용 DB로 이전 (config.py — 아래 '노션 연결' 참고)
    - LangSmith 관측 연결 (.env, 엔드포인트 apac)
    - 대화기억 최근 3개 윈도우 + 애매하면 되묻기 (agent.py `trim_messages`, persona.py)
- 2026-07-22  과제 트랙 **Phase 2 완료** (프로세스·스레드·메모리) → `reports/process-memory.md`
    - 관측 스크립트 2종 신설 (읽기 전용, 봇 코드와 무관):
      `scripts/inspect_process.sh`(A/B/C 스냅샷) · `scripts/watch_children.sh`(0.2초 감시)
    - npx 자식 프로세스 **생성→소멸 포착**: 봇→`npm exec`→`node` 2단 트리, 호출당 1쌍·수명 1~2초
      (스냅샷으론 안 잡힘 — 수명이 짧아 0.2초 감시 루프가 필요했음)
    - 소켓 3개 정체 확정: discord gateway / api.anthropic.com / apac.api.smith.langchain.com
      **LISTEN 없음** = 봇은 클라이언트 → Phase 3에서 `/health`로 문 내야 하는 근거
    - ⚠️ `ps -E`·`/proc/environ` **금지** (환경변수=토큰 덤프됨. `reports/`는 gitignore 아님)
    - 곁가지 버그 수정: `trim_messages` 창이 도구 2회+ 호출 시 빈 목록 → `BadRequestError`
      (`agent.py`) — 자르는 단위를 '메시지 개수'→'대화 턴'으로. `RECENT_WINDOW` 3→2
- 2026-07-24  과제 트랙 **Phase 3 완료** (`/health` + WireShark) → `secretary/webserver.py` (분석 정리는 블로그에만, 레포 보고서 없음)
    - `webserver.py`: FastAPI `/health`, uvicorn을 bot.py의 `asyncio.gather`로 봇과 **한 프로세스**에서 실행 (0.0.0.0:8000)
- 2026-07-27  과제 트랙 **Phase 4·5·6 완료** (Docker → EC2 → CI/CD)
    - 산출물: `Dockerfile` · `.dockerignore` · `docker-compose.yml` · `.github/workflows/deploy.yml`
    - Phase4 Docker: `python:3.11-slim` + Node20(노션 npx용) + `npm i -g @notionhq/notion-mcp-server`(런타임 다운로드 제거). `.env`는 **안 굽고** compose `env_file`로 주입, `data/`는 볼륨. 로컬 검증 OK
    - Phase5 EC2: Ubuntu 26.04 t3.micro, `docker.io`+`docker-compose-v2`(공식repo 대신 우분투 패키지 — 최신 OS 대응), **swap 2GB**(RAM 1GB 빌드 OOM 예방), `git clone` + `scp .env`. 폰에서 외부 `/health` 확인 ✅
    - Phase6 CI/CD: `deploy.yml`(appleboy/ssh-action) — **week11 push → SSH → `git pull`+재빌드+`/health`**. GitHub Secrets 3개(`EC2_HOST`/`EC2_USER`/`EC2_SSH_KEY`), 보안그룹 22 임시 개방(GitHub 접속용)
    - ⚠️ 함정 3종(다음에 또 만남):
      1) week11을 **main에서** 팠더니 Phase3(`/health`) 코드 누락 → 컨테이너에 `webserver.py` 없어 8000 안 열림. `git merge week10`로 해결. **교훈: 브랜치 베이스 = 의존 코드가 있는 곳**
      2) `EC2_SSH_KEY`를 손으로 붙여넣어 줄바꿈 깨짐 → `ssh: no key found`. `cat pem | pbcopy`로 통째 복사
      3) 배포 직후 `curl` 1회 → `Connection reset`(앱 미준비). 헬스체크를 **뜰 때까지 재시도** 루프로 (readiness 개념)
    - 브랜치 상태: **week11 = week10(+Phase3) + Docker + CI/CD**
      (2026-07-31 기준 week10·11·12·local-router는 **전부 main에 병합됨** — 브랜치는 과거 이정표일 뿐)
    - EC2는 과금 우려로 **종료**(코드 전부 week11에 있어 재배포 가능), Elastic IP 반납
- 2026-07-28  **12주차 vLLM 과제 필수① 완료** → `notebooks/vllm_qwen35.ipynb` (브랜치 `week12`)
    - 결정: 서빙 위치 **Colab Pro L4(24GB)** / 모델 **`Qwen/Qwen3.5-4B`** (2026-02 릴리스, 소형 0.8B·2B·4B·9B 중)
    - 측정값: 가중치 8.61 GiB·로딩 30초 / KV 캐시 8.84 GiB = **236,274 토큰** / 최대 동시성 **28.84x** / 기동 총 **약 7분**
    - 🎯 **thinking 모드는 이 용도에 못 씀**: "이건 어떤 요청?" 한 줄 질문에 `max_tokens=2048`을 다 쓰고도 **72.5초 / 답변 None**.
      끄면(`chat_template_kwargs.enable_thinking=false`) **0.7초 / 19토큰**에 정상 답변. 느린 게 아니라 **답을 못 냄**
    - ⚠️ 함정 3종: ① 모델카드 권장 `--max-model-len 262144`는 확보 캐시 236,274토큰보다 커서 **기동 실패**(8192로 낮춤)
      ② vLLM nightly가 torch CUDA 13.2를 끌어와 Colab의 torchaudio(12.8)와 충돌 → transformers가 *없는* 경우만 방어하고
      *깨진* 경우(RuntimeError)는 못 막음 → **`pip uninstall -y torchaudio`로 해결**
      ③ Colab에서 `!vllm serve`는 셀을 영구 점유 → `subprocess.Popen` + `/v1/models` 폴링(11주차 readiness와 같은 패턴)
    - 선택② (Docker+GPU EC2)는 **보류** — Colab Pro로 정해서 불필요
    - 보고서는 **블로그에만** 정리 (Phase 3~6과 같은 방식. 레포엔 `reports/vllm.md` 안 만듦)
- 2026-07-28  **봇의 뇌를 로컬 Qwen으로 바꿔보는 실험** (과제 아님 · 브랜치 `feature/local-router`)
    → `notebooks/vllm_serve_for_bot.ipynb` (도구 호출 서버 + cloudflared 터널), `scripts/chat_cli.py`
    - 스위치 설계: `.env`에 **`VLLM_BASE_URL`이 있을 때만** `ChatOpenAI`로 갈아끼움 (`agent.py` `_build_model`).
      없으면 기존 Claude 그대로 = 봇 동작 무변화. vLLM이 OpenAI 호환이라 **그래프 조립부는 한 줄도 안 바뀜**
    - ✅ **도구 호출 성공**: 4B가 "오늘 운동 다녀왔어"에서 `check_routine {"item": "운동"}`까지 채움
    - ⚠️ 함정 3종(이 실험 고유):
      ① 도구 파서는 **`hermes` 아니고 `qwen3_xml`**. Qwen3 문서는 hermes를 안내하지만(Qwen2.5 템플릿이 Hermes 스타일이라)
         **Qwen3.5는 도구 표기가 XML로 바뀜** → hermes면 `"auto" tool choice requires...` 400
      ② **옛 서버가 8000번을 점유**하면 새 서버는 바인딩 실패로 죽는데, 폴링은 옛 서버의 200을 보고 "준비 완료" 오판.
         → 기동 전 `pkill -f "vllm serve"` + 준비 후 **인증 없는 요청에 401이 오는지**로 내 서버인지 확인
      ③ 실험 후 **`.env`의 `VLLM_BASE_URL`을 비우는 걸 잊으면** 봇이 죽은 터널을 부른다 (터널은 세션과 함께 사라짐)
    - ⏭️ 미해결: 대화가 쌓이면 `BadRequestError` — `--max-model-len 8192`가 원인으로 추정.
      8192는 라우터(짧은 분류)용으로 정한 값인데 봇은 **노션 도구 24개 스키마**를 매 요청에 싣는다.
      확보 캐시가 236,274토큰이라 **32768로 올려도 동시성 7x** → 다음 실험 때 검증할 것
    - ⏭️ 미해결: CLI엔 디스코드 같은 이미지 URL 자동첨부가 없어 사진 인증 경로는 테스트 못 함 (실험환경 한계, 모델 문제 아님)
- 2026-07-31  **블레이버스 API 연결** (브랜치 `main`) → `secretary/blaybus_tools.py` · `scripts/blaybus_probe.py`
    - "API 없어서 자동화 못 함"이라던 판단을 뒤집음: **공개 API가 없을 뿐 웹앱 내부 API는 있었다.**
      개발자도구로 요청을 복제 → 디스코드 대화만으로 시작/중지 가능 (스펙은 위 '블레이버스 연결' 절)
    - 도구 3개: `blaybus_status` / `blaybus_start(task_title)` / `blaybus_stop`
      · 이름→ID 해석은 **완전일치 우선**. 안 그러면 "오후"가 `오후 (1/2)`·`(2/2)`까지 걸려 매번 되묻는다
      · `stop`은 인자 없음 — `/task-session/active` 응답에 taskId가 들어있어 스스로 찾는다
    - 순서가 핵심이었음: **① 인증 뚫리나(90줄 프로브) → ② 주소 파악 → ③ 도구 구현.**
      ②③을 먼저 했으면 ①에서 막혔을 때 전부 버렸을 것
    - `agent.py`는 조건부 4줄만 추가. 그래프 조립부 무변화 — vLLM 스위치와 같은 방식
    - **새 의존성 0개** (httpx는 노션 업로드용으로 이미 있었음)
    - ⚠️ 함정 (다음에 또 만남):
      1) 개발자도구 Name 열은 **경로의 마지막 조각만** 표시 (`start` ← 실제 `/task/{id}/session/start`).
         전체 주소는 우클릭 → **Copy URL**. 지도를 통째로 뜨려면 **Copy all listed URLs**
      2) `Copy all listed URLs`는 **주소만 주고 메서드·바디는 안 준다.** 생성·수정용 cURL은 따로 떠야 함.
         그리고 **버튼을 눌러야 찍힌다** — 가만히 두면 `presence/ping` 폴링만 쌓임
      3) **404는 인증 실패가 아니라 주소 오류.** 401/403(=너 누구냐)과 반드시 구분할 것.
         프로브가 이걸 뭉뚱그려 "실패"로 찍어서 엉뚱한 데(헤더 추가)를 팔 뻔했다
      4) 실험 스크립트는 **성공 케이스를 먼저** 돌린다. `x-csrf-token` 없이 한 번 보내면
         서버가 그 세션을 끊어서, 실패를 먼저 하면 뒤의 성공 케이스까지 401이 된다
      5) `.env` 값은 **작은따옴표**로 감쌀 것. `PW=abc #def`는 공백 뒤 `#`부터 주석,
         큰따옴표는 `\t`를 탭으로 해석해 **조용히 다른 값**이 된다 (에러도 안 남)
      6) `len()`은 타입을 안 가린다 — 셀프테스트가 `len(dict)`(키 개수 2)를 세서 **틀린 이유로 통과**했다.
         `isinstance`까지 단언할 것
    - ⏭️ 미구현: 아젠다/태스크 **생성·수정·삭제** — POST 바디를 모른다(생성 버튼을 안 눌러 cURL 미확보).
      다음 세션에서 cURL 수집 후 추가 예정. ⚠️ 삭제 도구는 **테스트용 아젠다에서만** 검증할 것
    - 블로그: #8-1(API 찾기·인증 뜯어보기) / #8-2(설계 결정·구현) / #8-3(생성·이름변경)
- 2026-08-03  **#9 비서 다듬기 + 멀티유저** (브랜치 `feature/debug`) — Phase 0~4
    - **Phase 0** 날짜 리셋: thread_id에 KST 날짜 → 하루 단위로 대화가 새로 시작.
      시작 시 7일 지난 스레드 삭제 + VACUUM (**77MB → 0.02MB**)
      · ⚠️ WAL 모드라 VACUUM만으론 파일이 안 줄어든다. `PRAGMA wal_checkpoint(TRUNCATE)`까지 해야
        봇이 도는 중에도 반납된다 (안 하면 껐을 때만 줄어들어 로그가 거짓말을 한다)
      · ⚠️ `date.today()`는 시스템 로컬 날짜다. UTC 컨테이너에서 **KST 0~9시 기록이 어제 행으로** 갔다
      · 곁가지: `scripts/chat_cli.py`도 thread_id에 날짜를 붙였다 — 안 그러면 정리에 걸려 매번 날아간다
    - **Phase 1** 노션 MCP 제거 → 전용 도구 (**도구 28개 → 17개**). `tools.py` 삭제,
      Dockerfile에서 Node·npm 통째로 제거. `homework_tools.py` 신설
      · 행 구조를 **템플릿 복사**로 (하드코딩 폐기). 작업 도중 실제로 '오늘 한 일' 헤딩이 늘어
        이 결정이 즉석에서 증명됐다
      · ⚠️ 조회 응답을 그대로 되보내면 400. `paragraph.icon: null` 같은 **null 필드를 빼야** 한다
      · h3 아무 칸이나 채우면 소속 h2('회고') 체크박스가 켜진다 — 목록이 아니라 **구조**를 읽는다
    - **Phase 2** 알림 `alarms.py` (백로그의 '오프라인 알람'이 이것). 1분 루프, **LLM 안 부름**
      · `load_schedules()` 하나만 갈아끼우면 되게 설계 → Phase 3에서 실제로 그것만 바꿨다
      · ⚠️ 채널 ID와 사용자 ID가 **둘 다 숫자**라 값만 봐선 구분이 안 된다 → 런타임 판별(`resolve_target`)
      · ⚠️ `discord.abc.User`로 `isinstance`하면 Protocol이라 속성 접근에서 터진다
    - **Phase 3** 등록 `users.py`·`onboarding.py`·`commands.py`. Fernet 암호화, 관문(`_allowed`)
      · 노션 **페이지 URL 하나로 DB들을 발견**한다 (사람에게 data_source_id를 요구할 순 없다)
      · ⚠️ `copy_global_to` + 전역 sync를 둘 다 하면 커맨드가 **두 벌로** 보인다
      · ⚠️ `/setup`이 `.env`에 밀려 먹통이던 버그 — 같은 대상이면 **나중 것(users)이 이겨야** 한다
    - **Phase 4** 사용자별 실행 `context.py`(contextvars). 도구가 그 사람의 토큰·DB·계정을 씀
      · 블레이버스 `_client` 하나 → **사람별 쿠키함 + 잠금**
      · 🔴 **2계정 테스트에서 실제 유출**: 블레이버스 미등록자가 물었더니 주인 아젠다가 나왔다.
        원인은 `or BLAYBUS_LOGIN_ID` 폴백 한 줄. **폴백 5군데 전부 제거** →
        없으면 `NotConfigured`로 안내. 조회라 다행이지 '시작해줘'였으면 주인 계정에 기록됐다
    - 블로그: #9 예정
- 2026-08-03  **#9 Phase 5** LLM 키 검증 + 인증 오류 안내 (커밋 `6aad3f4`)
    - `onboarding.verify_llm()` — 등록 때 그 키로 실제 한 번 찔러본다.
      ⚠️ 그 전엔 키 칸에 `hello`를 넣어도 저장되고, 대화할 때야 `AuthenticationError`가 떴다
    - 모델 기본값을 provider별로: `openai`=`gpt-4o-mini`, `anthropic`=`claude-sonnet-4-5`.
      바꾸려면 `/setup`의 `model` 인자 (vLLM도 같은 경로)
    - `/users`는 **관리자 전용**(`default_permissions`), `/health`는 **개수만** — 인증 없는 문이라
    - 봇이 인증 오류를 받으면 예외 이름을 보고 "키를 다시 넣어 주세요"로 안내 (bot.py)
- 2026-08-04  **블레이버스 이름 중복 대응** (아직 미커밋 → `feature/debug`)
    - 실제 데이터: '수요일' 워크 **5개**, '오전' 태스크 **11개**. `주차 > 요일 > 오전/오후`
      구조라 중복이 정상인데, `add_task`·`rename`·`start`에 경로 인자가 없어
      **되물어도 아가씨가 답을 전달할 방법이 없었다**
    - 🔴 그래서 모델이 `rename` 대신 `add_work`로 **우회해 쓰레기 워크를 만들었다**(8/4 아침 사고)
    - 세 도구에 `agenda_title`·`work_title` 추가. 되물음에 **경로**를 보여준다
      (`워크 '수요일'`만 반복되면 고를 수가 없다 → `_path_of`)
    - `blaybus_start`는 **완료된 태스크를 후보에서 제외**. 같은 워크에 같은 이름 3개라
      경로로도 특정이 안 된다. 블레이버스도 이어하기는 재시작이 아니라 복제다
    - `persona.py`에 "못 찾겠다고 `add_work`로 우회하지 마라" 명시
    - ⚠️ 셀프테스트가 **이름이 전부 유일한 트리**만 써서 이 버그를 원천적으로 못 잡았다
      → 중복 이름 트리(`12주차>수요일>오전`, `13주차>수요일>오전`)를 추가
- 🔜 미구현 기능(백로그): 밤에 블라인드 잔소리 (알림 본체는 Phase 2에서 완료)
- 🎓 과제 트랙 (2026-07-21 기획): 부트캠프 OS/네트워크·클라우드 과제를 봇에 얹음
    - 순서: 2 프로세스/메모리 → 3 `/health`+WireShark → 4 Docker → 5 EC2 → 6 CI/CD
      (Phase 1 서술형5문제는 스킵. LoRA→GGUF(백로그 ⑦)는 별도 Qwen 모델 필요 → 클라우드 트랙 뒤로 미룸)
    - 핵심 개념: 런타임 파이프라인 ≠ 배포 파이프라인, EC2=실행 장소. `/health`만 파이프라인에 문 추가
    - 상세: `PLAN.md` '9. 과제 트랙'. 한 단계씩 접근안→승인→실행
- 상세 기획·백로그: `PLAN.md` 참고

## 남은 일 (2026-08-04 기준)

| 순위 | 할 일 | 상태 |
|---|---|---|
| 1 | **Phase 6 — 노션 한 페이지를 여럿이 공유** | 조사·계획 끝, 미착수 |
| 2 | **main 병합 + EC2 재배포** | `deploy.yml`이 main 트리거. Elastic IP는 반납한 상태 |
| 3 | **LangSmith 전역 추적 끄기** | 🔴 남의 대화가 주인 계정으로 간다. 공개 전 필수 |
| 4 | 온보딩 안내문 (캡처 포함) | 실사용자가 토큰 발급·통합 만들기에서 막힘 |
| 5 | 블로그 #9 | |

**Phase 6 요지** (상세 계획은 착수할 때 다시 세운다)
- 지금은 **각자 노션을 복제**하는 방식인데, 아가씨의 실제 사용처는 **팀이 한 페이지를 공유**하는 것
- 검증 끝: `GET /users`는 **403**(통합 토큰은 사용자 목록 금지)이지만
  `GET /users/me`의 `bot.owner.user.id`가 **통합을 만든 사람**을 알려준다 →
  사용자에게 노션 id를 묻지 않아도 된다. ⚠️ **각자 자기 계정으로 통합을 만들어야** 한다
- people 필터(`{"people":{"contains":id}}`) 동작 확인 — 과제 DB 4행 중 자기 2행만 반환
- 🔴 진짜 문제: `notion_tools._find_row()`가 날짜로만 찾아 `rows[0]`을 쓴다.
  한 DB를 3명이 쓰면 같은 날 행이 3개 → **남의 행을 고친다**
- 공유 모드는 **자동 판별하지 말고 명시적으로 받는다.** person_id가 있다고 무조건 필터를 걸면
  복제 사용자의 기존 행엔 '사람'이 비어 있어 아무것도 못 찾는다
- ⚠️ people **쓰기**는 아직 미검증 — 첫 단계에서 테스트 행으로 확인할 것

## 규칙

- **커밋/푸시는 사용자가 직접** 한다 (공개 레포, Co-Authored-By 등 AI 흔적 금지)
- 파일 작업·검증은 도와도 git commit/push/PR은 사용자 몫

## 알아둘 함정

- python3.9로 venv 만들면 안 됨 (3.11 필수)
- 디스코드 답변 2000자 제한 (bot.py에서 자름). ⚠️ 슬래시 커맨드 **모달은 입력칸 5개**가 상한
- 봇이 **로컬에서 돌면 맥을 끄는 순간 전원의 봇이 오프라인**이 된다 (남을 받으려면 EC2 필요)
- `.env`·`config.py`를 바꾸면 봇을 껐다 켜야 반영됨. 재시작 시 낡은 프로세스까지 죽일 것(`pkill -f run.py`) — 안 그러면 옛 설정으로 계속 돎 (#3에서 크게 헤맴)
- 반드시 `~/project`에서 `claude` 실행 (다른 곳에서 켜면 이 CLAUDE.md가 자동 로드 안 됨)
- 블레이버스는 **비공식 API**라 예고 없이 스펙이 바뀔 수 있음. 깨지면 개발자도구로 다시 확보
- 프로젝트 루트 밖 스크립트(`/tmp/*.py` 등)에서 `secretary`를 import하려면 `PYTHONPATH=.` 필요.
  `scripts/`의 것은 `python -m scripts.<이름>`으로 돌리면 불필요
- 브랜치를 **미리 파두면 낡는다.** 그 사이 main이 앞서가면 `git switch` 시 "덮어쓴다"며 거부됨
  → 작업 직전에 파거나, 낡았으면 `git branch -D` 후 다시 생성 (**교훈: 브랜치는 작업 직전에**)
- 블레이버스에 **삭제 도구가 없다.** 잘못 만든 아젠다/워크/태스크는 아가씨가 **웹에서 손으로**
  지워야 한다 (POST 바디를 몰라 미구현). 그래서 테스트는 잔여물이 남는 걸 전제로 할 것
- 대화기억을 손으로 지울 땐 **`checkpoints`·`writes` 둘 다** 지우고
  `PRAGMA wal_checkpoint(TRUNCATE)`까지 한다. `checkpoints`만 지우면 도구 호출 기록이 고아로 남고,
  WAL 체크포인트를 빼면 파일 크기가 안 줄어 로그가 거짓말을 한다. **봇은 끄고 할 것**
  (`data/`의 `memory.sqlite.bak`·`memory1.sqlite`는 7일 정리 대상이 아닌 옛 잔여물이다)

<!--
========================================================================
다음 기능 킥오프 프롬프트 (새 채팅 첫 메시지로 복붙)
========================================================================
공주비서 프로젝트다. CLAUDE.md는 자동 로드됐을 테니
'진행 상황'과 '남은 일' 두 절을 먼저 읽어. 필요하면 PLAN.md와 secretary/도 훑고.

오늘 할 일: <남은 일 표에서 하나 골라 쓰기. 예: Phase 6 — 노션 한 페이지 공유>
브랜치는 <이름>에서 작업할게. 착수 전에 계획부터 보여줘.
========================================================================
-->
