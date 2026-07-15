"""MCP 서버(노션 등)를 LangGraph 도구로 변환하는 도구벨트 모듈.

핵심 아이디어:
    노션은 이미 '@notionhq/notion-mcp-server'라는 MCP 서버가 있고,
    그 안에 페이지 검색/생성, 블록 수정, DB 조회 같은 도구 ~15개가 들어있다.
    langchain-mcp-adapters가 그 서버를 통째로 불러다 LangGraph 도구로 자동 변환해준다.
    → 우리가 노션 API 함수를 손으로 짤 필요가 없다.

나중에 깃허브·피그마 등을 붙일 때는 아래 MCP_SERVERS 딕셔너리에
서버 한 덩어리만 추가하면 그 서비스 도구가 전부 자동으로 봇에 꽂힌다.
"""

from __future__ import annotations

from langchain_mcp_adapters.client import MultiServerMCPClient

from secretary.config import NOTION_TOKEN

# 봇이 연결할 MCP 서버 목록.
# 지금은 노션 하나. 앞으로 "github": {...} 처럼 줄만 추가하면 확장된다.
MCP_SERVERS: dict = {
    "notion": {
        # 로컬에 설치된 npx로 노션 MCP 서버를 하위 프로세스로 띄운다.
        "command": "npx",
        "args": ["-y", "@notionhq/notion-mcp-server"],
        # 그 서버가 쓸 노션 인테그레이션 토큰을 환경변수로 넘긴다.
        "env": {"NOTION_TOKEN": NOTION_TOKEN},
        # stdio: 표준입출력으로 MCP 프로토콜 통신 (가장 기본적인 로컬 연결 방식)
        "transport": "stdio",
    },
}


async def load_tools() -> list:
    """MCP 서버들에서 도구 목록을 받아 LangGraph 도구 리스트로 돌려준다.

    봇 시작 시 딱 한 번 호출된다. 반환된 도구들은 agent.py에서
    create_react_agent(model, tools=...)로 에이전트에 장착된다.
    """
    client = MultiServerMCPClient(MCP_SERVERS)
    tools = await client.get_tools()
    return tools
