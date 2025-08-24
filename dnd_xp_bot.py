# dnd_xp_bot.py
import os
import json
import asyncio
from typing import Dict, Any, Optional, Tuple, List

import discord
from discord import app_commands
from discord.ext import commands

# -------------------- Конфиг --------------------
DATA_FILE = os.getenv("XP_DATA_FILE", "xp_data.json")
MAX_MESSAGE_LEN = 1900
PROGRESS_WIDTH = 20

# Цвета
COLOR_PRIMARY = discord.Color.from_rgb(52, 152, 219)
COLOR_SUCCESS = discord.Color.from_rgb(46, 204, 113)
COLOR_WARN    = discord.Color.from_rgb(241, 196, 15)
COLOR_ERROR   = discord.Color.from_rgb(231, 76, 60)
COLOR_INFO    = discord.Color.from_rgb(149, 165, 166)

# Кумулятивные пороги XP для уровней 1–20 (D&D 5e)
XP_THRESHOLDS = [
    0,      # 1
    300,    # 2
    900,    # 3
    2700,   # 4
    6500,   # 5
    14000,  # 6
    23000,  # 7
    34000,  # 8
    48000,  # 9
    64000,  # 10
    85000,  # 11
    100000, # 12
    120000, # 13
    140000, # 14
    165000, # 15
    195000, # 16
    225000, # 17
    265000, # 18
    305000, # 19
    355000  # 20
]

# -------------------- Хранилище --------------------
db_lock = asyncio.Lock()

def load_db() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_db(db: Dict[str, Any]) -> None:
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)

def guild_bucket(db: Dict[str, Any], guild_id: int) -> Dict[str, Any]:
    gid = str(guild_id)
    if gid not in db:
        db[gid] = {"members": {}}
    return db[gid]

def ensure_member(db_guild: Dict[str, Any], user_id: int) -> Dict[str, Any]:
    members = db_guild["members"]
    uid = str(user_id)
    if uid not in members:
        members[uid] = {"xp": 0}
    return members[uid]

# -------------------- Логика XP --------------------
def level_from_xp(xp_total: int) -> int:
    lvl = 1
    for i, threshold in enumerate(XP_THRESHOLDS, start=1):
        if xp_total >= threshold:
            lvl = i
    return min(lvl, 20)

def next_threshold(level: int) -> Optional[int]:
    if level >= 20:
        return None
    return XP_THRESHOLDS[level]

def prev_threshold(level: int) -> int:
    return XP_THRESHOLDS[level - 1]

def remaining_to_next(xp_total: int) -> Optional[int]:
    lvl = level_from_xp(xp_total)
    nxt = next_threshold(lvl)
    if nxt is None:
        return None
    return max(0, nxt - xp_total)

def progress_in_level(xp_total: int) -> Tuple[int, int, int]:
    lvl = level_from_xp(xp_total)
    if lvl >= 20:
        return 0, 0, 20
    low = prev_threshold(lvl)
    high = next_threshold(lvl)
    cur = xp_total - low
    need = (high - low) if high is not None else 0
    return max(0, cur), max(0, need), lvl

def render_progress_bar(cur: int, need: int, width: int = PROGRESS_WIDTH) -> str:
    if need <= 0:
        return "█" * width
    filled = int(round(width * (cur / need)))
    filled = max(0, min(width, filled))
    return ("█" * filled) + ("░" * (width - filled))

def render_progress_abs(xp_total: int) -> Tuple[str, Optional[int]]:
    """
    - "<low> → <xp_total> из <high> (<percent>%)"  для уровней 1–19
    - "MAX"                                        для 20 уровня
    """
    cur, need, lvl = progress_in_level(xp_total)
    if lvl == 20:
        return "MAX", None
    low = prev_threshold(lvl)
    high = next_threshold(lvl)
    percent = int(cur / need * 100) if need else 100
    return f"{low} → {xp_total} из {high} ({percent}%)", percent

