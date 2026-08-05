"""디스코드 슬래시 커맨드 — 등록·설정·삭제.

왜 웹 폼이 아니라 디스코드 모달인가:
    webserver.py는 인증도 TLS도 없는 평문 HTTP다. 거기로 비밀번호를 받는 건
    이 프로젝트의 다른 어떤 문제보다 나쁘다. 디스코드는 TLS가 공짜이고,
    ephemeral 응답이면 채널에도 기록에도 안 남는다.
    ⚠️ DM 텍스트로 받는 것도 안 된다 — 사용자 대화기록에 영원히 남는다.

왜 등록과 설정을 나눴나:
    디스코드 모달은 입력칸이 **최대 5개**다. 자격증명만으로 이미 꽉 찬다.
    그래서 비밀은 /register(모달), 비밀이 아닌 취향은 /setup(슬래시 인자)로 나눈다.
"""

from __future__ import annotations

import re

import discord
from discord import app_commands

from secretary import onboarding, users
from secretary.config import OWNER_ID

# 블레이버스는 OAuth가 없어서 재사용 가능한 비밀번호를 봇이 들고 있어야 한다.
# 이 문장을 등록 화면에 반드시 띄운다 — 이게 Phase 3의 진짜 산출물이다.
WARNING = (
    "⚠️ 블레이버스는 공식 로그인 연동이 없어서 **비밀번호를 저장**해야 자동화가 돼요. "
    "암호화해서 넣지만, 이 서버가 털리면 복구될 수 있어요. "
    "다른 곳과 같은 비밀번호면 넣지 마세요. `/forget`으로 언제든 지웁니다."
)

PROVIDERS = [
    app_commands.Choice(name="Anthropic (Claude)", value="anthropic"),
    app_commands.Choice(name="OpenAI", value="openai"),
    app_commands.Choice(name="직접 띄운 vLLM 서버", value="vllm"),
]

# 기본 모델은 onboarding이 단일 출처다 (검증할 때와 저장할 때가 달라지면 안 된다).
_DEFAULT_MODEL = onboarding.DEFAULT_MODEL


