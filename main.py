import discord
from discord import app_commands
from datetime import datetime, timezone, timedelta, time as dtime
from zoneinfo import ZoneInfo
from discord.ext import tasks
from dotenv import load_dotenv
import os
import random
import string
import time as pytime

import gspread
from google.oauth2.service_account import Credentials
import requests  # til Clash of Clans API


# ================== TIMEZONE ==================
DK_TZ = ZoneInfo("Europe/Copenhagen")

# ================== LOAD .ENV ==================

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

COC_API_TOKENS = [
    t
    for t in [
        os.getenv("COC_API_TOKEN_SERVER"),
        os.getenv("COC_API_TOKEN_HOME"),
        os.getenv("COC_API_TOKEN_WORK"),
        os.getenv("COC_API_TOKEN"),
    ]
    if t
]

if TOKEN is None:
    print("[FEJL] DISCORD_TOKEN findes ikke i .env")
    raise SystemExit(1)

if not COC_API_TOKENS:
    print("[ADVARSEL] Ingen COC_API_TOKEN_* fundet i .env – Clash of Clans opslag vil ikke virke.")


# ================== INDSTILLINGER ==================

OUR_CLAN_TAG = "#2RL2LGP0Y"  # <-- jeres tag

# Google Sheets
GOOGLE_SERVICE_ACCOUNT_FILE = "service_account.json"
GOOGLE_SHEET_ID = "1Y7mcQZWXVBOBYuVNY74wxkmeJY_Yg8eZgSAhskKnhhM"

STRIKES_WORKSHEET_NAME = "Ark1"
MEMBERS_WORKSHEET_NAME = "Ark2"

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_gspread_client = None
_strikes_ws_cache = None
_members_ws_cache = None


def get_gspread_client():
    global _gspread_client
    if _gspread_client is not None:
        return _gspread_client

    creds = Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_FILE,
        scopes=GOOGLE_SCOPES,
    )
    _gspread_client = gspread.authorize(creds)
    return _gspread_client


def get_strikes_worksheet():
    global _strikes_ws_cache
    if _strikes_ws_cache is not None:
        return _strikes_ws_cache

    client = get_gspread_client()
    sh = client.open_by_key(GOOGLE_SHEET_ID)
    ws = sh.worksheet(STRIKES_WORKSHEET_NAME)
    _strikes_ws_cache = ws
    print("[INFO] Forbundet til strikes-worksheet:", ws.title)
    return ws


def get_members_worksheet():
    global _members_ws_cache
    if _members_ws_cache is not None:
        return _members_ws_cache

    client = get_gspread_client()
    sh = client.open_by_key(GOOGLE_SHEET_ID)
    ws = sh.worksheet(MEMBERS_WORKSHEET_NAME)
    _members_ws_cache = ws
    print("[INFO] Forbundet til members-worksheet:", ws.title)
    return ws


# ================== Clash of Clans API ==================

def normalize_tag(tag: str) -> str:
    """
    Fjerner #, uppercaser og erstatter evt. 'O' med '0'
    (Clash-tags bruger 0, ikke O).
    """
    if not tag:
        return ""
    t = tag.strip().upper().replace("#", "")
    t = t.replace("O", "0")
    return t


def get_coc_player(player_tag: str):
    """
    Slår en Clash of Clans spiller op via officielt API.
    Prøver tokens i rækkefølge.
    player_tag kan være med/uden #.
    """
    if not COC_API_TOKENS:
        return None

    base_url = "https://api.clashofclans.com/v1/players/"
    url = base_url + "%23" + normalize_tag(player_tag)

    last_error = None

    for idx, token in enumerate(COC_API_TOKENS, start=1):
        headers = {"Authorization": f"Bearer {token}"}
        try:
            print(f"[COC API] Forsøger token #{idx} for spiller {normalize_tag(player_tag)}...")
            resp = requests.get(url, headers=headers, timeout=10)

            if resp.status_code == 200:
                print(f"[COC API] Token #{idx} OK for {normalize_tag(player_tag)}")
                return resp.json()

            if resp.status_code in (401, 403):
                print(f"[COC API] Token #{idx} gav {resp.status_code} – prøver næste token hvis muligt.")
                last_error = f"{resp.status_code}: {resp.text}"
                continue

            print(f"[COC API] Fejl {resp.status_code} for {normalize_tag(player_tag)}: {resp.text}")
            last_error = f"{resp.status_code}: {resp.text}"
            break

        except Exception as e:
            print(f"[COC API] Exception med token #{idx} for {normalize_tag(player_tag)}: {e}")
            last_error = str(e)
            continue

    if last_error:
        print(f"[COC API] Alle tokens fejlede for {normalize_tag(player_tag)}. Sidste fejl: {last_error}")
    return None


def determine_role_from_coc_any(player_data: dict) -> str:
    """
    Returnerer rollen som pæn tekst uden at kræve 'vores clan'.
    Hvis ingen clan → Kicked.
    """
    if not player_data:
        return ""

    clan = player_data.get("clan")
    if not clan:
        return "Kicked"

    role_key = (player_data.get("role") or "").strip()
    role_map = {
        "member": "Member",
        "admin": "Elder",
        "coLeader": "Co-leader",
        "leader": "Leader",
    }
    return role_map.get(role_key, role_key.capitalize() if role_key else "")


