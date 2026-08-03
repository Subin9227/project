# 공주비서 컨테이너 이미지 레시피 (Phase 4)
#
# 왜 이렇게 굽나:
#   순수 Python 이미지다. 예전엔 노션 MCP 서버를 `npx`로 띄우느라 Node 20까지
#   같이 심었지만, #9 Phase 1에서 노션을 REST 직접 호출로 바꾸면서 Node·npm·
#   MCP 설치가 통째로 빠졌다. (이미지가 수백 MB 가벼워진다)
#
# 비밀값(.env)은 여기서 굽지 않는다. 런타임에 docker-compose가 주입한다.
# → 이미지가 유출돼도 토큰은 새지 않는다.

FROM python:3.11-slim

# 로그가 버퍼에 갇히지 않고 바로 보이게 (docker logs 즉시 확인용)
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# --- Python 의존성 ---
# requirements.txt만 먼저 복사해 설치하면, 코드만 바뀔 때 이 레이어가
# 캐시돼 재빌드가 빨라진다 (Docker 레이어 캐시 활용).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- 앱 코드 ---
# .dockerignore가 .env·data·.venv 등을 걸러주므로 필요한 것만 들어온다.
COPY secretary/ ./secretary/
COPY run.py .

# /health 문을 여는 포트. (실제 게시는 docker-compose의 ports가 한다.)
EXPOSE 8000

# 컨테이너 건강검진: 30초마다 /health를 두드려 200이 아니면 unhealthy로 표시.
# (Python 표준 라이브러리만 써서 curl 없이도 동작 — 슬림 이미지에 curl이 없어도 OK)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"

# 봇 기동. run.py가 asyncio 루프에서 디스코드 + /health 서버를 함께 돌린다.
CMD ["python", "run.py"]