class RegisterModal(discord.ui.Modal):
    """자격증명 5칸. 디스코드 모달의 상한이 정확히 5개라 더는 못 넣는다."""

    def __init__(self, provider: str):
        super().__init__(title=f"공주비서 등록 ({provider})")
        self.provider = provider

        # vLLM은 주소를 비워도 된다 — 비우면 봇이 쓰는 기본 서버(.env)를 따라간다.
        # 주소를 사람마다 저장해두면 서버를 옮길 때 등록자 전원의 행을 고쳐야 한다.
        local = provider == "vllm"
        self.llm = discord.ui.TextInput(
            label="vLLM 서버 주소 (비우면 기본 서버)" if local else "LLM API 키",
            placeholder=(
                "비워두셔도 돼요. 직접 띄운 서버가 있으면 http://..."
                if local
                else "sk-... (본인 키)"
            ),
            required=not local,
        )
        self.notion_token = discord.ui.TextInput(
            label="노션 통합 토큰",
            placeholder="ntn_... (노션 설정 → 연결 → 통합 만들기)",
            required=True,
        )
        self.notion_page = discord.ui.TextInput(
            label="노션 페이지 링크 (루틴·과제 DB가 있는)",
            placeholder="https://www.notion.so/...",
            required=True,
        )
        self.blaybus_id = discord.ui.TextInput(
            label="블레이버스 아이디 (안 쓰면 비우세요)", required=False
        )
        self.blaybus_pw = discord.ui.TextInput(
            label="블레이버스 비밀번호 (재사용 비번이면 넣지 마세요)",
            placeholder="암호화해 저장돼요. /forget으로 삭제 가능",
            required=False,
        )
        for item in (
            self.llm,
            self.notion_token,
            self.notion_page,
            self.blaybus_id,
            self.blaybus_pw,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        # 검증에 노션·블레이버스를 두드리므로 3초를 넘길 수 있다. 먼저 응답을 미룬다.
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = str(interaction.user.id)
        report: list[str] = []

        # 뇌 — 여기서 실제로 한 번 불러본다. 안 하면 'hello' 같은 값도 그대로 저장되고,
        # 대화할 때가 되어서야 AuthenticationError가 뜬다.
        secret = self.llm.value.strip()
        llm_error = await onboarding.verify_llm(self.provider, secret)
        fields: dict[str, str | None] = {}
        if llm_error:
            report.append(f"❌ 뇌({self.provider}): {llm_error}")
        else:
            # ⚠️ provider를 바꿔 다시 등록하면 **반대편 칸을 반드시 비워야** 한다.
            #    예전엔 vllm 분기가 vllm_base_url만 넣어서, openai로 등록했던 사람의
            #    옛 키와 'gpt-4o-mini'가 그대로 남았다. agent가 `llm_key or VLLM_API_KEY`로
            #    읽으니 **OpenAI 키가 로컬 서버로 나가 401** — /forget 말고는 길이 없었다
            #    (2026-08-05). '준 것만 갱신'이라 None을 넣어 지워야 한다.
            fields["llm_provider"] = self.provider
            if self.provider == "vllm":
                # 빈 주소는 저장하지 않는다 → agent가 .env의 VLLM_BASE_URL을 쓴다
                fields["vllm_base_url"] = secret or None
                fields["llm_key"] = None
                fields["llm_model"] = None  # .env의 VLLM_MODEL을 따른다
            else:
                fields["llm_key"] = secret
                fields["llm_model"] = _DEFAULT_MODEL[self.provider]
                fields["vllm_base_url"] = None
            report.append(f"✅ 뇌: {self.provider}")

        # 노션 — 페이지 하나에서 DB들을 찾아낸다
        token = self.notion_token.value.strip()
        found, err = await onboarding.discover_databases(token, self.notion_page.value)
        if err:
            report.append(f"❌ 노션: {err}")
        else:
            routine, homework = onboarding.pick_databases(found)
            fields.update(
                notion_token=token, routine_ds_id=routine, homework_ds_id=homework
            )
            names = ", ".join(d["title"] for d in found)
            report.append(f"✅ 노션: DB {len(found)}개 찾음 — {names}")

        # 블레이버스 — 비워두면 그 기능만 빠진다
        bid = self.blaybus_id.value.strip()
        bpw = self.blaybus_pw.value.strip()
        if bid and bpw:
            projects, berr = await onboarding.verify_blaybus(bid, bpw)
            if berr:
                report.append(f"❌ 블레이버스: {berr}")
            else:
                fields.update(
                    blaybus_id=bid, blaybus_pw=bpw, blaybus_pid=str(projects[0]["id"])
                )
                report.append(f"✅ 블레이버스: {projects[0]['title']}")
                if len(projects) > 1:
                    others = ", ".join(str(p["id"]) for p in projects[1:])
                    report.append(f"   (프로젝트가 여럿이에요. 다른 것: {others})")
        else:
            report.append("➖ 블레이버스: 건너뜀 (나중에 /register로 다시 하면 돼요)")

        try:
            users.save(uid, **fields)
        except users.CredentialsDisabled as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return

        report.append("\n다음은 `/setup`으로 알림 시각을 정해 주세요.")
        # 공개 채널에서 여러 명이 쓰면 답변이 서로 보인다. 코드로 막지 않고 안내한다
        # (일반 메시지 답장은 ephemeral이 안 되고, 막으면 1인 사용이 불편해진다).
        report.append(
            "💬 대화는 저에게 **DM**으로 거시는 걸 권해요. "
            "채널에서 하면 제 답변이 다른 분께도 보여요."
        )
        await interaction.followup.send("\n".join(report), ephemeral=True)


# 요일은 드롭다운으로만 받는다. 손으로 치게 하면 'wen'처럼 틀리는데,
# _parse_weekly가 모르는 요일을 **조용히 월요일로** 떨어뜨려서 아무도 못 알아챈다
# (2026-08-05: 수요일로 설정했다고 믿은 채 월요일 알림을 기다림).
WEEKDAYS = [
    app_commands.Choice(name="월요일", value="MON"),
    app_commands.Choice(name="화요일", value="TUE"),
    app_commands.Choice(name="수요일", value="WED"),
    app_commands.Choice(name="목요일", value="THU"),
    app_commands.Choice(name="금요일", value="FRI"),
    app_commands.Choice(name="토요일", value="SAT"),
    app_commands.Choice(name="일요일", value="SUN"),
]

# 24시간 HH:MM. 앞자리 0은 없어도 받고 저장할 때 채운다.
_CLOCK = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_clock(value: str) -> str | None:
    """'8:40' → '08:40'. 시각이 아니면 None.

    ⚠️ 저장 전에 걸러야 한다. 예전엔 '8시40분'도 그대로 저장됐고, 한참 뒤
       alarms._parse_time()이 ValueError로 터졌다. 넣은 사람은 성공한 줄 알고,
       사고는 알림 루프에서 났다.
    """
    found = _CLOCK.match((value or "").strip())
    return f"{int(found.group(1)):02d}:{found.group(2)}" if found else None


def build_weekly(day: str | None, clock: str | None, current: str | None) -> str | None:
    """(요일, 시각) 중 준 것만 바꿔 'WED 07:40' 꼴로. 둘 다 없으면 None(=안 바꿈).

    저장 형식을 'MON 08:00'으로 유지하는 이유: alarms._parse_weekly가 그걸 읽는다.
    """
    if not day and not clock:
        return None
    parts = (current or "").split()
    now_day = parts[0] if parts else "MON"
    now_clock = parts[1] if len(parts) > 1 else "08:30"
    return f"{day or now_day} {clock or now_clock}"


# /setup의 target 인자를 해석할 낱말. 채널 ID를 외우고 있는 사람은 없다.
_HERE = {"여기", "이채널", "here", "this"}
_OFF = {"끄기", "꺼", "안받기", "없음", "off", "none"}


def parse_alarm_target(
    value: str | None, channel_id: int | str
) -> tuple[str, str | None]:
    """('keep'|'off'|'set', 설정할 값).

    ⚠️ 값을 안 주면 **아무것도 안 바꾼다**('keep'). 예전엔 현재 채널로 조용히
       채워서, 시간만 고치려고 /setup을 부른 사람의 채널이 전부 알림 대상이 됐다
       (2026-08-04: 등록 6명에 알림 스케줄 4개). 같은 함수의 다른 칸들은
       처음부터 '준 것만 갱신'이었는데 이 칸만 예외였다.
    ⚠️ '끄기'는 받을 곳을 **지우지 않는다**('off'). 지웠더니 다시 켤 때 채널 ID를
       또 찾아야 했고, 시간만 다시 넣은 사람은 알림이 안 와 영문을 몰랐다
       (2026-08-05).
    """
    text = (value or "").strip()
    if not text:  # 공백만 준 것도 '안 준 것'으로 본다
        return "keep", None
    low = text.lower()
    if low in _OFF:
        return "off", None
    if low in _HERE:
        return "set", str(channel_id)
    return "set", text


def _mask(value: str | None) -> str:
    """비밀은 앞 4자만 보여준다. 화면 공유·캡처로 새는 걸 막는다."""
    if not value:
        return "없음"
    return f"{value[:4]}…({len(value)}자)"


def setup_commands(tree: app_commands.CommandTree) -> None:
    """슬래시 커맨드를 트리에 붙인다. bot.py가 한 번 부른다."""

    @tree.command(name="register", description="공주비서에 내 노션·블레이버스를 연결해요")
    @app_commands.describe(provider="어떤 모델을 쓸지 (본인 API 키가 필요해요)")
    @app_commands.choices(provider=PROVIDERS)
    async def register(
        interaction: discord.Interaction, provider: app_commands.Choice[str]
    ):
        if not users.enabled():
            await interaction.response.send_message(
                "❌ 서버에 CRED_KEY가 없어서 등록을 받을 수 없어요. "
                "(비밀번호를 평문으로 저장하지 않기 위해서예요)",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(RegisterModal(provider.value))

    @tree.command(name="setup", description="알림 시각과 쓸 모델을 정해요")
    @app_commands.describe(
        model="쓸 모델 이름 (예: gpt-4o, claude-sonnet-4-5). 제공자는 /register에서 정해요",
        target="알림 받을 곳: '여기'(이 채널) / '끄기' / 채널·사람 ID. 비우면 그대로 둬요",
        work_start="업무 시작 알림 시각 (예: 09:00)",
        work_end="업무 종료 알림 시각 (예: 18:00)",
        weekday="주간 브리핑 요일 (목록에서 고르세요)",
        weekly_time="주간 브리핑 시각 (예: 08:30)",
        mention="채널로 받을 때 멘션할 사람 ID. 비우면 나",
    )
    @app_commands.choices(weekday=WEEKDAYS)
    async def setup(
        interaction: discord.Interaction,
        model: str | None = None,
        target: str | None = None,
        work_start: str | None = None,
        work_end: str | None = None,
        weekday: app_commands.Choice[str] | None = None,
        weekly_time: str | None = None,
        mention: str | None = None,
    ):
        uid = str(interaction.user.id)
        me = users.get(uid)
        if me is None:
            await interaction.response.send_message(
                "먼저 `/register`로 연결해 주세요.", ephemeral=True
            )
            return

        # 모델을 바꾸면 실제로 한 번 불러본다(3초를 넘길 수 있어 응답을 미룬다).
        # ⚠️ 확인 안 하면 오타를 냈을 때 대화 도중에야 NotFoundError가 뜬다.
        await interaction.response.defer(ephemeral=True, thinking=True)
        if model:
            secret = me.vllm_base_url if me.llm_provider == "vllm" else me.llm_key
            error = await onboarding.verify_llm(me.llm_provider, secret, model.strip())
            if error:
                await interaction.followup.send(f"❌ {error}", ephemeral=True)
                return

        fields: dict[str, str | None] = {}
        action, new_target = parse_alarm_target(target, interaction.channel_id)
        if action == "off":
            # 받을 곳·멘션·시간은 그대로 두고 발송만 멈춘다. 다시 켤 때
            # '/setup target:여기'만 치면 예전 설정이 그대로 살아난다.
            fields["alarm_off"] = "1"
        elif action == "set":
            fields["alarm_target"] = new_target
            fields["alarm_mention"] = mention or uid
            fields["alarm_off"] = None  # 받을 곳을 정하면 자동으로 켜진다
        elif mention:
            fields["alarm_mention"] = mention.strip()

        # 시각은 저장 전에 형식을 본다. 통과 못 하면 아무것도 저장하지 않는다 —
        # 반만 저장되면 아가씨는 성공한 줄 알고 알림은 엉뚱하게 돈다.
        clocks: dict[str, str] = {}
        for label, key, raw in (
            ("업무 시작", "work_start", work_start),
            ("업무 종료", "work_end", work_end),
            ("주간 브리핑", "weekly_time", weekly_time),
        ):
            if not raw:
                continue
            clock = parse_clock(raw)
            if clock is None:
                await interaction.followup.send(
                    f"❌ {label} 시각 '{raw}'를 못 읽었어요. `08:40`처럼 넣어 주세요.",
                    ephemeral=True,
                )
                return
            clocks[key] = clock

        weekly_at = build_weekly(
            weekday.value if weekday else None, clocks.get("weekly_time"), me.weekly_at
        )
        for key, value in (
            ("llm_model", model.strip() if model else None),
            ("work_start", clocks.get("work_start")),
            ("work_end", clocks.get("work_end")),
            ("weekly_at", weekly_at),
        ):
            if value:
                fields[key] = value

        users.save(uid, **fields)
        u = users.get(uid)
        await interaction.followup.send(
            "✅ 설정을 저장했어요.\n"
            f"  뇌: {u.llm_provider} / {u.llm_model or '(기본값)'}\n"
            f"  알림 받을 곳: {u.alarm_target or '(아직 없음)'}"
            f"{' — 지금은 꺼둠' if u.alarm_off else ''}\n"
            f"  업무: {u.work_start or '(안 함)'} ~ {u.work_end or '(안 함)'}\n"
            f"  주간: {u.weekly_at or '(안 함)'}",
            ephemeral=True,
        )

    @tree.command(name="status", description="내 연결 상태를 확인해요")
    async def status(interaction: discord.Interaction):
        u = users.get(str(interaction.user.id))
        if u is None:
            await interaction.response.send_message(
                "아직 등록 안 하셨어요. `/register`로 시작하세요.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "\n".join(
                [
                    f"뇌: {u.llm_provider or '없음'} / 키 {_mask(u.llm_key)}",
                    f"노션 토큰: {_mask(u.notion_token)}",
                    f"  루틴 DB: {u.routine_ds_id or '없음'}",
                    f"  과제 DB: {u.homework_ds_id or '없음'}",
                    f"블레이버스: {u.blaybus_id or '없음'}"
                    f" / 비번 {_mask(u.blaybus_pw)} / 프로젝트 {u.blaybus_pid or '-'}",
                    f"알림: {u.alarm_target or '아직 없음'}"
                    f"{' (꺼둠)' if u.alarm_off else ''}"
                    f" ({u.work_start or '-'}~{u.work_end or '-'}, 주간 {u.weekly_at or '-'})"
                    # 예전 /setup이 채널을 조용히 등록해서, 받는 줄 모르는 사람이 있다
                    + (
                        " — 켜려면 `/setup target:여기`"
                        if u.alarm_off
                        else " — 끄려면 `/setup target:끄기`"
                        if u.alarm_target
                        else ""
                    ),
                    f"오늘 쓴 메시지: {u.daily_count if u.count_date else 0}",
                ]
            ),
            ephemeral=True,
        )

    @tree.command(name="users", description="(주인 전용) 누가 등록했고 연결은 됐는지")
    # 목록에서 가리는 용도. 서버 관리자에게는 보이지만 **실행은 아래 OWNER_ID 검사가
    # 막는다** — 남의 서버 관리자가 눌러도 거부된다.
    @app_commands.default_permissions(administrator=True)
    async def user_list(interaction: discord.Interaction):
        """운영용. 누가 막혀 있는지 알아야 도와줄 수 있다.

        ⚠️ 값은 절대 안 보여준다 — 연결 여부(✅/➖)만. 남의 노션 토큰이나
           블레이버스 비밀번호는 주인도 볼 이유가 없다.
        """
        if not OWNER_ID or str(interaction.user.id) != OWNER_ID:
            await interaction.response.send_message(
                "이건 주인만 쓸 수 있어요, 아가씨.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = users.all_users()
        if not rows:
            await interaction.followup.send("아직 등록한 사람이 없어요.", ephemeral=True)
            return

        lines = [f"등록 {len(rows)}명"]
        for u in rows[:20]:  # 디스코드 2000자 한도. 넘으면 잘라 보여준다
            try:
                who = str(await interaction.client.fetch_user(int(u.discord_id)))
            except Exception:  # noqa: BLE001 - 탈퇴했거나 못 찾는 경우
                who = f"(ID {u.discord_id})"
            brain = u.llm_provider or "없음"
            if not u.registered:
                brain += " ⚠️미완"
            lines.append(
                f"• {who}\n"
                f"    뇌 {brain} / 노션 {'✅' if u.notion_token else '➖'}"
                f"(루틴 {'✅' if u.routine_ds_id else '➖'}"
                f" 과제 {'✅' if u.homework_ds_id else '➖'})"
                f" / 블레이버스 {'✅' if u.blaybus_id else '➖'}"
                f" / 알림 {'✅' if u.alarm_target else '➖'}"
                f" / 오늘 {u.daily_count if u.count_date else 0}건"
            )
        if len(rows) > 20:
            lines.append(f"… 외 {len(rows) - 20}명")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @tree.command(name="forget", description="저장된 내 정보를 전부 지워요")
    async def forget(interaction: discord.Interaction):
        gone = users.forget(str(interaction.user.id))
        await interaction.response.send_message(
            "🗑️ 전부 지웠어요. 비밀번호도 함께 사라졌어요."
            if gone
            else "지울 게 없어요 (등록된 적 없음).",
            ephemeral=True,
        )


def _selftest() -> None:
    assert _mask(None) == "없음" and _mask("") == "없음"
    masked = _mask("sk-abcdefghijklmnop")
    # 앞 4자만 남고 나머지는 안 보여야 한다
    assert masked.startswith("sk-a") and "efghij" not in masked
    assert isinstance(masked, str)
    # 고를 수 있는 provider마다 기본 모델이 있어야 한다 (없으면 검증이 'x'로 떨어진다)
    assert {c.value for c in PROVIDERS} == set(_DEFAULT_MODEL), _DEFAULT_MODEL
    assert len(PROVIDERS) == 3
    # 고지 문장이 비어 있으면 안 된다 — 이게 이 단계의 산출물이다
    assert "비밀번호" in WARNING and "/forget" in WARNING

    # /setup의 target 해석. 여기가 알림 대상이 멋대로 늘어나던 자리다.
    # 값을 안 주면 건드리지 않는다 (예전엔 현재 채널로 조용히 채웠다)
    assert parse_alarm_target(None, 111) == ("keep", None)
    assert parse_alarm_target("", 111) == ("keep", None)
    assert parse_alarm_target("   ", 111) == ("keep", None)  # 공백만도 '안 준 것'
    # 끄기 — 받을 곳을 지우는 게 아니라 '멈춤'이다 (지우면 다시 켤 때 ID를 또 찾아야 한다)
    for word in ("끄기", "off", "OFF", "없음", " 안받기 "):
        assert parse_alarm_target(word, 111) == ("off", None), word
    # 이 채널
    for word in ("여기", "here", "HERE", " 이채널 "):
        assert parse_alarm_target(word, 111) == ("set", "111"), word
    # 그 밖엔 준 값 그대로 (채널이든 사람이든 ID)
    assert parse_alarm_target(" 1533725805023203369 ", 111) == ("set", "1533725805023203369")
    assert isinstance(parse_alarm_target("여기", 111), tuple)

    # 시각은 저장 전에 걸러야 한다 — 예전엔 '8시40분'이 저장되고 알림 루프에서 터졌다
    assert parse_clock("08:40") == "08:40"
    assert parse_clock("8:40") == "08:40"  # 앞자리 0은 채워준다
    assert parse_clock(" 23:59 ") == "23:59"
    assert parse_clock("00:00") == "00:00"
    for bad in ("8시40분", "8-40", "24:00", "12:60", "0840", "", None, "WED"):
        assert parse_clock(bad) is None, bad

    # 요일·시각 중 준 것만 바꾼다. 저장 형식은 'WED 07:40' (alarms._parse_weekly가 읽는다)
    assert build_weekly("WED", "07:40", None) == "WED 07:40"
    assert build_weekly("WED", None, "MON 08:30") == "WED 08:30"  # 시각 유지
    assert build_weekly(None, "07:40", "MON 08:30") == "MON 07:40"  # 요일 유지
    assert build_weekly(None, None, "MON 08:30") is None  # 둘 다 없으면 안 바꾼다
    assert build_weekly("WED", None, None) == "WED 08:30"  # 기존이 없으면 기본 시각
    assert isinstance(build_weekly("WED", "07:40", None), str)

    # 요일은 드롭다운으로만 받는다 (손으로 치면 'wen'이 된다)
    assert [c.value for c in WEEKDAYS] == ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    assert [c.name for c in WEEKDAYS][2] == "수요일"
    print("selftest OK")


if __name__ == "__main__":
    _selftest()