def determine_role_in_our_clan(player_data: dict) -> str:
    """
    Returnerer rolle hvis spilleren er i VORES clan – ellers 'Kicked'.
    """
    if not player_data:
        return ""

    clan = player_data.get("clan")
    if not clan:
        return "Kicked"

    player_clan_tag = normalize_tag(clan.get("tag", ""))
    our_clan_tag = normalize_tag(OUR_CLAN_TAG)

    if not our_clan_tag:
        return ""

    if player_clan_tag != our_clan_tag:
        return "Kicked"

    role_key = (player_data.get("role") or "").strip()
    role_map = {
        "member": "Member",
        "admin": "Elder",
        "coLeader": "Co-leader",
        "leader": "Leader",
    }
    return role_map.get(role_key, role_key.capitalize() if role_key else "")


# ================== STRIKES HELPERS ==================

def generate_strike_id() -> str:
    chars = string.ascii_uppercase + string.digits
    return "".join(random.choice(chars) for _ in range(5))


def get_total_strikes_for_coc(coc_id_raw: str) -> int:
    """
    Summerer kolonne 4 (Antal strikes) for ALLE rækker for dette COC ID.
    Når udløbne strikes slettes fra arket, falder totalen automatisk.
    """
    ws = get_strikes_worksheet()
    all_values = ws.get_all_values()
    rows = all_values[1:] if len(all_values) > 1 else []

    coc_norm = normalize_tag(coc_id_raw)
    total = 0

    for row in rows:
        if len(row) < 4:
            continue
        row_coc = normalize_tag(row[0] or "")
        if row_coc != coc_norm:
            continue

        try:
            total += int(str(row[3]).strip())
        except Exception:
            pass

    return total


def append_strike_row(ws, row: list) -> None:
    """
    Indsætter en strike-række i første rigtige tomme række i tabellen.

    Vi kigger KUN i kolonne A (COC ID) for at finde sidste brugte række.
    Det betyder, at vi ikke bliver snydt af formatering eller ting langt
    nede i andre kolonner.
    """
    # Hent alle udfyldte værdier i kolonne A (inkl. header i række 1)
    col_a = ws.col_values(1)
    # Næste række er "længden + 1"
    next_row_index = len(col_a) + 1

    # Vi forventer 10 kolonner: A–J
    cell_range = f"A{next_row_index}:J{next_row_index}"
    ws.update(cell_range, [row], value_input_option="USER_ENTERED")
    print(f"[OK] Strike skrevet på række {next_row_index} i Ark1 ✅")


def create_strike_and_build_embed(
    coc_id_raw: str,
    reason: str,
    antal: int,
    giver: discord.Member,
    *,
    strict_coc_lookup: bool = False,
) -> discord.Embed:
    """
    strict_coc_lookup=True:
      - Brug KUN COC API til at finde navn/rolle.
      - Ingen Ark2 fallback.
    strict_coc_lookup=False:
      - Brug COC API, ellers fallback Ark2.
    """
    ws = get_strikes_worksheet()
    coc_norm = normalize_tag(coc_id_raw)
    if not coc_norm:
        raise ValueError("Ugyldigt COC ID.")

    player_data = get_coc_player(coc_norm)

    name = ""
    rolle = ""

    if player_data:
        name = (player_data.get("name") or "").strip()
        # her giver vi rolle uden at kræve vores clan (strikes kan gives til folk der lige er smuttet osv.)
        rolle = determine_role_from_coc_any(player_data)

    if strict_coc_lookup:
        if not player_data or not name:
            raise ValueError(
                "Kunne ikke finde spilleren via Clash of Clans API. "
                "Tjek COC ID og at din API token/IP er korrekt."
            )
    else:
        if not name:
            # fallback Ark2 (kun relevant når man kommer via Discord-user flow)
            try:
                ws_members = get_members_worksheet()
                all_members = ws_members.get_all_values()
                rows = all_members[1:] if len(all_members) > 1 else []
                for row in rows:
                    if not row:
                        continue
                    row_coc = normalize_tag(row[0] or "")
                    if row_coc == coc_norm:
                        name = (row[1] or "").strip()
                        rolle = (row[2] or "").strip()
                        break
            except Exception:
                pass

    if not name:
        name = coc_norm

    # total strikes før + denne
    total_before = get_total_strikes_for_coc(coc_norm)
    total_now = total_before + int(antal)

    now = datetime.now(timezone.utc)
    expiry_date = (now + timedelta(days=30)).date()
    udloeb_str = expiry_date.strftime("%d/%m/%Y")
    dato_tildelt_str = now.strftime("%d/%m/%Y")
    strike_id = generate_strike_id()

    row = [
        coc_norm,                              # COC ID
        name,                                  # NAVN
        rolle,                                 # Rolle
        int(antal),                            # Antal strikes
        int(total_now),                        # Strikes i alt
        reason,                                # Årsag
        udloeb_str,                            # Udløb
        strike_id,                             # Strike ID
        dato_tildelt_str,                      # Dato tildelt
        giver.display_name or giver.name,      # Tildelt af
    ]

    # 👇 NYT: brug vores egen append-funktion, som kun kigger på kolonne A
    append_strike_row(ws, row)

    embed = discord.Embed(
        title="Strike tilføjet",
        description=f"Strike tilføjet til {name} [#{coc_norm}] af {giver.mention}.",
        color=discord.Color.red(),
    )
    embed.add_field(name="Antal", value=str(int(antal)), inline=True)
    embed.add_field(name="Strikes i alt", value=str(int(total_now)), inline=True)
    embed.add_field(name="Udløb", value=udloeb_str, inline=False)
    embed.add_field(name="Årsag", value=reason, inline=False)
    embed.set_footer(text=f"Strike ID: {strike_id} • {now.strftime('%d-%m-%Y %H:%M')}")
    return embed



