# 공주비서 (Princess Secretary)

디스코드 집사봇. 사용자가 자연어로 말하면 노션 등 기록처를 대신 채워주는 비서.
'닛몰캐쉬 잘자요 아가씨' 말투로 사용자를 "아가씨"라 부른다.

## 실행

- **Python 3.11+ 필수** (3.9로 venv 만들면 최신 패키지 설치 실패)
- **Node.js 필요** — 노션 MCP 서버를 `npx`로 띄운다 (첫 실행 시 자동 다운로드)
- 진입점: `.venv/bin/python run.py`
- `.env` 필수 키 4개: `DISCORD_BOT_TOKEN`, `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `NOTION_TOKEN`
  (선택: `LANGSMITH_TRACING`·`LANGSMITH_API_KEY`·`LANGSMITH_ENDPOINT`·`LANGSMITH_PROJECT` — 관측용, #3 추가)
  (선택: `BLAYBUS_LOGIN_ID`·`BLAYBUS_PASSWORD` — **있을 때만** 블레이버스 도구 3개 장착.
   `BLAYBUS_PROJECT_ID` 기본 4983. ⚠️ 비밀번호에 `#`이 있으면 작은따옴표로 감쌀 것)
  (선택: `VLLM_BASE_URL`·`VLLM_API_KEY`·`VLLM_MODEL` — 있으면 Claude 대신 로컬 모델, #12 실험)
- `.env` / `.venv/` / `data/` 는 gitignore (커밋 금지)

## 구조 (secretary/ 패키지)

| 파일 | 역할 |
|---|---|
| `config.py` | env·토큰·경로 로드 + 노션 상수(루틴 DS ID, API base, 5MB 한도) |
| `persona.py` | '아가씨' 집사 시스템 프롬프트 |
| `tools.py` | 노션 MCP 도구 로드 (`MultiServerMCPClient` → 도구 24개) |
| `notion_tools.py` | MCP가 못 하는 파일 업로드/이미지 블록을 노션 REST 직접 호출(httpx) |
| `blaybus_tools.py` | 블레이버스 **비공식 API** 직접 호출(httpx). 도구 3개(status/start/stop) |
| `webserver.py` | FastAPI `/health`. bot.py의 `asyncio.gather`로 봇과 **한 프로세스**(Phase 3) |
| `agent.py` | `create_react_agent` (모델 + MCP도구 + 커스텀도구 + SqliteSaver 기억) |
| `bot.py` | 디스코드 게이트웨이. on_message → agent.ainvoke → 답장 |
| `run.py` (루트) | `asyncio.run(main())` 진입점 |

## 실행 흐름

```
run.py → bot.py main() → agent.py build_agent()
   ├─ AsyncSqliteSaver(data/memory.sqlite) 열기  (thread_id = 디스코드 채널ID)
   ├─ tools.py load_tools() → npx 노션 MCP 서버 → 도구 24개
   ├─ notion_tools.ROUTINE_TOOLS(사진인증 1개)
   └─ BLAYBUS_LOGIN_ID 있으면 blaybus_tools.BLAYBUS_TOOLS(3개) → 총 28개
메시지 오면: Claude가 도구를 이름으로 골라 호출 → 결과 → 페르소나 답장
(webserver.py의 uvicorn은 bot.py에서 asyncio.gather로 같은 프로세스에 함께 실행)
```

## 노션 연결 (중요)

- MCP 경로: `tools.py` → npx `@notionhq/notion-mcp-server`(공식 오픈소스) → 노션 REST.
  읽기·검색·텍스트용. 파일 업로드/이미지 블록은 **없음**.
- 직접 경로: `notion_tools.py` → httpx로 노션 REST 직접. 사진 업로드 전용.
- 둘 다 같은 `NOTION_TOKEN` 사용. 대상은 **개인 워크스페이스**(org 아님).
- 데일리루틴 데이터소스: `a5a0ffe9-306d-82c0-b5df-07a9145e578b`
  ("비서 전용 페이지" 안 "오프라인 데일리 루틴 (2)". config.py `NOTION_ROUTINE_DS_ID`)
  ⚠️ 옛 DB `df10ffe9-306d-834e-bae9-0717212de385`는 #3에서 폐기(참조 금지).
  (하루 1행, 체크박스 6개=코테/도착8시/운동/영어스피킹/어드민나잇/회고, 달성률 수식).
  ⚠️ 헤딩 "어드민 나잇"(공백) ≠ 체크박스 "어드민나잇"(붙임).

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
- 엔드포인트 (`{pid}` = `BLAYBUS_PROJECT_ID`)
  | 용도 | 요청 |
  |---|---|
  | 진행 중인 세션 | `GET /task-session/active` (taskId·제목·경과초 포함 → stop이 인자 불필요) |
  | 태스크 목록 | `GET /project/{pid}/task` (이름→ID 변환용) |
  | 시작 / 중지 | `POST /task/{taskId}/session/start` · `/stop` (바디 없음) |
- 검증 도구: `scripts/blaybus_probe.py` — 읽기 전용. `python -m scripts.blaybus_probe '<경로>'`

## 핵심 설계 결정

- 봇 1개(구현) + 내부는 LangGraph **단일 `create_react_agent`**. 웹훅 페르소나 4요원은 **목표(미구현, PLAN §1)** — 팔다리 늘면 리팩터링
- 웹 대화(`/chat`)·멀티유저는 **안 함/보류** (보안·권한 복잡, PLAN §9). Streamlit은 읽기 전용 조망만
- 노션: 개인 워크스페이스에 채우고 org(게스트)엔 사용자가 수동 복붙
- 블레이버스: **공식 API는 없지만 웹앱 내부 API가 있음** → 개발자도구로 요청을 복제해 자동화.
  봇이 직접 시작/종료 (2026-07-31, 아래 '블레이버스 연결' 참고). ⚠️ 비공식이라 예고 없이 깨질 수 있음
- 도구 전략: 있는 건 MCP, 빠진 건 REST 직접
- 부트캠프 과제 접목: LoRA→PTQ→GGUF 모델을 봇 1차 라우터로 / OS·네트워크·클라우드 과제는 봇에 얹음 (상세 `PLAN.md` §9)

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
    - 블로그: #8-1(API 찾기·인증 뜯어보기) / #8-2(설계 결정·구현)
- 🔜 미구현 기능(백로그): 오프라인 알람 — `alarms.py` + `discord.ext.tasks`, 하루 4번 토글 DM
      (07:25 켜 / 11:45 꺼 / 12:58 켜 / 17:48 꺼), 밤엔 블라인드 잔소리.
    - **블레이버스 API가 붙었으므로 "링크 보내고 클릭은 사용자"가 아니라 봇이 직접 토글 가능.**
      알림은 확인·잔소리용으로 격하
    - 하드코딩 페르소나 템플릿 (Claude 호출 없이). .env 필요: `DISCORD_USER_ID`(또는 채널ID)
- 🎓 과제 트랙 (2026-07-21 기획): 부트캠프 OS/네트워크·클라우드 과제를 봇에 얹음
    - 순서: 2 프로세스/메모리 → 3 `/health`+WireShark → 4 Docker → 5 EC2 → 6 CI/CD
      (Phase 1 서술형5문제는 스킵. LoRA→GGUF(백로그 ⑦)는 별도 Qwen 모델 필요 → 클라우드 트랙 뒤로 미룸)
    - 핵심 개념: 런타임 파이프라인 ≠ 배포 파이프라인, EC2=실행 장소. `/health`만 파이프라인에 문 추가
    - 상세: `PLAN.md` '9. 과제 트랙'. 한 단계씩 접근안→승인→실행
- 상세 기획·백로그: `PLAN.md` 참고

## 규칙

- **커밋/푸시는 사용자가 직접** 한다 (공개 레포, Co-Authored-By 등 AI 흔적 금지)
- 파일 작업·검증은 도와도 git commit/push/PR은 사용자 몫

## 알아둘 함정

- python3.9로 venv 만들면 안 됨 (3.11 필수)
- MCP 도구는 호출마다 npx 세션이 새로 뜸 (첫 노션 호출 약간 느림 — 뼈대 단계라 의도적)
- 디스코드 답변 2000자 제한 (bot.py에서 자름)
- `.env`·`config.py`를 바꾸면 봇을 껐다 켜야 반영됨. 재시작 시 낡은 프로세스까지 죽일 것(`pkill -f run.py`) — 안 그러면 옛 설정으로 계속 돎 (#3에서 크게 헤맴)
- 반드시 `~/project`에서 `claude` 실행 (다른 곳에서 켜면 이 CLAUDE.md가 자동 로드 안 됨)
- 블레이버스는 **비공식 API**라 예고 없이 스펙이 바뀔 수 있음. 깨지면 개발자도구로 다시 확보
- 프로젝트 루트 밖 스크립트(`/tmp/*.py` 등)에서 `secretary`를 import하려면 `PYTHONPATH=.` 필요.
  `scripts/`의 것은 `python -m scripts.<이름>`으로 돌리면 불필요
- 브랜치를 **미리 파두면 낡는다.** 그 사이 main이 앞서가면 `git switch` 시 "덮어쓴다"며 거부됨
  → 작업 직전에 파거나, 낡았으면 `git branch -D` 후 다시 생성 (**교훈: 브랜치는 작업 직전에**)

<!--
========================================================================
다음 기능 킥오프 프롬프트 (새 채팅 첫 메시지로 복붙)
========================================================================
공주비서 프로젝트다. 먼저 맥락 파악:
- CLAUDE.md는 자동 로드됐을 거야. 특히 '진행 상황' 섹션을 봐
  (지금까지 뭘 했고 다음에 뭘 할지가 거기 있음)
- PLAN.md 읽기 (전체 기획, 팔다리 8개)
- secretary/ 코드 훑기

오늘 할 일: 블레이버스 + 오프라인 알람.
(방향은 CLAUDE.md·PLAN.md ④ 참고)
========================================================================
-->
