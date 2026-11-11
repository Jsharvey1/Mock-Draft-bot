# NFL Mock Draft Bot

Slash-command picks and trades in Discord, written straight to your Google Sheet, with a ping for whoever is up next.

The sheet is the source of truth: the pick "on the clock" is the first pick row with an empty player cell. The bot stores nothing itself, so restarts and manual sheet edits are safe.

## Commands

| Command | Who | What it does |
|---|---|---|
| `/pick player position [school]` | GM on the clock | Fills in the pick, announces it, pings the next GM |
| `/trade your_team other_team [you_send] [you_get] [other]` | Any GM | Posts a proposal with Accept/Decline buttons; on accept, the picks change teams in the sheet |
| `/onclock` | Anyone | Re-pings the GM on the clock |
| `/upcoming` | Anyone | Next 10 picks |
| `/teampicks team` | Anyone | All picks a team holds or has made |
| `/gms` | Anyone | Teams and their GMs |
| `/assign team user` | Commish | Makes a user the GM of a team (repeat for users with several teams) |
| `/undo` | Commish | Clears the most recent pick |

Set `PICKS_CHANNEL_ID` to post every pick, accepted trade and ping in one channel (right-click the channel → **Copy Channel ID**). GMs can then run commands anywhere; they get a private confirmation and the announcement goes to the draft channel. Leave it blank to announce wherever the command was used.

Each pick is posted as one line, `72. <emoji> New Orleans Saints - P.J. Williams | OT | SMU`, followed by a separate message pinging whoever is up next. The emoji is a server emoji named after the team: its nickname (`saints`), full name (`neworleanssaints`) or abbreviation (`no`). If the server has none, the line has no emoji.

"Commish" = anyone with Manage Server permission or the `Commish` role. They can also pick for an absent GM and accept trades for a team.

## Sheet layout

One tab (default name `Draft`) with one row per pick, in draft order:

| A: Pick # | B: Team | C: Player | D: Pos | E: School | F: Notes |
|---|---|---|---|---|---|
| 1 | Las Vegas Raiders | | | | |
| 2 | New York Jets | | | | [from CAR] |

- Pick numbers must be overall numbers (1–257), not per-round. Rows without a number, like "Round 2" headers, are ignored.
- Any number of teams or rounds works: the bot just follows the rows.
- Different tab name or columns? Set `DRAFT_TAB`, `FIRST_PICK_ROW`, `COL_*` (see `.env.example`).
- The bot adds two tabs: `GMs` (team → Discord user) and `Trades` (log).
- Traded picks get the new team in column B and a note like `[from CAR via NYJ]` in column F.

## Setup

**1. Discord bot**
1. https://discord.com/developers/applications → New Application → Bot → Reset Token → copy it (`DISCORD_TOKEN`).
2. OAuth2 → URL Generator → scopes `bot` + `applications.commands`, permissions `Send Messages` + `View Channels`. Open the URL to invite the bot.

**2. Google access**
1. https://console.cloud.google.com → new project → enable the **Google Sheets API**.
2. IAM & Admin → Service Accounts → create one → Keys → Add key → JSON. The file's contents are `GOOGLE_CREDENTIALS_JSON`.
3. Share your sheet with the service account's email (`...@...iam.gserviceaccount.com`) as **Editor**.
4. `SHEET_ID` is the long string in the sheet URL between `/d/` and `/edit`.

**3. Host (Railway)**
1. Put this folder in a GitHub repo → https://railway.app → New Project → Deploy from GitHub.
2. Variables tab: add `DISCORD_TOKEN`, `SHEET_ID`, `GOOGLE_CREDENTIALS_JSON`, plus `GUILD_ID` (right-click your server → Copy Server ID) so commands appear immediately.
3. It starts with `python bot.py` (from the `Procfile`). Render or Fly.io work the same way as a "background worker".

**4. In Discord**: `/assign` each team to its GM, then `/onclock` to start.

## Run locally to test

```
pip install -r requirements.txt
export DISCORD_TOKEN=... SHEET_ID=... GOOGLE_CREDENTIALS_JSON="$(cat key.json)"
python bot.py
```