def get_strikes_for_coc(coc_id_raw: str):
    ws = get_strikes_worksheet()
    all_values = ws.get_all_values()
    rows = all_values[1:] if len(all_values) > 1 else []

    coc_norm = normalize_tag(coc_id_raw)
    strikes = []

    for idx, row in enumerate(rows, start=2):
        if len(row) < 8:
            continue

        row_coc = normalize_tag(row[0] or "")
        if row_coc != coc_norm:
            continue

        entry = {
            "row_index": idx,
            "coc_id": row_coc,
            "navn": row[1] if len(row) > 1 else "",
            "rolle": row[2] if len(row) > 2 else "",
            "antal": row[3] if len(row) > 3 else "",
            "total": row[4] if len(row) > 4 else "",
            "reason": row[5] if len(row) > 5 else "",
            "udloeb": row[6] if len(row) > 6 else "",
            "strike_id": row[7] if len(row) > 7 else "",
            "dato_tildelt": row[8] if len(row) > 8 else "",
            "givet_af": row[9] if len(row) > 9 else "",
        }

        try:
            entry["antal"] = int(entry["antal"])
        except Exception:
            pass

        strikes.append(entry)

    strikes.sort(key=lambda x: x["row_index"], reverse=True)
    return strikes


def build_removed_strike_embed(strike_row: dict, invoker: discord.Member) -> discord.Embed:
    coc_id = strike_row.get("coc_id", "")
    navn = strike_row.get("navn", "") or coc_id
    antal = strike_row.get("antal", "")
    reason = strike_row.get("reason", "")
    udloeb = strike_row.get("udloeb", "")
    strike_id = strike_row.get("strike_id", "")
    dato_tildelt = strike_row.get("dato_tildelt", "")

    now = datetime.now(timezone.utc)

    embed = discord.Embed(
        title="Strike fjernet",
        description=f"Strike fjernet fra {navn} [#{coc_id}] af {invoker.mention}.",
        color=discord.Color.green(),
    )
    embed.add_field(name="Antal", value=str(antal), inline=True)
    if udloeb:
        embed.add_field(name="Oprindelig udløbsdato", value=udloeb, inline=True)
    if dato_tildelt:
        embed.add_field(name="Dato tildelt", value=dato_tildelt, inline=True)
    if reason:
        embed.add_field(name="Årsag", value=reason, inline=False)

    embed.set_footer(text=f"Strike ID: {strike_id} • fjernet {now.strftime('%d-%m-%Y %H:%M')}")
    return embed


# ================== DISCORD CLIENT ==================

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True


class StrikeLoggerClient(discord.Client):
    def __init__(self, **options):
        super().__init__(**options)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        print("[INFO] Slash-commands synced med Discord.")

        # Kør cleanup fast 00:05 hver dag (DK tid)
        self.cleanup_expired_strikes_task.start()

        # Kør update roles fast 00:00 hver dag (DK tid) – IKKE ved opstart
        self.update_roles_task.start()

    async def on_ready(self):
        print(f"[OK] Logget ind som: {self.user} (id={self.user.id})")
        print("[INFO] Botten er klar.")

        try:
            ws = get_strikes_worksheet()
            print(f"[OK] Strikes-sheet klar: {ws.title}")
            ws2 = get_members_worksheet()
            print(f"[OK] Members-sheet klar: {ws2.title}")
        except Exception as e:
            print("[FEJL] Kunne ikke forbinde til Google Sheet:", e)

    # ================== CLEANUP UDLØBNE STRIKES (00:05) ==================

    @tasks.loop(time=dtime(hour=0, minute=5, tzinfo=DK_TZ))
    async def cleanup_expired_strikes_task(self):
        await self.cleanup_expired_strikes()

    @cleanup_expired_strikes_task.before_loop
    async def before_cleanup_expired_strikes_task(self):
        await self.wait_until_ready()

    async def cleanup_expired_strikes(self):
        try:
            ws = get_strikes_worksheet()
            all_values = ws.get_all_values()

            if len(all_values) <= 1:
                print("[CLEANUP] Ingen strikes at gennemgå.")
                return

            rows = all_values[1:]
            today = datetime.now(timezone.utc).date()

            rows_to_delete = []
            for idx, row in enumerate(rows, start=2):
                if len(row) < 7:
                    continue

                udloeb_str = (row[6] or "").strip()
                if not udloeb_str:
                    continue

                try:
                    expiry_date = datetime.strptime(udloeb_str, "%d/%m/%Y").date()
                except ValueError:
                    continue

                if expiry_date < today:
                    rows_to_delete.append(idx)

            if not rows_to_delete:
                print("[CLEANUP] Ingen udløbne strikes at slette i dag.")
                return

            for row_index in reversed(rows_to_delete):
                ws.delete_rows(row_index)

            print(f"[CLEANUP] Slettede {len(rows_to_delete)} udløbne strike-række(r).")

        except Exception as e:
            print("[FEJL] Daglig cleanup af strikes fejlede:", e)

    # ================== UPDATE ROLES (00:00) ==================

    @tasks.loop(time=dtime(hour=0, minute=0, tzinfo=DK_TZ))
    async def update_roles_task(self):
        await self.update_roles_in_sheet()

    @update_roles_task.before_loop
    async def before_update_roles_task(self):
        await self.wait_until_ready()

    async def update_roles_in_sheet(self):
        """
        Opdaterer 'Rolle' i Ark1 ved at slå hver unik COC ID op i COC API
        og sætte rollen til Member/Elder/Co-leader/Leader hvis i vores clan,
        ellers Kicked.
        """
        try:
            ws = get_strikes_worksheet()
            headers = ws.row_values(1)

            try:
                coc_col = headers.index("COC ID") + 1
                rolle_col = headers.index("Rolle") + 1
            except ValueError:
                print("[UPDATE_ROLES] [FEJL] Mangler 'COC ID' eller 'Rolle' i header.")
                return

            all_values = ws.get_all_values()
            if len(all_values) <= 1:
                print("[UPDATE_ROLES] Ingen data i Ark1.")
                return

            # data uden header
            data_rows = all_values[1:]

            updated = 0
            skipped = 0

            # Vi opdaterer række-for-række (ja, det kan være mange — men du kører det kun 1 gang/dag)
            for row_index, row in enumerate(data_rows, start=2):
                if len(row) < coc_col:
                    skipped += 1
                    continue

                coc_id = normalize_tag(row[coc_col - 1] or "")
                if not coc_id:
                    skipped += 1
                    continue

                player = get_coc_player(coc_id)
                rolle = determine_role_in_our_clan(player)

                if not rolle:
                    skipped += 1
                    continue

                ws.update_cell(row_index, rolle_col, rolle)
                updated += 1

                # lille pause for at undgå rate-limit
                pytime.sleep(0.2)

            print(f"[UPDATE_ROLES] Opdaterede: {updated} | Sprang over: {skipped}")

        except Exception as e:
            print("[UPDATE_ROLES] [FEJL] update_roles_in_sheet fejlede:", e)


