"""NFL mock draft Discord bot: slash-command picks and trades, written straight
to a Google Sheet, with a ping for whoever is on the clock."""
import asyncio
import os
import re

import discord
from discord import app_commands

from sheets import DraftSheet, Pick, open_spreadsheet

ADMIN_ROLE = os.environ.get("ADMIN_ROLE", "Commish")
GUILD_ID = os.environ.get("GUILD_ID")  # optional: makes slash commands appear instantly

client = discord.Client(intents=discord.Intents.default(),
                        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
tree = app_commands.CommandTree(client)
lock = asyncio.Lock()          # one sheet-changing action at a time
sheet: DraftSheet | None = None


async def run(fn, *args, **kwargs):
    """gspread is blocking; keep it off the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def is_admin(member) -> bool:
    return bool(getattr(member, "guild_permissions", None) and member.guild_permissions.manage_guild) \
        or any(r.name == ADMIN_ROLE for r in getattr(member, "roles", []))


def current_pick(picks: list[Pick]) -> Pick | None:
    return next((p for p in picks if not p.made), None)


def clock_line(picks: list[Pick], gms: dict[str, int]) -> str:
    p = current_pick(picks)
    if p is None:
        return "🏁 **The draft is complete!**"
    uid = sheet.gm_of(p.team, gms)
    who = f"<@{uid}>" if uid else "*(no GM assigned — use `/assign`)*"
    line = f"⏰ **On the clock:** Pick {p.number} — **{p.team}** {who}"
    nxt = next((q for q in picks if not q.made and q.row > p.row), None)
    if nxt:
        line += f"\nOn deck: Pick {nxt.number} — {nxt.team}"
    return line


def parse_pick_numbers(text: str | None) -> list[int]:
    return [int(n) for n in re.findall(r"\d+", text or "")]


async def team_autocomplete(interaction: discord.Interaction, current: str):
    try:
        teams = await run(sheet.teams)
    except Exception:
        return []
    return [app_commands.Choice(name=t, value=t) for t in teams if current.lower() in t.lower()][:25]


def match_team(name: str, picks: list[Pick], gms: dict[str, int]) -> str | None:
    for t in [p.team for p in picks] + list(gms):
        if t.lower() == name.strip().lower():
            return t
    return None


# ------------------------------------------------------------------ picks
@tree.command(description="Make your pick when you're on the clock")
@app_commands.describe(player="Player name", position="e.g. QB, EDGE, CB", school="College")
async def pick(interaction: discord.Interaction, player: str, position: str, school: str = ""):
    await interaction.response.defer(ephemeral=True)
    async with lock:
        picks, gms = await run(sheet.picks), await run(sheet.gms)
        cur = current_pick(picks)
        if cur is None:
            return await interaction.followup.send("The draft is already complete.")
        gm = sheet.gm_of(cur.team, gms)
        if interaction.user.id != gm and not is_admin(interaction.user):
            return await interaction.followup.send(
                f"It's not your pick — {cur.team} is on the clock at pick {cur.number}.")
        dupe = next((p for p in picks if p.made and p.player.lower() == player.strip().lower()), None)
        if dupe:
            return await interaction.followup.send(
                f"**{dupe.player}** already went at pick {dupe.number} to {dupe.team}.")
        await run(sheet.record_pick, cur, player.strip(), position.strip().upper(), school.strip())
        picks = await run(sheet.picks)
    detail = ", ".join(x for x in (position.strip().upper(), school.strip()) if x)
    by = "" if interaction.user.id == gm else f" *(entered by {interaction.user.display_name})*"
    await interaction.channel.send(
        f"✅ **Pick {cur.number}** — **{cur.team}** select **{player.strip()}** ({detail}){by}\n\n"
        + clock_line(picks, gms))
    await interaction.followup.send("Pick recorded.")


@tree.command(description="[Commish] Undo the most recent pick")
async def undo(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Only the commish can undo picks.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    async with lock:
        picks, gms = await run(sheet.picks), await run(sheet.gms)
        made = [p for p in picks if p.made]
        if not made:
            return await interaction.followup.send("No picks have been made yet.")
        last = made[-1]
        await run(sheet.clear_pick, last)
        picks = await run(sheet.picks)
    await interaction.channel.send(
        f"↩️ Pick {last.number} ({last.team}: {last.player}) was undone.\n\n" + clock_line(picks, gms))
    await interaction.followup.send("Done.")


@tree.command(description="Show who's on the clock (and ping them)")
async def onclock(interaction: discord.Interaction):
    await interaction.response.defer()
    picks, gms = await run(sheet.picks), await run(sheet.gms)
    await interaction.followup.send(clock_line(picks, gms))


@tree.command(description="Show the next 10 picks")
async def upcoming(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    left = [p for p in await run(sheet.picks) if not p.made][:10]
    await interaction.followup.send(
        "\n".join(f"`{p.number:>3}` {p.team} {p.notes}".rstrip() for p in left) or "The draft is complete.")


@tree.command(description="Show every pick a team holds or has made")
@app_commands.autocomplete(team=team_autocomplete)
async def teampicks(interaction: discord.Interaction, team: str):
    await interaction.response.defer(ephemeral=True)
    mine = [p for p in await run(sheet.picks) if p.team.lower() == team.strip().lower()]
    lines = [f"`{p.number:>3}` " + (f"{p.player} ({p.position})" if p.made else "—") + f" {p.notes}".rstrip()
             for p in mine]
    await interaction.followup.send("\n".join(lines)[:1900] or f"No picks found for “{team}”.")


# ------------------------------------------------------------------ GMs
@tree.command(description="[Commish] Assign a Discord user as GM of a team")
@app_commands.autocomplete(team=team_autocomplete)
async def assign(interaction: discord.Interaction, team: str, user: discord.Member):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Only the commish can assign GMs.", ephemeral=True)
    await interaction.response.defer()
    async with lock:
        picks, gms = await run(sheet.picks), await run(sheet.gms)
        name = match_team(team, picks, gms)
        if not name:
            return await interaction.followup.send(f"No team called “{team}” in the draft sheet.")
        await run(sheet.set_gm, name, user.id, user.display_name)
    await interaction.followup.send(f"**{name}** → {user.mention}")


@tree.command(description="List teams and their GMs")
async def gms(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    assigned = await run(sheet.gms)
    lines = [f"{t}: " + (f"<@{sheet.gm_of(t, assigned)}>" if sheet.gm_of(t, assigned) else "—")
             for t in await run(sheet.teams)]
    await interaction.followup.send("\n".join(lines)[:1900] or "No teams found.")


# ------------------------------------------------------------------ trades
def validate_trade(picks, team_a, team_b, nums_a, nums_b):
    """Returns (picks_a, picks_b) or raises ValueError with a readable reason."""
    by_num = {p.number: p for p in picks}
    result = []
    for team, nums in ((team_a, nums_a), (team_b, nums_b)):
        chosen = []
        for n in nums:
            p = by_num.get(n)
            if p is None:
                raise ValueError(f"Pick {n} doesn't exist in the sheet.")
            if p.made:
                raise ValueError(f"Pick {n} has already been used.")
            if p.team.lower() != team.lower():
                raise ValueError(f"Pick {n} belongs to {p.team}, not {team}.")
            chosen.append(p)
        result.append(chosen)
    return result


def trade_summary(team_a, team_b, nums_a, nums_b, other):
    fmt = lambda nums: ", ".join(f"#{n}" for n in nums) or "—"
    text = f"**{team_a}** send: {fmt(nums_a)}\n**{team_b}** send: {fmt(nums_b)}"
    return text + (f"\nOther terms: {other}" if other else "")


class TradeView(discord.ui.View):
    def __init__(self, proposer_id, team_a, team_b, nums_a, nums_b, other):
        super().__init__(timeout=24 * 3600)
        self.proposer_id, self.team_a, self.team_b = proposer_id, team_a, team_b
        self.nums_a, self.nums_b, self.other = nums_a, nums_b, other

    async def _finish(self, interaction, text):
        self.stop()
        await interaction.edit_original_response(content=text, view=None)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        async with lock:
            picks, gms = await run(sheet.picks), await run(sheet.gms)
            if interaction.user.id != sheet.gm_of(self.team_b, gms) and not is_admin(interaction.user):
                return await interaction.followup.send(
                    f"Only the {self.team_b} GM or the commish can accept this.", ephemeral=True)
            try:
                pa, pb = validate_trade(picks, self.team_a, self.team_b, self.nums_a, self.nums_b)
            except ValueError as e:
                return await self._finish(interaction, f"❌ Trade void — {e}")
            before = current_pick(picks)
            await run(sheet.apply_trade, self.team_a, self.team_b, pa, pb, self.other)
            picks = await run(sheet.picks)
        text = "🔁 **TRADE**\n" + trade_summary(self.team_a, self.team_b, self.nums_a, self.nums_b, self.other)
        after = current_pick(picks)
        if before and after and before.team != after.team:   # the pick on the clock changed hands
            text += "\n\n" + clock_line(picks, gms)
        await self._finish(interaction, text)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        gms = await run(sheet.gms)
        allowed = {sheet.gm_of(self.team_b, gms), self.proposer_id}
        if interaction.user.id not in allowed and not is_admin(interaction.user):
            return await interaction.followup.send("That's not your trade to decline.", ephemeral=True)
        await self._finish(interaction, f"🚫 Trade between {self.team_a} and {self.team_b} was declined/cancelled.")


@tree.command(description="Propose a trade — the other GM must accept it")
@app_commands.describe(
    your_team="The team you're trading as", other_team="The team you're trading with",
    you_send="Overall pick numbers you give up, e.g. 12, 45", you_get="Overall pick numbers you receive",
    other="Anything else in the deal (players, future picks) — logged, not tracked")
@app_commands.autocomplete(your_team=team_autocomplete, other_team=team_autocomplete)
async def trade(interaction: discord.Interaction, your_team: str, other_team: str,
                you_send: str = "", you_get: str = "", other: str = ""):
    await interaction.response.defer()
    picks, gms = await run(sheet.picks), await run(sheet.gms)
    team_a, team_b = match_team(your_team, picks, gms), match_team(other_team, picks, gms)
    nums_a, nums_b = parse_pick_numbers(you_send), parse_pick_numbers(you_get)
    try:
        if not team_a or not team_b or team_a == team_b:
            raise ValueError("Pick two different teams from the list.")
        if interaction.user.id != sheet.gm_of(team_a, gms) and not is_admin(interaction.user):
            raise ValueError(f"You're not the GM of {team_a}.")
        if not (nums_a or nums_b or other.strip()):
            raise ValueError("Nothing is being traded.")
        validate_trade(picks, team_a, team_b, nums_a, nums_b)
    except ValueError as e:
        return await interaction.followup.send(f"❌ {e}")
    uid = sheet.gm_of(team_b, gms)
    ping = f"<@{uid}>" if uid else f"*(no {team_b} GM assigned — commish can accept)*"
    await interaction.followup.send(
        f"📨 **Trade proposal** — {ping}, accept?\n" + trade_summary(team_a, team_b, nums_a, nums_b, other.strip()),
        view=TradeView(interaction.user.id, team_a, team_b, nums_a, nums_b, other.strip()))


# ------------------------------------------------------------------ startup
@tree.error
async def on_error(interaction: discord.Interaction, error):
    msg = f"Something went wrong: `{getattr(error, 'original', error)}`"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass
    raise error


@client.event
async def on_ready():
    global sheet
    if sheet is None:
        sheet = await run(lambda: DraftSheet(open_spreadsheet()))
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
        else:
            await tree.sync()
    print(f"Logged in as {client.user}; draft tab '{sheet.l.draft_tab}' loaded.")


if __name__ == "__main__":
    client.run(os.environ["DISCORD_TOKEN"])