# -------------------- Discord setup --------------------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# -------------------- Утилиты оформления --------------------
def make_embed(
    title: str,
    description: Optional[str] = None,
    *,
    color: discord.Color = COLOR_PRIMARY,
    user: Optional[discord.abc.User] = None,
    guild: Optional[discord.Guild] = None
) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.timestamp = discord.utils.utcnow()
    if user is not None:
        embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    if guild is not None:
        embed.set_footer(text=guild.name, icon_url=guild.icon.url if guild.icon else discord.Embed.Empty)
    return embed

async def _get_member_safe(guild: discord.Guild, uid: int) -> Optional[discord.Member]:
    m = guild.get_member(uid)
    if m is not None:
        return m
    try:
        return await guild.fetch_member(uid)
    except discord.NotFound:
        return None

async def send_text_safely(interaction: discord.Interaction, text: str, ephemeral: bool = False):
    chunks: List[str] = []
    start = 0
    while start < len(text):
        chunks.append(text[start:start + MAX_MESSAGE_LEN])
        start += MAX_MESSAGE_LEN
    if not interaction.response.is_done():
        await interaction.response.send_message(chunks[0], ephemeral=ephemeral)
        chunks = chunks[1:]
    for ch in chunks:
        await interaction.followup.send(ch, ephemeral=ephemeral)

async def send_embed_safely(interaction: discord.Interaction, embed: discord.Embed, ephemeral: bool = False):
    if not interaction.response.is_done():
        await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
    else:
        await interaction.followup.send(embed=embed, ephemeral=ephemeral)

# ---- гильдейный синк ----
async def _copy_to_guild_and_sync(guild: discord.Guild | int):
    gid = guild.id if isinstance(guild, discord.Guild) else guild
    gobj = discord.Object(id=gid)
    bot.tree.clear_commands(guild=gobj)
    bot.tree.copy_global_to(guild=gobj)
    await bot.tree.sync(guild=gobj)

async def _ensure_guild_has_all_commands(guild: discord.Guild):
    # Форсим копирование и синк всегда, чтобы изменения параметров сразу появлялись
    await _copy_to_guild_and_sync(guild)

@bot.event
async def on_ready():
    try:
        for guild in bot.guilds:
            await _ensure_guild_has_all_commands(guild)
        print("Slash copied & synced per-guild (forced)")
    except Exception as e:
        print(f"Slash ensure error: {e}")
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

@bot.event
async def on_guild_join(guild: discord.Guild):
    try:
        await _copy_to_guild_and_sync(guild)
        print(f"Slash copied & synced for joined guild {guild.id}")
    except Exception as e:
        print(f"Slash sync on join error: {e}")

# -------------------- Команды --------------------
# helper: кто считается модератором для добавления других
def _is_moderator_or_admin(member: discord.Member) -> bool:
    perms = member.guild_permissions
    if perms.administrator or perms.manage_guild or perms.manage_roles:
        return True
    role_names = {r.name.lower() for r in member.roles}
    if role_names & {"moderator", "модератор", "mod", "мод"}:
        return True
    return False

