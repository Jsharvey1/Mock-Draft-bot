"""Google Sheets layer. The sheet is the single source of truth:
the pick on the clock is simply the first pick row with an empty player cell."""
import datetime
import json
import os
import re
import time
from dataclasses import dataclass

import gspread


def _col(letter: str) -> int:
    """Column letter -> zero-based index. '' -> -1 (column not used)."""
    letter = (letter or "").strip().upper()
    if not letter:
        return -1
    n = 0
    for ch in letter:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


@dataclass
class Pick:
    row: int        # 1-based sheet row
    number: int     # overall pick number
    team: str
    player: str
    position: str
    school: str
    notes: str

    @property
    def made(self) -> bool:
        return bool(self.player.strip())


class Layout:
    """Where things live in the draft tab. Every value can be overridden by env var."""

    def __init__(self, env=os.environ):
        self.draft_tab = env.get("DRAFT_TAB", "Draft")
        self.gm_tab = env.get("GM_TAB", "GMs")
        self.trade_tab = env.get("TRADE_TAB", "Trades")
        self.first_row = int(env.get("FIRST_PICK_ROW", "2"))
        self.pick = env.get("COL_PICK", "A")
        self.team = env.get("COL_TEAM", "B")
        self.player = env.get("COL_PLAYER", "C")
        self.position = env.get("COL_POSITION", "D")
        self.school = env.get("COL_SCHOOL", "E")
        self.notes = env.get("COL_NOTES", "F")


def open_spreadsheet():
    creds = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    return gspread.service_account_from_dict(creds).open_by_key(os.environ["SHEET_ID"])


class DraftSheet:
    def __init__(self, spreadsheet, layout: Layout | None = None):
        self.ss = spreadsheet
        self.l = layout or Layout()
        self.draft = spreadsheet.worksheet(self.l.draft_tab)
        self.gm_ws = self._get_or_create(self.l.gm_tab, ["Team", "Discord User ID", "Discord Name"])
        self.trade_ws = self._get_or_create(
            self.l.trade_tab, ["Time (UTC)", "Team A", "Team B", "A sends picks", "B sends picks", "Other terms"])
        self._cache = (0.0, [])

    def _get_or_create(self, title, headers):
        try:
            return self.ss.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self.ss.add_worksheet(title=title, rows=200, cols=len(headers))
            ws.append_row(headers, value_input_option="RAW")
            return ws

    # ---------- reading ----------
    def picks(self, max_age: float = 0) -> list[Pick]:
        """All pick rows, in sheet order. max_age>0 allows a cached copy (for autocomplete)."""
        ts, cached = self._cache
        if max_age and time.time() - ts < max_age:
            return cached
        l = self.l
        idx = {k: _col(getattr(l, k)) for k in ("pick", "team", "player", "position", "school", "notes")}

        def cell(row, key):
            i = idx[key]
            return row[i].strip() if 0 <= i < len(row) else ""

        out = []
        for r, row in enumerate(self.draft.get_all_values(), start=1):
            if r < l.first_row:
                continue
            m = re.search(r"\d+", cell(row, "pick"))
            team = cell(row, "team")
            if not m or not team:      # round headers, blank rows, etc.
                continue
            out.append(Pick(r, int(m.group()), team, cell(row, "player"),
                            cell(row, "position"), cell(row, "school"), cell(row, "notes")))
        self._cache = (time.time(), out)
        return out

    def teams(self) -> list[str]:
        seen = {}
        for p in self.picks(max_age=30):
            seen.setdefault(p.team.lower(), p.team)
        for team in self.gms():
            seen.setdefault(team.lower(), team)
        return sorted(seen.values())

    def gms(self) -> dict[str, int]:
        """team name -> Discord user id"""
        out = {}
        for row in self.gm_ws.get_all_values()[1:]:
            if len(row) >= 2 and row[0].strip() and row[1].strip().isdigit():
                out[row[0].strip()] = int(row[1].strip())
        return out

    def gm_of(self, team: str, gms: dict[str, int] | None = None) -> int | None:
        gms = self.gms() if gms is None else gms
        for t, uid in gms.items():
            if t.lower() == team.lower():
                return uid
        return None

    # ---------- writing ----------
    def _write(self, updates: dict[tuple[int, str], str]):
        data = []
        for (row, key), value in updates.items():
            letter = getattr(self.l, key).strip().upper()
            if letter:
                data.append({"range": f"{letter}{row}", "values": [[value]]})
        if data:
            self.draft.batch_update(data, value_input_option="RAW")
        self._cache = (0.0, [])

    def record_pick(self, pick: Pick, player: str, position: str, school: str):
        self._write({(pick.row, "player"): player, (pick.row, "position"): position,
                     (pick.row, "school"): school})

    def clear_pick(self, pick: Pick):
        self._write({(pick.row, "player"): "", (pick.row, "position"): "", (pick.row, "school"): ""})

    def set_gm(self, team: str, user_id: int, name: str):
        rows = self.gm_ws.get_all_values()
        for r, row in enumerate(rows, start=1):
            if r > 1 and row and row[0].strip().lower() == team.lower():
                self.gm_ws.batch_update(
                    [{"range": f"A{r}:C{r}", "values": [[team, str(user_id), name]]}],
                    value_input_option="RAW")
                return
        # RAW + str keeps the 18-digit ID from being mangled into a number
        self.gm_ws.append_row([team, str(user_id), name], value_input_option="RAW")

    def apply_trade(self, team_a, team_b, picks_a: list[Pick], picks_b: list[Pick], other: str):
        updates = {}
        for picks, old, new in ((picks_a, team_a, team_b), (picks_b, team_b, team_a)):
            for p in picks:
                updates[(p.row, "team")] = new
                updates[(p.row, "notes")] = _trade_note(p.notes, old)
        self._write(updates)
        self.trade_ws.append_row(
            [datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M"), team_a, team_b,
             ", ".join(str(p.number) for p in picks_a), ", ".join(str(p.number) for p in picks_b), other],
            value_input_option="RAW")


def _trade_note(existing: str, old_team: str) -> str:
    """'' -> '[from DAL]';  '[from CAR]' -> '[from CAR via DAL]'"""
    existing = existing.strip()
    if not existing:
        return f"[from {old_team}]"
    if existing.startswith("[from") and existing.endswith("]"):
        return f"{existing[:-1]} via {old_team}]"
    return f"{existing} [from {old_team}]"