# ================== INSTANTIER BOT ==================

client = StrikeLoggerClient(intents=intents)

# ================== SLASH: /link_coc ==================

@client.tree.command(
    name="link_coc",
    description="Link en Clash of Clans profil til en Discord-bruger (kun for rollen 'Ledere')",
)
@app_commands.describe(
    player_tag="Player tag, fx #ABC123",
    user="Den Discord-bruger, profilen skal linkes til",
)
async def link_coc(interaction: discord.Interaction, player_tag: str, user: discord.Member):
    if interaction.guild is None:
        await interaction.response.send_message("Denne kommando kan kun bruges på serveren.", ephemeral=True)
        return

    assert isinstance(interaction.user, discord.Member)
    invoker: discord.Member = interaction.user

    is_leader = any(role.name == "Ledere" for role in invoker.roles)
    if not is_leader:
        await interaction.response.send_message("Kun brugere med rollen **Ledere** kan bruge denne kommando.", ephemeral=True)
        return

    tag_input = (player_tag or "").strip().upper()
    if not tag_input:
        await interaction.response.send_message("Du skal skrive et player tag, fx `#GP9UUQP92`.", ephemeral=True)
        return

    if not tag_input.startswith("#"):
        tag_input = "#" + tag_input

    clean_tag = normalize_tag(tag_input)

    player_data = get_coc_player(clean_tag)
    if not player_data:
        await interaction.response.send_message("Jeg kunne ikke finde en spiller med det tag. Tjek at du har skrevet det rigtigt.", ephemeral=True)
        return

    name = player_data.get("name", "?")
    coc_id = clean_tag

    # Rolle i VORES clan (ellers Kicked)
    role_text = determine_role_in_our_clan(player_data) or "Kicked"

    th_level = player_data.get("townHallLevel")
    th_text = f"TH{th_level}" if th_level else ""

    role_names = [r.name for r in user.roles]
    if "Ledere" in role_names:
        discord_rank = "Ledere"
    elif "Elder" in role_names:
        discord_rank = "Elder"
    elif "Member" in role_names:
        discord_rank = "Member"
    else:
        discord_rank = "None"

    try:
        ws_members = get_members_worksheet()
        all_values = ws_members.get_all_values()
        data_rows = all_values[1:] if len(all_values) > 1 else []

        row_to_update = None
        for idx, row in enumerate(data_rows, start=2):
            if not row:
                continue
            row_coc = normalize_tag((row[0] or ""))
            if row_coc == coc_id:
                row_to_update = idx
                break

        new_row = [
            coc_id,
            name,
            role_text,
            th_text,
            user.name,
            discord_rank,
            str(user.id),
        ]

        if row_to_update:
            ws_members.update(f"A{row_to_update}:G{row_to_update}", [new_row])
            print(f"[OK] Opdaterede række {row_to_update}")
        else:
            ws_members.append_row(new_row, value_input_option="USER_ENTERED")
            print(f"[OK] Tilføjede ny række for {coc_id}")

        msg = f"Hej {invoker.mention}! COC-profilen **{name} (#{coc_id})** er nu linket til {user.mention}."
        await interaction.response.send_message(msg, ephemeral=True)

    except Exception as e:
        print("[FEJL] Kunne ikke skrive til members-worksheet:", e)
        await interaction.response.send_message("Der skete en fejl da jeg forsøgte at gemme linket i regnearket.", ephemeral=True)


# ================== SLASH: /unlink_coc ==================