@bot.tree.command(
    name="join",
    description="Вступить в пати (себя или другого игрока, если у тебя админ/модератор)."
)
@app_commands.describe(
    user="Кого добавить (по умолчанию — себя)",
    level="Стартовый уровень (1–20)",
    xp="Стартовый суммарный XP"
)
async def join(
    interaction: discord.Interaction,
    user: Optional[discord.Member] = None,
    level: app_commands.Range[int, 1, 20] = 1,
    xp: app_commands.Range[int, 0, 1_000_000_000] = 0
):
    # только в гильдии
    if interaction.guild is None:
        return await interaction.response.send_message("Только в сервере.", ephemeral=True)

    target = user or interaction.user

    # проверка прав: если добавляют не себя — нужен админ или модератор
    if target != interaction.user and not _is_moderator_or_admin(interaction.user):
        return await interaction.response.send_message(
            "Только админ или модератор может добавлять других.",
            ephemeral=True
        )

    async with db_lock:
        db = load_db()
        g = guild_bucket(db, interaction.guild_id)
        members = g["members"]
        uid = str(target.id)

        if uid in members:
            xp_now = int(members[uid].get("xp", 0))
            lvl = level_from_xp(xp_now)
            rem = remaining_to_next(xp_now)

            eb = discord.Embed(
                title=f"{target.display_name} уже в пати",
                color=COLOR_WARN
            )
            eb.set_thumbnail(url=target.display_avatar.url)
            eb.add_field(name="Уровень", value=str(lvl), inline=True)
            eb.add_field(name="XP", value=str(xp_now), inline=True)
            if rem is not None:
                eb.add_field(name="До следующего уровня", value=f"{rem} XP", inline=False)

            await send_embed_safely(interaction, eb)
            return

        # новый участник — устанавливаем минимум XP для уровня
        min_xp = XP_THRESHOLDS[level - 1]
        if xp < min_xp:
            xp = min_xp
        members[uid] = {"xp": xp}
        save_db(db)

    cur_level = level_from_xp(xp)
    rem = remaining_to_next(xp)

    eb = discord.Embed(
        title=f"{target.display_name} вступил в пати!",
        color=COLOR_SUCCESS
    )
    eb.set_thumbnail(url=target.display_avatar.url)
    eb.add_field(name="Уровень", value=str(cur_level), inline=True)
    eb.add_field(name="XP", value=str(xp), inline=True)
    if rem is not None:
        eb.add_field(name="До следующего уровня", value=f"{rem} XP", inline=False)

    await send_embed_safely(interaction, eb)

