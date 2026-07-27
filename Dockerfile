# 공주비서 컨테이너 이미지 레시피 (Phase 4)
#
# 왜 이렇게 굽나:
#   이 봇은 Python으로 돌지만, 노션 도구를 쓰려면 Node.js도 필요하다.
#   tools.py가 노션 MCP 서버를 `npx @notionhq/notion-mcp-server`로 자식
#   프로세스로 띄우기 때문. 그래서 한 이미지 안에 Python 3.11 + Node 20을
#   같이 심는다. (Node 없으면 봇은 떠도 노션 호출 순간 죽는다.)
#
# 비밀값(.env)은 여기서 굽지 않는다. 런타임에 docker-compose가 주입한다.
# → 이미지가 유출돼도 토큰은 새지 않는다.

FROM python:3.11-slim

# 로그가 버퍼에 갇히지 않고 바로 보이게 (docker logs 즉시 확인용)
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# --- Node.js 20 LTS 설치 (노션 MCP의 npx용) ---
# curl로 NodeSource 설치 스크립트를 받아 apt 저장소를 추가한 뒤 nodejs 설치.
# ca-certificates는 https 다운로드에 필요. 설치 후 apt 캐시를 지워 이미지를 줄인다.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# --- Python 의존성 ---
# requirements.txt만 먼저 복사해 설치하면, 코드만 바뀔 때 이 레이어가
# 캐시돼 재빌드가 빨라진다 (Docker 레이어 캐시 활용).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- 노션 MCP 서버 미리 설치 ---
# tools.py는 `npx -y @notionhq/notion-mcp-server`로 호출하는데, 미리 심어두지
# 않으면 컨테이너 안 첫 노션 호출 때 npm 레지스트리에서 다운로드한다(지연·실패
# 위험). 전역 설치해두면 npx가 그 설치본을 바로 찾아 쓴다. (코드 변경 불필요)
RUN npm install -g @notionhq/notion-mcp-server

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
