"""NFL mock draft Discord bot: slash-command picks and trades, written straight
to a Google Sheet, with a ping for whoever is on the clock."""
import asyncio
import os
import re
import time

import discord
from discord import app_commands

from sheets import DraftSheet, Pick, open_spreadsheet

ADMIN_ROLE = os.environ.get("ADMIN_ROLE", "Commish")
GUILD_ID = os.environ.get("GUILD_ID")  # optional: makes slash commands appear instantly
PICKS_CHANNEL_ID = os.environ.get("PICKS_CHANNEL_ID", "").strip()  # optional: where picks are posted
CLOCK_CHANNEL_ID = os.environ.get("CLOCK_CHANNEL_ID", "").strip()  # optional: where "you're up" pings go

client = discord.Client(intents=discord.Intents.default(),
                        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
tree = app_commands.CommandTree(client)
lock = asyncio.Lock()          # one sheet-changing action at a time
sheet: DraftSheet | None = None


async def _channel(setting: str, fallback):
    if setting.isdigit():
        cid = int(setting)
        return client.get_channel(cid) or await client.fetch_channel(cid)
    return fallback


settings: dict[str, str] = {}   # from the sheet's Settings tab; set with /setchannels


async def picks_channel(fallback):
    """The picks channel from /setchannels (or PICKS_CHANNEL_ID), otherwise where the command was used."""
    return await _channel(settings.get("picks_channel") or PICKS_CHANNEL_ID, fallback)


async def clock_channel(fallback):
    """The ping channel if CLOCK_CHANNEL_ID is set, otherwise the picks channel."""
    return await _channel(settings.get("clock_channel") or CLOCK_CHANNEL_ID, await picks_channel(fallback))


def clock_ping(picks: list[Pick], gms: dict[str, int], guild=None) -> str | None:
    """e.g. '@Kebo — Pick 3: <emoji> Chicago Bears'. None when the draft is over."""
    p = current_pick(picks)
    if p is None:
        return None
    uid = sheet.gm_of(p.team, gms)
    who = f"<@{uid}>" if uid else "*(no GM assigned)*"
    emoji = team_emoji(guild, p.team)
    return f"{who} — Pick {p.number}: {emoji + ' ' if emoji else ''}{p.team}"


async def ping_next(fallback, picks, gms):
    text = clock_ping(picks, gms, getattr(fallback, "guild", None))
    if text:
        channel = await clock_channel(fallback)
        await channel.send(text)
        return channel


async def announce(interaction: discord.Interaction, text: str):
    channel = await picks_channel(interaction.channel)
    await channel.send(text)
    return channel


_name_cache: dict[tuple[int, str], tuple[float, int | None]] = {}   # (guild, name) -> (time, user id)


def _best_match(members, name: str) -> int | None:
    """Exact username wins over an exact nickname/display name, so 'kebo' finds @kebo, not a 'Kebo' nickname."""
    name = name.lower()
    for fields in (lambda m: (m.name,), lambda m: (m.nick, m.global_name, m.display_name)):
        for m in members:
            if any(n and n.lower() == name for n in fields(m)):
                return m.id
    return None


async def resolve_user(guild, name: str) -> int | None:
    """Find a server member by exact username, display name or nickname (case-insensitive)."""
    if guild is None:
        return None
    key = (guild.id, name.lower())
    hit = _name_cache.get(key)
    if hit and (hit[1] is not None or time.time() - hit[0] < 120):   # retry misses after 2 min
        return hit[1]
    uid = _best_match(guild.members, name)
    if uid is None:
        try:
            found = await guild.query_members(query=name, limit=25, cache=True)
            uid = _best_match(found, name)
        except (asyncio.TimeoutError, discord.HTTPException):
            return None
    _name_cache[key] = (time.time(), uid)
    return uid


async def load_gms(guild, missing: list | None = None) -> dict[str, int]:
    """team -> Discord user id, from the GMs tab. Column B may be an ID or a username."""
    entries = await run(sheet.gm_entries)
    names = {t: v.lstrip("@").strip() for t, v in entries.items() if not v.isdigit()}
    ids = await asyncio.gather(*(resolve_user(guild, n) for n in names.values()))
    out = {t: int(v) for t, v in entries.items() if v.isdigit()}
    for (team, name), uid in zip(names.items(), ids):
        if uid:
            out[team] = uid
        elif missing is not None:
            missing.append((team, name))
    return out


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


NFL_ABBR = {
    "arizona cardinals": "ari", "atlanta falcons": "atl", "baltimore ravens": "bal", "buffalo bills": "buf",
    "carolina panthers": "car", "chicago bears": "chi", "cincinnati bengals": "cin", "cleveland browns": "cle",
    "dallas cowboys": "dal", "denver broncos": "den", "detroit lions": "det", "green bay packers": "gb",
    "houston texans": "hou", "indianapolis colts": "ind", "jacksonville jaguars": "jax",
    "kansas city chiefs": "kc", "las vegas raiders": "lv", "los angeles chargers": "lac",
    "los angeles rams": "lar", "miami dolphins": "mia", "minnesota vikings": "min",
    "new england patriots": "ne", "new orleans saints": "no", "new york giants": "nyg", "new york jets": "nyj",
    "philadelphia eagles": "phi", "pittsburgh steelers": "pit", "san francisco 49ers": "sf",
    "seattle seahawks": "sea", "tampa bay buccaneers": "tb", "tennessee titans": "ten",
    "washington commanders": "was",
}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def team_emoji(guild, team: str) -> str:
    """A server emoji named after the team: full name, nickname (e.g. 'vikings') or abbreviation ('min')."""
    if guild is None:
        return ""
    words = team.split()
    names = {_norm(team), _norm(words[-1]) if words else ""}
    abbr = NFL_ABBR.get(" ".join(words).lower())
    if abbr:
        names |= {abbr, "nfl" + abbr, abbr + "logo"}
    names.discard("")
    for e in guild.emojis:
        if _norm(e.name) in names:
            return str(e)
    return ""


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
        picks, gms = await run(sheet.picks), await load_gms(interaction.guild)
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
    parts = [player.strip(), position.strip().upper(), school.strip()]
    emoji = team_emoji(interaction.guild, cur.team)
    line = f"{cur.number}. {emoji + ' ' if emoji else ''}{cur.team} - " + " | ".join(x for x in parts if x)
    channel = await announce(interaction, line)
    if current_pick(picks) is None:
        await channel.send("🏁 **The draft is complete!**")
    else:
        await ping_next(interaction.channel, picks, gms)
    await interaction.followup.send(f"Pick recorded and posted in {channel.mention}.")


@tree.command(description="[Commish] Undo the most recent pick")
async def undo(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Only the commish can undo picks.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    async with lock:
        picks, gms = await run(sheet.picks), await load_gms(interaction.guild)
        made = [p for p in picks if p.made]
        if not made:
            return await interaction.followup.send("No picks have been made yet.")
        last = made[-1]
        await run(sheet.clear_pick, last)
        picks = await run(sheet.picks)
    await announce(
        interaction,
        f"↩️ Pick {last.number} ({last.team}: {last.player}) was undone.")
    await ping_next(interaction.channel, picks, gms)
    await interaction.followup.send("Done.")


@tree.command(description="Show who's on the clock (and ping them)")
async def onclock(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    picks, gms = await run(sheet.picks), await load_gms(interaction.guild)
    await interaction.followup.send(clock_line(picks, gms))   # full details, only visible to you
    channel = await ping_next(interaction.channel, picks, gms)
    if channel and channel.id != interaction.channel_id:
        await interaction.followup.send(f"Pinged in {channel.mention}.", ephemeral=True)


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


# ------------------------------------------------------------------ setup
@tree.command(description="[Commish] Choose where picks are posted and where the next GM is pinged")
@app_commands.describe(picks="Channel for pick announcements and trades",
                       pings="Channel for the @mention of whoever is on the clock")
async def setchannels(interaction: discord.Interaction,
                      picks: discord.TextChannel | None = None, pings: discord.TextChannel | None = None):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Only the commish can set channels.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    lines, warn = [], []
    for key, channel, label in (("picks_channel", picks, "Picks"), ("clock_channel", pings, "Pings")):
        if channel is None:
            continue
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages):
            warn.append(f"⚠️ I can't post in {channel.mention} yet. Give my role **View Channel** and "
                        f"**Send Messages** there.")
        await run(sheet.set_setting, key, str(channel.id))
        settings[key] = str(channel.id)
        lines.append(f"{label}: {channel.mention}")
    if not lines:
        current = [f"{label}: " + (f"<#{settings[k]}>" if settings.get(k) else "not set")
                   for k, label in (("picks_channel", "Picks"), ("clock_channel", "Pings"))]
        return await interaction.followup.send("\n".join(current))
    await interaction.followup.send("\n".join(lines + warn))


# ------------------------------------------------------------------ GMs
@tree.command(description="[Commish] Assign a Discord user as GM of a team")
@app_commands.autocomplete(team=team_autocomplete)
async def assign(interaction: discord.Interaction, team: str, user: discord.Member):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Only the commish can assign GMs.", ephemeral=True)
    await interaction.response.defer()
    async with lock:
        picks, gms = await run(sheet.picks), await load_gms(interaction.guild)
        name = match_team(team, picks, gms)
        if not name:
            return await interaction.followup.send(f"No team called “{team}” in the draft sheet.")
        await run(sheet.set_gm, name, user.id, user.display_name)
    await interaction.followup.send(f"**{name}** → {user.mention}")


@tree.command(description="List teams and their GMs")
async def gms(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    missing = []
    assigned = await load_gms(interaction.guild, missing)
    unknown = {t.lower(): n for t, n in missing}
    lines = []
    for t in await run(sheet.teams):
        uid = sheet.gm_of(t, assigned)
        if uid:
            lines.append(f"{t}: <@{uid}>")
        elif t.lower() in unknown:
            lines.append(f"{t}: ⚠️ can't find `{unknown[t.lower()]}` in this server")
        else:
            lines.append(f"{t}: —")
    await interaction.followup.send("\n".join(lines)[:1900] or "No teams found.",
                                    allowed_mentions=discord.AllowedMentions.none())


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


def _and(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _assets(nums: list[int], extra: str) -> str:
    """[18, 86] + 'OT Joe Alt, 2028 5th' -> 'picks 18 and 86, OT Joe Alt and 2028 5th'"""
    items = []
    if nums:
        items.append(("pick " if len(nums) == 1 else "picks ") + _and([str(n) for n in nums]))
    items += [x.strip() for x in re.split(r",|\band\b|&", extra or "") if x.strip()]
    return _and(items)


def team_label(guild, team: str) -> str:
    emoji = team_emoji(guild, team)
    return f"{emoji} {team}" if emoji else team


def trade_summary(team_a, team_b, nums_a, nums_b, extra_a="", extra_b=""):
    fmt = lambda nums, extra: _assets(nums, extra) or "nothing"
    return f"**{team_a}** send: {fmt(nums_a, extra_a)}\n**{team_b}** send: {fmt(nums_b, extra_b)}"


def trade_announcement(guild, team_a, team_b, nums_a, nums_b, extra_a="", extra_b=""):
    """e.g. 'The 🏈 Cleveland Browns send pick 18 and a 2027 4th to the 🏈 New York Giants for picks 22 and 86.'"""
    a, b = team_label(guild, team_a), team_label(guild, team_b)
    give, get = _assets(nums_a, extra_a), _assets(nums_b, extra_b)
    if give and get:
        return f"🔁 The {a} send {give} to the {b} for {get}."
    if give:
        return f"🔁 The {a} send {give} to the {b}."
    return f"🔁 The {a} acquire {get} from the {b}."


class TradeView(discord.ui.View):
    def __init__(self, proposer_id, team_a, team_b, nums_a, nums_b, extra_a, extra_b):
        super().__init__(timeout=24 * 3600)
        self.proposer_id, self.team_a, self.team_b = proposer_id, team_a, team_b
        self.nums_a, self.nums_b, self.extra_a, self.extra_b = nums_a, nums_b, extra_a, extra_b

    async def _finish(self, interaction, text):
        self.stop()
        await interaction.edit_original_response(content=text, view=None)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        async with lock:
            picks, gms = await run(sheet.picks), await load_gms(interaction.guild)
            if interaction.user.id != sheet.gm_of(self.team_b, gms) and not is_admin(interaction.user):
                return await interaction.followup.send(
                    f"Only the {self.team_b} GM or the commish can accept this.", ephemeral=True)
            try:
                pa, pb = validate_trade(picks, self.team_a, self.team_b, self.nums_a, self.nums_b)
            except ValueError as e:
                return await self._finish(interaction, f"❌ Trade void — {e}")
            before = current_pick(picks)
            other = "; ".join(x for x in (f"{self.team_a} send: {self.extra_a}" if self.extra_a else "",
                                         f"{self.team_b} send: {self.extra_b}" if self.extra_b else "") if x)
            await run(sheet.apply_trade, self.team_a, self.team_b, pa, pb, other)
            picks = await run(sheet.picks)
        text = trade_announcement(interaction.guild, self.team_a, self.team_b,
                                  self.nums_a, self.nums_b, self.extra_a, self.extra_b)
        after = current_pick(picks)
        picks_ch = await picks_channel(interaction.channel)
        if picks_ch.id != interaction.channel_id:
            await self._finish(interaction, "✅ Trade accepted — " + text.removeprefix("🔁 "))
            await picks_ch.send(text)
        else:
            await self._finish(interaction, text)
        if before and after and before.team != after.team:   # the pick on the clock changed hands
            await ping_next(interaction.channel, picks, gms)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        gms = await load_gms(interaction.guild)
        allowed = {sheet.gm_of(self.team_b, gms), self.proposer_id}
        if interaction.user.id not in allowed and not is_admin(interaction.user):
            return await interaction.followup.send("That's not your trade to decline.", ephemeral=True)
        await self._finish(interaction, f"🚫 Trade between {self.team_a} and {self.team_b} was declined/cancelled.")


@tree.command(description="Propose a trade — the other GM must accept it")
@app_commands.describe(
    your_team="The team you're trading as", other_team="The team you're trading with",
    you_send="Pick numbers you give up this draft, e.g. 18, 86",
    you_get="Pick numbers you receive this draft, e.g. 22",
    you_send_other="Players/future picks you give up, e.g. OT Joe Alt, 2028 5th",
    you_get_other="Players/future picks you receive, e.g. WR Jordyn Tyson")
@app_commands.autocomplete(your_team=team_autocomplete, other_team=team_autocomplete)
async def trade(interaction: discord.Interaction, your_team: str, other_team: str,
                you_send: str = "", you_get: str = "", you_send_other: str = "", you_get_other: str = ""):
    await interaction.response.defer()
    picks, gms = await run(sheet.picks), await load_gms(interaction.guild)
    team_a, team_b = match_team(your_team, picks, gms), match_team(other_team, picks, gms)
    nums_a, nums_b = parse_pick_numbers(you_send), parse_pick_numbers(you_get)
    extra_a, extra_b = you_send_other.strip(), you_get_other.strip()
    try:
        if not team_a or not team_b or team_a == team_b:
            raise ValueError("Pick two different teams from the list.")
        if interaction.user.id != sheet.gm_of(team_a, gms) and not is_admin(interaction.user):
            raise ValueError(f"You're not the GM of {team_a}.")
        if not (nums_a or nums_b or extra_a or extra_b):
            raise ValueError("Nothing is being traded.")
        validate_trade(picks, team_a, team_b, nums_a, nums_b)
    except ValueError as e:
        return await interaction.followup.send(f"❌ {e}")
    uid = sheet.gm_of(team_b, gms)
    ping = f"<@{uid}>" if uid else f"*(no {team_b} GM assigned — commish can accept)*"
    await interaction.followup.send(
        f"📨 **Trade proposal** — {ping}, accept?\n" + trade_summary(team_a, team_b, nums_a, nums_b, extra_a, extra_b),
        view=TradeView(interaction.user.id, team_a, team_b, nums_a, nums_b, extra_a, extra_b))


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
        settings.update(await run(sheet.settings))
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
        else:
            await tree.sync()
    print(f"Logged in as {client.user}; draft tab '{sheet.l.draft_tab}' loaded.")


if __name__ == "__main__":
    client.run(os.environ["DISCORD_TOKEN"])
