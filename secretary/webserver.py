"""상태 서버 — 봇에 붙이는 아주 작은 HTTP '문' (/health).

왜 있나:
    공주비서는 원래 디스코드·Anthropic·노션으로 '나가기만' 하는 클라이언트라,
    바깥에서 접속받을 문(LISTEN 소켓)이 하나도 없다. (Phase 2 관측으로 확인)
    WireShark로 캡처하려면 '들어오는 서버 트래픽'이 있어야 해서, 여기서 문을 하나 낸다.

설계 두 가지:
    1) 일부러 평문 HTTP다. HTTPS로 가리지 않아야 WireShark에서 내용이 그대로 보인다
       (평문 HTTP vs 암호화 HTTPS 대비 실험 = 보고서의 결론).
    2) 봇과 '같은' asyncio 루프에서 함께 돈다(bot.py에서 gather). 별도 프로세스로
       띄우지 않으므로, 프로세스가 여전히 1개 → Phase 2 관측 스크립트를 그대로 재사용.
"""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI

from secretary import users
from secretary.config import HEALTH_HOST, HEALTH_PORT

# FastAPI 앱. /docs(자동 문서)도 함께 열려, 브라우저로 http://IP:8000/docs 확인 가능.
app = FastAPI(title="공주비서 상태 서버")

# 봇 클라이언트를 여기에 걸어두면 /health가 서버 수를 셀 수 있다.
# (webserver가 bot을 import하면 순환이라, bot이 자기를 넘겨주는 방향으로 만든다)
_client = None


def attach_client(client) -> None:
    global _client
    _client = client


@app.get("/health")
async def health() -> dict:
    """살아있는지 + 몇 명이 쓰는지. 평문 JSON이라 폰 브라우저로도 바로 보인다.

    ⚠️ 이 문에는 인증이 없다. 그래서 **개수만** 낸다 — 서버 이름이나 사용자 ID는
       넣지 않는다.
    """
    body = {"status": "ok", "service": "공주비서"}
    if _client is not None:
        body["guilds"] = len(_client.guilds)  # 초대된 서버 수
    try:
        body["users"] = len(users.all_users()) if users.enabled() else 0
    except Exception:  # noqa: BLE001 - 통계 때문에 헬스체크가 실패하면 안 된다
        body["users"] = None
    return body


def build_health_server() -> uvicorn.Server:
    """/health 문을 여는 uvicorn 서버 객체를 만들어 돌려준다.

    실제 실행(await server.serve())은 bot.py가 봇과 함께 gather로 돌린다.
    """
    config = uvicorn.Config(
        app,
        host=HEALTH_HOST,  # 0.0.0.0 → 폰 등 외부 기기 접속 허용 (config.py 주석 참고)
        port=HEALTH_PORT,  # 8000
        log_level="info",
    )
    server = uvicorn.Server(config)

    # uvicorn은 기본적으로 SIGINT/SIGTERM 핸들러를 자기가 가로챈다. 그러면 run.py의
    # KeyboardInterrupt(Ctrl+C) 종료 흐름과 어긋날 수 있어, 신호 처리는 끈다.
    # (종료는 asyncio.run 쪽에 맡긴다.)
    server.install_signal_handlers = lambda: None

    return server