@bot.tree.command(name="status", description="Показать уровень и XP игрока (по умолчанию — себя).")
@app_commands.describe(user="Кого смотреть (по умолчанию — тебя)")
async def status(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    user = user or interaction.user
    db = load_db()
    g = guild_bucket(db, interaction.guild_id)
    m = g["members"].get(str(user.id))

    if not m:
        await interaction.response.send_message(
            "Ты ещё не в пати. Используй /join." if user == interaction.user else f"{user.display_name} ещё не в пати.",
            ephemeral=True
        )
        return

    xp = int(m.get("xp", 0))
    _, _, lvl = progress_in_level(xp)
    rem = remaining_to_next(xp)

    eb = discord.Embed(
        title=f"Статус {user.display_name}",
        color=COLOR_PRIMARY
    )
    eb.set_thumbnail(url=user.display_avatar.url)
    eb.add_field(name="Уровень", value=str(lvl), inline=True)
    eb.add_field(name="XP", value=str(xp), inline=True)

    if rem is not None:
        eb.add_field(name="До следующего уровня", value=f"{rem} XP", inline=False)

    await send_embed_safely(interaction, eb)

@bot.tree.command(name="party", description="Показать список всех участников пати.")
async def party(interaction: discord.Interaction):
    db = load_db()
    g = guild_bucket(db, interaction.guild_id)
    members = g["members"]

    if not members:
        await interaction.response.send_message("Пати пока пустая. Используйте /join.", ephemeral=True)
        return

    sortable = []
    for uid, data in members.items():
        xp = int(data.get("xp", 0))
        lvl = level_from_xp(xp)
        member = await _get_member_safe(interaction.guild, int(uid))
        name = member.display_name if member else f"UID:{uid}"
        sortable.append((-lvl, -xp, name.lower(), xp, lvl, name))
    sortable.sort()

    eb = make_embed(
        f"Состав пати — {len(sortable)} участник(ов)",
        color=COLOR_PRIMARY,
        guild=interaction.guild
    )

    for idx, (_, __, ___, xp, lvl, name) in enumerate(sortable, start=1):
        rem = remaining_to_next(xp)
        val = f"Уровень {lvl} • {xp} XP"
        if rem is not None and lvl < 20:
            val += f"\nДо след.: {rem} XP"

        eb.add_field(
            name=f"{idx}. {name}",
            value=val,
            inline=False
        )

    await send_embed_safely(interaction, eb)

# -------------------- XP операции --------------------
async def add_xp_for_member(guild: discord.Guild, user: discord.Member, add: int) -> Dict[str, Any]:
    async with db_lock:
        db = load_db()
        g = guild_bucket(db, guild.id)
        m = ensure_member(g, user.id)

        before_xp = int(m.get("xp", 0))
        before_lvl = level_from_xp(before_xp)

        m["xp"] = max(0, before_xp + add)
        after_xp = int(m["xp"])
        after_lvl = level_from_xp(after_xp)
        save_db(db)

    leveled = after_lvl > before_lvl
    rem = remaining_to_next(after_xp)
    cur, need, _ = progress_in_level(after_xp)

    return {
        "user": user,
        "add": add,
        "after_xp": after_xp,
        "after_lvl": after_lvl,
        "leveled": leveled,
        "rem": rem,
        "cur_in_level": cur,
        "need_for_level": need,
    }

def build_addxp_embed(interaction: discord.Interaction, info: Dict[str, Any], reason: Optional[str]) -> discord.Embed:
    user = info["user"]
    add = info["add"]
    after_xp = info["after_xp"]
    after_lvl = info["after_lvl"]
    leveled = info["leveled"]
    rem = info["rem"]
    cur = info["cur_in_level"]
    need = info["need_for_level"]

    title = f"{'+' if add >= 0 else ''}{add} XP для {user.display_name}"
    color = COLOR_SUCCESS if add > 0 else (COLOR_ERROR if add < 0 else COLOR_INFO)

    eb = make_embed(title, color=(COLOR_WARN if leveled else color), user=user, guild=interaction.guild)
    eb.add_field(name="Всего XP", value=str(after_xp), inline=True)
    eb.add_field(name="Уровень", value=str(after_lvl), inline=True)

    if after_lvl == 20:
        eb.add_field(name="Прогресс", value="MAX", inline=False)
    else:
        prog_label, _ = render_progress_abs(after_xp)
        eb.add_field(name="Прогресс", value=f"[{render_progress_bar(cur, need)}] {prog_label}", inline=False)
        if rem is not None:
            eb.add_field(name="До следующего уровня", value=f"{rem} XP", inline=True)

    if reason:
        eb.add_field(name="Причина", value=reason, inline=False)

    if leveled:
        eb.set_footer(text=f"{interaction.guild.name} • Уровень повышен!",
                      icon_url=interaction.guild.icon.url if interaction.guild.icon else discord.Embed.Empty)
    return eb

@bot.tree.command(name="addxp", description="Добавить XP игроку и показать прогресс.")
@app_commands.describe(user="Кому добавить", amount="Сколько XP (может быть отрицательным)", reason="За что (необязательно)")
async def addxp(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: app_commands.Range[int, -1_000_000_000, 1_000_000_000],
    reason: Optional[str] = None
):
    if amount == 0:
        eb = make_embed("Ноль XP — ноль эффекта.", color=COLOR_INFO, guild=interaction.guild)
        await send_embed_safely(interaction, eb, ephemeral=True)
        return

    info = await add_xp_for_member(interaction.guild, user, amount)

    eb = discord.Embed(
        title=f"{user.display_name} получил {'+' if amount >= 0 else ''}{amount} XP",
        color=COLOR_PRIMARY
    )
    eb.set_thumbnail(url=user.display_avatar.url)

    eb.add_field(name="Уровень", value=str(info["after_lvl"]), inline=True)
    eb.add_field(name="XP", value=str(info["after_xp"]), inline=True)

    if info["rem"] is not None and info["after_lvl"] < 20:
        eb.add_field(name="До следующего уровня", value=f"{info['rem']} XP", inline=False)

    if reason:
        eb.set_footer(text=f"Причина: {reason}")

    await send_embed_safely(interaction, eb)

    if info["leveled"]:
        await interaction.followup.send(f"🎉 {user.mention} повысил уровень!")

@bot.tree.command(name="addxp_all", description="Добавить XP всем участникам пати (результат — списком).")
@app_commands.describe(amount="Сколько XP (может быть отрицательным)", reason="За что (необязательно)")
async def addxp_all(
    interaction: discord.Interaction,
    amount: app_commands.Range[int, -1_000_000_000, 1_000_000_000],
    reason: Optional[str] = None
):
    if amount == 0:
        eb = make_embed("Ноль XP — ноль эффекта.", color=COLOR_INFO, guild=interaction.guild)
        await send_embed_safely(interaction, eb, ephemeral=True)
        return

    db = load_db()
    g = guild_bucket(db, interaction.guild_id)
    member_ids = list(g["members"].keys())
    if not member_ids:
        eb = make_embed("В пати никого. Пусть все сделают /join.", color=COLOR_INFO, guild=interaction.guild)
        await send_embed_safely(interaction, eb, ephemeral=True)
        return

    leveled_up = []
    eb = make_embed(
        f"Распределение XP ({'+' if amount>=0 else ''}{amount} всем)",
        color=COLOR_PRIMARY,
        guild=interaction.guild
    )

    for uid in member_ids:
        member = await _get_member_safe(interaction.guild, int(uid))
        if member is None:
            continue
        info = await add_xp_for_member(interaction.guild, member, amount)

        if info["leveled"]:
            leveled_up.append(member.mention)

        val = f"{info['after_xp']} XP (ур. {info['after_lvl']})"
        if info["rem"] is not None and info["after_lvl"] < 20:
            val += f"\nДо след.: {info['rem']} XP"
        if reason:
            val += f"\nПричина: {reason}"

        eb.add_field(
            name=member.mention,
            value=val,
            inline=False
        )

    await send_embed_safely(interaction, eb)

    if leveled_up:
        if len(leveled_up) == 1:
            msg = f"🎉 {leveled_up[0]} повысил уровень!"
        else:
            names = ", ".join(leveled_up)
            msg = f"🎉 {names} повысили уровень!"
        await interaction.followup.send(msg)

@bot.tree.command(name="setlevel", description="Админская: установить уровень игроку.")
@app_commands.describe(user="Кому", level="Новый уровень (1–20)")
async def setlevel(
    interaction: discord.Interaction,
    user: discord.Member,
    level: app_commands.Range[int, 1, 20]
):
    if not interaction.user.guild_permissions.administrator:
        eb = make_embed("Нужны права администратора.", color=COLOR_ERROR, guild=interaction.guild)
        await send_embed_safely(interaction, eb, ephemeral=True)
        return

    async with db_lock:
        db = load_db()
        g = guild_bucket(db, interaction.guild_id)
        m = ensure_member(g, user.id)
        m["xp"] = XP_THRESHOLDS[level - 1]
        save_db(db)

    xp = int(m["xp"])
    cur, need, lvl = progress_in_level(xp)
    rem = remaining_to_next(xp)

    eb = make_embed(f"Установлен уровень для {user.display_name}", color=COLOR_WARN, user=user, guild=interaction.guild)
    eb.add_field(name="Уровень", value=str(level), inline=True)
    eb.add_field(name="Всего XP", value=str(xp), inline=True)

    if lvl == 20:
        eb.add_field(name="Прогресс", value="MAX", inline=False)
    else:
        prog_label, _ = render_progress_abs(xp)
        eb.add_field(name="Прогресс", value=f"[{render_progress_bar(cur, need)}] {prog_label}", inline=False)
        if rem is not None:
            eb.add_field(name="До следующего уровня", value=f"{rem} XP", inline=True)
    await send_embed_safely(interaction, eb)

@bot.tree.command(name="xptable", description="Пороговые значения XP для уровней 1–20.")
async def xptable(interaction: discord.Interaction):
    lines = ["Пороги XP (кумулятивно):"]
    for i, threshold in enumerate(XP_THRESHOLDS, start=1):
        lines.append(f"{i:>2}: {threshold}")
    eb = make_embed("Таблица XP 1–20", "```\n" + "\n".join(lines) + "\n```", color=COLOR_INFO, guild=interaction.guild)
    await send_embed_safely(interaction, eb)

# ------------ Старт ------------
token = os.getenv("DISCORD_TOKEN")
if not token:
    raise RuntimeError("Положи токен бота в переменную окружения DISCORD_TOKEN")
bot.run(token)