@client.tree.command(
    name="unlink_coc",
    description="Fjern linket mellem et COC player tag og en Discord-bruger (kun Ledere)",
)
@app_commands.describe(player_tag="Player tag, fx #ABC123, som skal un-linkes")
async def unlink_coc(interaction: discord.Interaction, player_tag: str):
    if interaction.guild is None:
        await interaction.response.send_message("Denne kommando kan kun bruges på serveren.", ephemeral=True)
        return

    assert isinstance(interaction.user, discord.Member)
    invoker: discord.Member = interaction.user

    is_leader = any(role.name == "Ledere" for role in invoker.roles)
    if not is_leader:
        await interaction.response.send_message("Kun brugere med rollen **Ledere** kan bruge denne kommando.", ephemeral=True)
        return

    tag_input = (player_tag or "").strip().upper()
    if not tag_input:
        await interaction.response.send_message("Du skal skrive et player tag, fx `#GP9UUQP92`.", ephemeral=True)
        return

    clean_tag = normalize_tag(tag_input)

    try:
        ws_members = get_members_worksheet()
        all_values = ws_members.get_all_values()
        data_rows = all_values[1:] if len(all_values) > 1 else []

        row_to_delete = None
        for idx, row in enumerate(data_rows, start=2):
            if not row:
                continue
            row_coc = normalize_tag(str(row[0] if len(row) > 0 else ""))
            if row_coc == clean_tag:
                row_to_delete = idx
                break

        if row_to_delete is None:
            await interaction.response.send_message(f"Jeg kunne ikke finde nogen række i arket med COC ID `#{clean_tag}`.", ephemeral=True)
            return

        ws_members.delete_rows(row_to_delete)
        print(f"[OK] Slettede række {row_to_delete} for COC ID #{clean_tag}")
        await interaction.response.send_message(f"Linket for COC ID `#{clean_tag}` er nu fjernet fra arket.", ephemeral=True)

    except Exception as e:
        print("[FEJL] Kunne ikke slette række i members-worksheet:", e)
        await interaction.response.send_message("Der skete en fejl da jeg forsøgte at fjerne linket i regnearket.", ephemeral=True)


# ================== SLASH: /my_strikes ==================

