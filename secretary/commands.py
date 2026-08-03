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

        secret_label = "vLLM 서버 주소" if provider == "vllm" else "LLM API 키"
        self.llm = discord.ui.TextInput(
            label=secret_label,
            placeholder=(
                "http://... (내 vLLM)" if provider == "vllm" else "sk-... (본인 키)"
            ),
            required=True,
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
            fields["llm_provider"] = self.provider
            if self.provider == "vllm":
                fields["vllm_base_url"] = secret
            else:
                fields["llm_key"] = secret
                fields["llm_model"] = _DEFAULT_MODEL[self.provider]
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
        target="알림 받을 곳의 ID (채널이든 나든). 비우면 이 채널",
        work_start="업무 시작 알림 시각 (예: 09:00)",
        work_end="업무 종료 알림 시각 (예: 18:00)",
        weekly="주간 브리핑 (예: MON 08:00)",
        mention="채널로 받을 때 멘션할 사람 ID. 비우면 나",
    )
    async def setup(
        interaction: discord.Interaction,
        model: str | None = None,
        target: str | None = None,
        work_start: str | None = None,
        work_end: str | None = None,
        weekly: str | None = None,
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

        fields = {
            "alarm_target": target or str(interaction.channel_id),
            "alarm_mention": mention or uid,
        }
        for key, value in (
            ("llm_model", model),
            ("work_start", work_start),
            ("work_end", work_end),
            ("weekly_at", weekly),
        ):
            if value:
                fields[key] = value.strip()

        users.save(uid, **fields)
        u = users.get(uid)
        await interaction.followup.send(
            "✅ 설정을 저장했어요.\n"
            f"  뇌: {u.llm_provider} / {u.llm_model or '(기본값)'}\n"
            f"  받을 곳: {u.alarm_target}\n"
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
                    f"알림: {u.alarm_target or '없음'}"
                    f" ({u.work_start or '-'}~{u.work_end or '-'}, 주간 {u.weekly_at or '-'})",
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
    assert set(_DEFAULT_MODEL) == {"anthropic", "openai"}  # vllm은 모델을 서버가 정한다
    assert len(PROVIDERS) == 3
    # 고지 문장이 비어 있으면 안 된다 — 이게 이 단계의 산출물이다
    assert "비밀번호" in WARNING and "/forget" in WARNING
    print("selftest OK")


if __name__ == "__main__":
    _selftest()