@client.tree.command(
    name="my_strikes",
    description="Se dine strikes (baseret på linkede COC-profiler).",
)
@app_commands.describe(
    public="Hvis true, sender jeg svaret offentligt i kanalen i stedet for privat.",
    language="Sprog: Dansk (default) eller English.",
    discord_user="(Kun for Ledere) Se strikes for denne bruger i stedet for dig selv.",
)
@app_commands.choices(
    language=[
        app_commands.Choice(name="Dansk", value="da"),
        app_commands.Choice(name="English", value="en"),
    ]
)
async def my_strikes(
    interaction: discord.Interaction,
    public: bool = False,
    language: app_commands.Choice[str] | None = None,
    discord_user: discord.Member | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message("Denne kommando kan kun bruges på serveren.", ephemeral=True)
        return

    assert isinstance(interaction.user, discord.Member)
    invoker: discord.Member = interaction.user

    role_names = [r.name for r in invoker.roles]
    has_min_role = any(r in role_names for r in ("Member", "Elder", "Ledere"))
    is_leader = "Ledere" in role_names

    if not has_min_role:
        await interaction.response.send_message("Du skal mindst have rollen **Member** for at bruge denne kommando.", ephemeral=True)
        return

    if discord_user is not None and not is_leader:
        await interaction.response.send_message("Kun brugere med rollen **Ledere** kan slå andres strikes op.", ephemeral=True)
        return

    target: discord.Member = discord_user or invoker
    lang_code = language.value if language else "da"
    target_id_str = str(target.id)

    try:
        ws_members = get_members_worksheet()
        all_members = ws_members.get_all_values()
        rows = all_members[1:] if len(all_members) > 1 else []

        linked_accounts: list[tuple[str, str]] = []
        for row in rows:
            if len(row) < 7:
                continue
            coc_id = (row[0] or "").strip().upper()
            navn = (row[1] or "").strip()
            discord_id = (row[6] or "").strip()
            if discord_id == target_id_str and coc_id:
                linked_accounts.append((coc_id, navn))

        if not linked_accounts:
            msg = (
                f"Hi {target.mention}! I can't find any linked COC profiles for this Discord account.\n"
                "Ask a leader to link it with `/link_coc`."
            ) if lang_code == "en" else (
                f"Hej {target.mention}! Jeg kan ikke finde nogen linkede COC-profiler til denne Discord-konto.\n"
                "Bed en leder om at linke den med `/link_coc`."
            )
            await interaction.response.send_message(msg, ephemeral=True)
            return

    except Exception as e:
        print("[FEJL] Kunne ikke læse members-worksheet i /my_strikes:", e)
        await interaction.response.send_message("Der skete en fejl da jeg forsøgte at slå de linkede profiler op.", ephemeral=True)
        return

    try:
        ws_strikes = get_strikes_worksheet()
        all_strikes = ws_strikes.get_all_values()
        strike_rows = all_strikes[1:] if len(all_strikes) > 1 else []

        results_lines: list[str] = []

        for coc_id, navn in linked_accounts:
            latest = None
            total_for_coc = 0

            for row in strike_rows:
                if len(row) < 7:
                    continue
                row_coc = (row[0] or "").strip().upper()
                if normalize_tag(row_coc) != normalize_tag(coc_id):
                    continue

                try:
                    antal_row = int(str(row[3]).strip())
                except Exception:
                    antal_row = 0

                total_for_coc += antal_row
                latest = row

            if not latest:
                line = f"{navn} (#{coc_id}): no strikes recorded. 💚" if lang_code == "en" else f"{navn} (#{coc_id}): ingen strikes registreret. 💚"
                results_lines.append(line)
                continue

            try:
                weight = int(str(latest[3]).strip())
            except Exception:
                weight = latest[3]

            reason = latest[5] if len(latest) > 5 else ""
            udloeb = latest[6] if len(latest) > 6 else ""

            if lang_code == "en":
                line = (
                    f"{navn} (#{coc_id}): you have **{total_for_coc} strikes** in total. "
                    f"Last strike gave **{weight}** for **{reason}** (expires **{udloeb}**)."
                )
            else:
                line = (
                    f"{navn} (#{coc_id}): du har **{total_for_coc} strikes** i alt. "
                    f"Sidste strike gav **{weight}** for **{reason}** (udløber **{udloeb}**)."
                )

            results_lines.append(line)

    except Exception as e:
        print("[FEJL] Kunne ikke læse strikes-worksheet i /my_strikes:", e)
        await interaction.response.send_message("Der skete en fejl da jeg forsøgte at finde strikes.", ephemeral=True)
        return

    header = f"Hi {target.mention}! Here are your strikes:\n" if lang_code == "en" else f"Hej {target.mention}! Her er dine strikes:\n"
    body = "\n\n".join(results_lines)

    footer = ""
    if discord_user is not None and discord_user.id != invoker.id:
        footer = f"\n\n_Requested by {invoker.mention}_" if lang_code == "en" else f"\n\n_Forespurgt af {invoker.mention}_"

    full_msg = header + "\n" + body + footer
    await interaction.response.send_message(full_msg, ephemeral=not public)


# ================== UI-KOMPONENTER: /add_strike & /remove_strike ==================

class StrikeProfileSelectView(discord.ui.View):
    def __init__(self, profiles, reason: str, antal: int, invoker: discord.Member):
        super().__init__(timeout=60)
        self.profiles = profiles
        self.reason = reason
        self.antal = antal
        self.invoker = invoker
        self.add_item(StrikeProfileSelect(self))


class StrikeProfileSelect(discord.ui.Select):
    def __init__(self, parent_view: StrikeProfileSelectView):
        self.parent_view = parent_view

        options = []
        for coc_id, navn, rolle, th_level in parent_view.profiles:
            label = navn or coc_id
            desc_parts = []
            if th_level:
                desc_parts.append(th_level)
            if rolle:
                desc_parts.append(rolle)
            desc_parts.append(f"#{coc_id}")
            description = " • ".join(desc_parts)

            options.append(discord.SelectOption(label=label, description=description, value=coc_id))

        super().__init__(
            placeholder="Vælg COC-profil…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent_view.invoker.id:
            await interaction.response.send_message("Kun den leder, der oprettede striken, kan vælge profil.", ephemeral=True)
            return

        coc_id = self.values[0]
        await interaction.response.defer(ephemeral=True, thinking=False)

        try:
            embed = create_strike_and_build_embed(
                coc_id_raw=coc_id,
                reason=self.parent_view.reason,
                antal=self.parent_view.antal,
                giver=self.parent_view.invoker,
                strict_coc_lookup=False,
            )
        except Exception as e:
            await interaction.edit_original_response(content=f"Der skete en fejl ved oprettelse af striken: {e}", view=None)
            return

        await interaction.edit_original_response(content=f"Strike oprettet for profil #{coc_id}.", view=None)

        # Offentligt embed (alle kan se)
        await interaction.followup.send(embed=embed, ephemeral=False)


class RemoveStrikeProfileSelectView(discord.ui.View):
    def __init__(self, profiles, invoker: discord.Member):
        super().__init__(timeout=60)
        self.profiles = profiles
        self.invoker = invoker
        self.add_item(RemoveStrikeProfileSelect(self))


class RemoveStrikeProfileSelect(discord.ui.Select):
    def __init__(self, parent_view: RemoveStrikeProfileSelectView):
        self.parent_view = parent_view

        options = []
        for coc_id, navn, rolle, th_level in parent_view.profiles:
            label = navn or coc_id
            desc_parts = []
            if th_level:
                desc_parts.append(th_level)
            if rolle:
                desc_parts.append(rolle)
            desc_parts.append(f"#{coc_id}")
            description = " • ".join(desc_parts)
            options.append(discord.SelectOption(label=label, description=description, value=coc_id))

        super().__init__(
            placeholder="Vælg COC-profil…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent_view.invoker.id:
            await interaction.response.send_message("Kun den leder, der startede kommandoen, kan vælge profil.", ephemeral=True)
            return

        coc_id = self.values[0]

        try:
            strikes = get_strikes_for_coc(coc_id)
        except Exception as e:
            await interaction.response.send_message(f"Kunne ikke hente strikes for denne profil: {e}", ephemeral=True)
            return

        if not strikes:
            await interaction.response.edit_message(content=f"Profil #{coc_id} har ingen strikes at fjerne.", view=None)
            return

        view = RemoveStrikeSelectView(strikes=strikes, invoker=self.parent_view.invoker)
        await interaction.response.edit_message(content=f"Vælg hvilket strike for profil #{coc_id} du vil fjerne:", view=view)


class RemoveStrikeSelectView(discord.ui.View):
    def __init__(self, strikes, invoker: discord.Member):
        super().__init__(timeout=60)
        self.strikes = strikes
        self.invoker = invoker
        self.add_item(RemoveStrikeSelect(self))


class RemoveStrikeSelect(discord.ui.Select):
    def __init__(self, parent_view: RemoveStrikeSelectView):
        self.parent_view = parent_view

        options = []
        for strike in parent_view.strikes[:25]:
            sid = strike.get("strike_id") or "?"
            antal = strike.get("antal", "?")
            reason = strike.get("reason", "") or "-"
            udloeb = strike.get("udloeb", "") or "-"

            if len(reason) > 60:
                reason = reason[:57] + "..."

            label = f"ID: {sid} • Antal: {antal}"
            description = f"{reason} • Udløb: {udloeb}"

            options.append(discord.SelectOption(label=label, description=description, value=str(strike["row_index"])))

        super().__init__(
            placeholder="Vælg strike…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent_view.invoker.id:
            await interaction.response.send_message("Kun den leder, der startede kommandoen, kan fjerne strike.", ephemeral=True)
            return

        try:
            row_index = int(self.values[0])
        except ValueError:
            await interaction.response.send_message("Ugyldigt valg af strike.", ephemeral=True)
            return

        strike_data = None
        for s in self.parent_view.strikes:
            if s["row_index"] == row_index:
                strike_data = s
                break

        if not strike_data:
            await interaction.response.send_message("Kunne ikke finde data for det valgte strike.", ephemeral=True)
            return

        ws = get_strikes_worksheet()
        await interaction.response.defer(ephemeral=True, thinking=False)

        try:
            ws.delete_rows(row_index)
        except Exception as e:
            await interaction.edit_original_response(content=f"Der skete en fejl da striken skulle fjernes: {e}", view=None)
            return

        embed = build_removed_strike_embed(strike_data, self.parent_view.invoker)
        await interaction.edit_original_response(content=f"Strike med ID {strike_data.get('strike_id', '?')} er fjernet.", view=None)

        # Offentligt embed
        await interaction.followup.send(embed=embed, ephemeral=False)


# ================== SLASH: /add_strike ==================

@client.tree.command(
    name="add_strike",
    description="Tilføj en strike til en spiller (kun for Ledere).",
)
@app_commands.describe(
    reason="Årsag til striken.",
    spiller="Vælg Discord-bruger (valgfri, hvis spilleren er linket).",
    coc_id="COC ID, fx #GP9UUQP92 (bruges især hvis spilleren ikke er linket).",
    antal="Hvor mange strikes denne hændelse giver (default 1).",
)
async def add_strike(
    interaction: discord.Interaction,
    reason: str,
    spiller: discord.Member | None = None,
    coc_id: str | None = None,
    antal: int = 1,
):
    # Kun i guilds
    if interaction.guild is None:
        await interaction.response.send_message(
            "Denne kommando kan kun bruges på serveren.",
            ephemeral=True,
        )
        return

    assert isinstance(interaction.user, discord.Member)
    invoker: discord.Member = interaction.user

    # Kun Ledere
    is_leader = any(role.name == "Ledere" for role in invoker.roles)
    if not is_leader:
        await interaction.response.send_message(
            "Kun brugere med rollen **Ledere** kan bruge denne kommando.",
            ephemeral=True,
        )
        return

    reason = (reason or "").strip()
    if not reason:
        await interaction.response.send_message(
            "Du skal angive en **årsag** til striken.",
            ephemeral=True,
        )
        return

    if antal is None or antal <= 0:
        antal = 1

    # Man skal have enten spiller eller et COC ID
    if spiller is None and not coc_id:
        await interaction.response.send_message(
            "Du skal enten vælge en **Discord-spiller** eller angive et **COC ID**.",
            ephemeral=True,
        )
        return

    # ✅ Defer afhængigt af flow:
    # - coc_id: offentlig (så embed ikke bliver "Kun du kan se denne")
    # - spiller: ephemeral (så dropdown + flow kan køre privat)
    if coc_id:
        await interaction.response.defer(ephemeral=False, thinking=True)
    else:
        await interaction.response.defer(ephemeral=True, thinking=True)

    # =========================
    # === COC ID flow (public)
    # =========================
    if coc_id:
        normalized = normalize_tag(coc_id)

        try:
            embed = create_strike_and_build_embed(
                coc_id_raw=normalized,
                reason=reason,
                antal=antal,
                giver=invoker,
                strict_coc_lookup=True,  # KUN COC API, ingen Ark2 fallback
            )
        except Exception as e:
            # NB: vi har deferred offentligt, men fejl må gerne være privat
            await interaction.followup.send(
                f"Der skete en fejl ved oprettelse af striken: {e}",
                ephemeral=True,
            )
            return

        # Offentlig embed (alle kan se)
        await interaction.followup.send(embed=embed, ephemeral=False)
        return

    # ==========================================
    # === Discord-user flow (dropdown, private)
    # ==========================================
    assert spiller is not None

    try:
        ws_members = get_members_worksheet()
        all_members = ws_members.get_all_values()
        rows = all_members[1:] if len(all_members) > 1 else []
    except Exception as e:
        print("[FEJL] Kunne ikke læse members-worksheet i /add_strike:", e)
        await interaction.followup.send(
            "Der skete en fejl da jeg forsøgte at slå linkede profiler op.",
            ephemeral=True,
        )
        return

    target_id_str = str(spiller.id)
    profiles: list[tuple[str, str, str, str]] = []  # (coc_id, navn, rolle, th_level)

    for row in rows:
        if len(row) < 7:
            continue

        row_coc = (row[0] or "").strip().upper()
        navn = (row[1] or "").strip()
        rolle = (row[2] or "").strip()
        th_level = (row[3] or "").strip()
        discord_id = (row[6] or "").strip()

        if discord_id == target_id_str and row_coc:
            profiles.append((row_coc, navn, rolle, th_level))

    if not profiles:
        await interaction.followup.send(
            "Den valgte Discord-bruger er ikke linket til nogen COC-profiler.\n"
            "Brug `/link_coc`, eller angiv et COC ID direkte i `/add_strike`.",
            ephemeral=True,
        )
        return

    view = StrikeProfileSelectView(
        profiles=profiles,
        reason=reason,
        antal=antal,
        invoker=invoker,
    )

    await interaction.followup.send(
        (
            f"{spiller.mention} har følgende COC-profiler linket.\n"
            "Vælg venligst **hvilken profil** striken skal gives til:"
        ),
        view=view,
        ephemeral=True,
    )



# ================== SLASH: /remove_strike ==================

@client.tree.command(
    name="remove_strike",
    description="Fjern en strike fra en spiller (kun for Ledere).",
)
@app_commands.describe(
    spiller="Vælg Discord-bruger (valgfri – så vælger du profil + strike via dropdowns).",
    strike_id="Strike ID, fx IU55A (valgfri – hvis du kender ID'et og vil fjerne direkte).",
)
async def remove_strike(
    interaction: discord.Interaction,
    spiller: discord.Member | None = None,
    strike_id: str | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message("Denne kommando kan kun bruges på serveren.", ephemeral=True)
        return

    assert isinstance(interaction.user, discord.Member)
    invoker: discord.Member = interaction.user

    is_leader = any(role.name == "Ledere" for role in invoker.roles)
    if not is_leader:
        await interaction.response.send_message("Kun brugere med rollen **Ledere** kan bruge denne kommando.", ephemeral=True)
        return

    if not spiller and not strike_id:
        await interaction.response.send_message("Du skal enten vælge en **Discord-spiller** eller angive et **Strike ID**.", ephemeral=True)
        return

    # Direkte via strike_id
    if strike_id:
        sid = (strike_id or "").strip().upper()
        if not sid:
            await interaction.response.send_message("Du skal angive et gyldigt Strike ID.", ephemeral=True)
            return

        ws = get_strikes_worksheet()
        all_values = ws.get_all_values()
        rows = all_values[1:] if len(all_values) > 1 else []

        match_row = None
        match_index = None

        for idx, row in enumerate(rows, start=2):
            if len(row) < 8:
                continue
            row_sid = (row[7] or "").strip().upper()
            if row_sid == sid:
                match_index = idx
                match_row = row
                break

        if match_row is None or match_index is None:
            await interaction.response.send_message(f"Jeg kunne ikke finde nogen strike med ID `{sid}`.", ephemeral=True)
            return

        strike_data = {
            "row_index": match_index,
            "coc_id": normalize_tag(match_row[0] if len(match_row) > 0 else ""),
            "navn": match_row[1] if len(match_row) > 1 else "",
            "rolle": match_row[2] if len(match_row) > 2 else "",
            "antal": match_row[3] if len(match_row) > 3 else "",
            "total": match_row[4] if len(match_row) > 4 else "",
            "reason": match_row[5] if len(match_row) > 5 else "",
            "udloeb": match_row[6] if len(match_row) > 6 else "",
            "strike_id": match_row[7] if len(match_row) > 7 else "",
            "dato_tildelt": match_row[8] if len(match_row) > 8 else "",
            "givet_af": match_row[9] if len(match_row) > 9 else "",
        }

        try:
            strike_data["antal"] = int(strike_data["antal"])
        except Exception:
            pass

        try:
            ws.delete_rows(match_index)
        except Exception as e:
            await interaction.response.send_message(f"Der skete en fejl da striken skulle fjernes: {e}", ephemeral=True)
            return

        embed = build_removed_strike_embed(strike_data, invoker)
        await interaction.response.send_message(embed=embed, ephemeral=False)
        return

    # Dropdown-flow via spiller
    assert spiller is not None

    try:
        ws_members = get_members_worksheet()
        all_members = ws_members.get_all_values()
        rows = all_members[1:] if len(all_members) > 1 else []
    except Exception as e:
        print("[FEJL] Kunne ikke læse members-worksheet i /remove_strike:", e)
        await interaction.response.send_message("Der skete en fejl da jeg forsøgte at slå linkede profiler op.", ephemeral=True)
        return

    target_id_str = str(spiller.id)
    profiles: list[tuple[str, str, str, str]] = []

    for row in rows:
        if len(row) < 7:
            continue
        row_coc = (row[0] or "").strip().upper()
        navn = (row[1] or "").strip()
        rolle = (row[2] or "").strip()
        th_level = (row[3] or "").strip()
        discord_id = (row[6] or "").strip()

        if discord_id == target_id_str and row_coc:
            profiles.append((row_coc, navn, rolle, th_level))

    if not profiles:
        await interaction.response.send_message(
            "Den valgte Discord-bruger er ikke linket til nogen COC-profiler.\n"
            "Brug `/link_coc`, eller angiv et Strike ID direkte.",
            ephemeral=True,
        )
        return

    view = RemoveStrikeProfileSelectView(profiles=profiles, invoker=invoker)
    await interaction.response.send_message(
        f"{spiller.mention} har følgende COC-profiler linket.\nVælg først **hvilken profil** du vil se strikes for:",
        view=view,
        ephemeral=True,
    )


# ================== START BOT ==================
client.run(TOKEN)
