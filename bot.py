import os
import re
import time
import random
import asyncio
import logging
import sqlite3
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# ============================================================
#  НАЛАШТУВАННЯ (міняй тут, а не по всьому коду)
# ============================================================
LOG_CHANNEL_NAME = "mod-logs"        # канал для логів модерації
VERIFIED_ROLE_NAME = "Verified"      # роль після кнопки верифікації
SUPPORT_ROLE_NAME = "Moderator"      # роль, яка бачить тікети (необов'язкова)

SPAM_LIMIT = 5                       # макс. повідомлень...
SPAM_WINDOW = 8                      # ...за стільки секунд

ALLOWED_DOMAINS = {
    "youtube.com", "youtu.be", "discord.com", "discord.gg",
    "github.com", "wikipedia.org", "google.com",
}
# Заборонені слова (малими літерами). Додай свої.
BAD_WORDS = {"бля", "ідіот", "сука", "мразь", "тварь", "бидло", "чмо", "іди нахуй"}

# True = модератори можуть кидати будь-які лінки й слова
MODS_EXEMPT = False

NEW_ACCOUNT_DAYS = 7                 # акаунти молодші = підозрілі
WARN_LIMIT = 3                       # стільки попереджень = авто-тайм-аут
WARN_TIMEOUT_MIN = 60                # тривалість авто-тайм-ауту
RAID_JOINS = 5                       # стільки входів...
RAID_WINDOW = 10                     # ...за стільки секунд = тривога

XP_COOLDOWN = 60                     # XP дається не частіше ніж раз на N секунд
DAILY_REWARD = 100                   # нагорода за /daily
CURRENCY = "монет"

# ============================================================
#  ПІДГОТОВКА
# ============================================================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("Не знайдено DISCORD_TOKEN. Перевір файл .env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot")

URL_RE = re.compile(r"https?://([^\s/?#]+)", re.IGNORECASE)

# ---------------- База даних (файл bot.db) ----------------
db = sqlite3.connect("bot.db")
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER, user_id INTEGER, mod_id INTEGER,
    reason TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS levels (
    guild_id INTEGER, user_id INTEGER,
    xp INTEGER DEFAULT 0, last_xp REAL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS economy (
    guild_id INTEGER, user_id INTEGER,
    balance INTEGER DEFAULT 0, last_daily REAL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
""")
db.commit()


def level_from_xp(xp: int) -> int:
    return int((xp / 100) ** 0.5)


def xp_for_level(level: int) -> int:
    return 100 * level * level


def add_xp(guild_id: int, user_id: int):
    """Додає XP. Повертає (старий_рівень, новий_рівень) або None, якщо кулдаун."""
    now = time.time()
    gain = random.randint(15, 25)
    row = db.execute("SELECT xp, last_xp FROM levels WHERE guild_id=? AND user_id=?",
                     (guild_id, user_id)).fetchone()
    if row is None:
        db.execute("INSERT INTO levels (guild_id, user_id, xp, last_xp) VALUES (?,?,?,?)",
                   (guild_id, user_id, gain, now))
        db.commit()
        return 0, level_from_xp(gain)
    if now - row["last_xp"] < XP_COOLDOWN:
        return None
    new_xp = row["xp"] + gain
    db.execute("UPDATE levels SET xp=?, last_xp=? WHERE guild_id=? AND user_id=?",
               (new_xp, now, guild_id, user_id))
    db.commit()
    return level_from_xp(row["xp"]), level_from_xp(new_xp)


def get_eco(guild_id: int, user_id: int) -> sqlite3.Row:
    db.execute("INSERT OR IGNORE INTO economy (guild_id, user_id) VALUES (?,?)",
               (guild_id, user_id))
    return db.execute("SELECT balance, last_daily FROM economy WHERE guild_id=? AND user_id=?",
                      (guild_id, user_id)).fetchone()


# ---------------- Допоміжні функції ----------------
def find_bad_links(text: str) -> list[str]:
    bad = []
    for m in URL_RE.finditer(text):
        host = m.group(1).split("@")[-1].split(":")[0].lower().rstrip(".,!?)")
        if not any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS):
            bad.append(host)
    return bad


def has_bad_word(text: str) -> bool:
    words = set(re.findall(r"\w+", text.lower()))
    return bool(words & BAD_WORDS)


async def send_log(guild: discord.Guild, text: str, color=discord.Color.orange()):
    channel = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
    if channel is None:
        return
    embed = discord.Embed(description=text[:4000], color=color,
                          timestamp=discord.utils.utcnow())
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        log.warning("Не вдалося написати в %s", LOG_CHANNEL_NAME)


def can_moderate(interaction: discord.Interaction, target: discord.Member) -> Optional[str]:
    guild = interaction.guild
    if target == interaction.user:
        return "Не можна застосувати це до самого себе."
    if target == guild.me:
        return "Не можна застосувати це до бота."
    if target == guild.owner:
        return "Не можна чіпати власника сервера."
    if interaction.user != guild.owner and target.top_role >= interaction.user.top_role:
        return "Ця людина має роль не нижчу за твою."
    if target.top_role >= guild.me.top_role:
        return "Роль цієї людини вища за роль бота. Підніми роль бота в налаштуваннях сервера."
    return None


# ============================================================
#  КНОПКИ: ВЕРИФІКАЦІЯ І ТІКЕТИ
# ============================================================
class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Я не бот", emoji="✅",
                       style=discord.ButtonStyle.success, custom_id="verify_button")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = discord.utils.get(interaction.guild.roles, name=VERIFIED_ROLE_NAME)
        if role is None:
            await interaction.response.send_message(
                f"Роль «{VERIFIED_ROLE_NAME}» не знайдена. Скажи адміну.", ephemeral=True)
            return
        if role in interaction.user.roles:
            await interaction.response.send_message("Ти вже верифікований ✅", ephemeral=True)
            return
        try:
            await interaction.user.add_roles(role, reason="Пройшов верифікацію")
        except discord.Forbidden:
            await interaction.response.send_message(
                "У бота немає прав видати цю роль. Роль бота має бути вище за "
                f"«{VERIFIED_ROLE_NAME}» і мати право Manage Roles.", ephemeral=True)
            return
        await interaction.response.send_message("Верифікацію пройдено ✅", ephemeral=True)
        await send_log(interaction.guild, f"✅ {interaction.user.mention} пройшов верифікацію",
                       discord.Color.green())


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Закрити тікет", emoji="🔒",
                       style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        is_owner = channel.topic == f"ticket:{interaction.user.id}"
        is_staff = interaction.user.guild_permissions.manage_channels
        if not (is_owner or is_staff):
            await interaction.response.send_message("Закрити тікет може лише його автор або модератор.",
                                                    ephemeral=True)
            return
        await interaction.response.send_message("Тікет закривається через 5 секунд...")
        await send_log(interaction.guild, f"🎫 {interaction.user.mention} закрив тікет `{channel.name}`")
        await asyncio.sleep(5)
        try:
            await channel.delete(reason="Тікет закрито")
        except discord.HTTPException:
            pass


class TicketOpenView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Створити тікет", emoji="🎫",
                       style=discord.ButtonStyle.primary, custom_id="ticket_open")
    async def open(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        name = f"ticket-{interaction.user.id}"
        existing = discord.utils.get(guild.text_channels, name=name)
        if existing:
            await interaction.followup.send(f"У тебе вже є тікет: {existing.mention}", ephemeral=True)
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True),
        }
        support = discord.utils.get(guild.roles, name=SUPPORT_ROLE_NAME)
        if support:
            overwrites[support] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True)

        try:
            channel = await guild.create_text_channel(
                name, overwrites=overwrites, category=interaction.channel.category,
                topic=f"ticket:{interaction.user.id}", reason="Новий тікет")
        except discord.Forbidden:
            await interaction.followup.send(
                "У бота немає права Manage Channels. Скажи адміну.", ephemeral=True)
            return

        await channel.send(
            f"Привіт, {interaction.user.mention}! Опиши свою проблему, і модератори скоро відповідять.",
            view=TicketCloseView())
        await interaction.followup.send(f"Тікет створено: {channel.mention}", ephemeral=True)
        await send_log(guild, f"🎫 {interaction.user.mention} створив тікет {channel.mention}",
                       discord.Color.blue())


# ============================================================
#  БОТ
# ============================================================
class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            # Безпека: бот ніколи не пінгує @everyone і ролі, навіть якщо
            # хтось підсунув такий текст у команду.
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
        self.recent: dict[int, list[float]] = {}        # антиспам
        self.joins: dict[int, list[float]] = {}         # анти-рейд
        self.last_raid_alert: dict[int, float] = {}
        self.silent_deletes: set[int] = set()           # щоб не логувати власні видалення

    async def setup_hook(self):
        self.add_view(VerifyView())
        self.add_view(TicketOpenView())
        self.add_view(TicketCloseView())
        await self.tree.sync()
        log.info("Slash-команди синхронізовано")


bot = MyBot()


async def remove_message(message: discord.Message, warning: str) -> bool:
    """Видаляє повідомлення й пише попередження. True, якщо вдалося."""
    bot.silent_deletes.add(message.id)
    try:
        await message.delete()
        await message.channel.send(warning, delete_after=5)
        return True
    except discord.Forbidden:
        bot.silent_deletes.discard(message.id)
        log.warning("Немає прав видаляти повідомлення в #%s", message.channel)
    except discord.NotFound:
        pass
    return False


@bot.event
async def on_ready():
    log.info("Бот запущений як %s", bot.user)
    log.info("Сервери: %s", [g.name for g in bot.guilds])
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name="за порядком • /help"))


# ---------------- Учасники ----------------
@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    if guild.system_channel:
        await guild.system_channel.send(f"Привіт, {member.mention}! Ласкаво просимо 👋")

    age_days = (discord.utils.utcnow() - member.created_at).days
    text = f"📥 {member.mention} зайшов на сервер (акаунт створено {age_days} дн. тому)"
    if age_days < NEW_ACCOUNT_DAYS:
        text += "\n⚠️ **Новий акаунт, будь уважним: це може бути бот або рейд.**"
    await send_log(guild, text)

    # Анти-рейд: багато входів за короткий час
    now = time.time()
    joins = [t for t in bot.joins.get(guild.id, []) if now - t < RAID_WINDOW]
    joins.append(now)
    bot.joins[guild.id] = joins
    if len(joins) >= RAID_JOINS and now - bot.last_raid_alert.get(guild.id, 0) > 60:
        bot.last_raid_alert[guild.id] = now
        await send_log(guild, f"🚨 **Можливий рейд!** {len(joins)} входів за {RAID_WINDOW} с. "
                              "Перевір список учасників і за потреби скористайся `/lock`.",
                       discord.Color.red())


@bot.event
async def on_member_remove(member: discord.Member):
    await send_log(member.guild, f"📤 {member} вийшов із сервера")


# ---------------- Логи повідомлень ----------------
@bot.event
async def on_message_delete(message: discord.Message):
    if message.id in bot.silent_deletes:
        bot.silent_deletes.discard(message.id)
        return
    if message.guild is None or message.author.bot:
        return
    content = message.content or "(без тексту)"
    await send_log(message.guild,
                   f"🗑️ Повідомлення від {message.author.mention} видалено в {message.channel.mention}\n"
                   f"```{content[:500]}```")


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if after.guild is None or after.author.bot or before.content == after.content:
        return
    await send_log(after.guild,
                   f"✏️ {after.author.mention} змінив повідомлення в {after.channel.mention}\n"
                   f"**Було:** {before.content[:300]}\n**Стало:** {after.content[:300]}")


# ---------------- Головний обробник повідомлень ----------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    exempt = MODS_EXEMPT and message.author.guild_permissions.manage_messages

    if not exempt:
        # 1) Антиспам
        now = time.time()
        times = [t for t in bot.recent.get(message.author.id, []) if now - t < SPAM_WINDOW]
        times.append(now)
        bot.recent[message.author.id] = times
        if len(times) > SPAM_LIMIT:
            await remove_message(message, f"{message.author.mention}, не спам, будь ласка 🙏")
            return

        # 2) Фільтр посилань
        bad = find_bad_links(message.content)
        if bad:
            if await remove_message(message, f"{message.author.mention}, це посилання не дозволене 🚫"):
                await send_log(message.guild,
                               f"🔗 Видалено повідомлення від {message.author.mention} "
                               f"у {message.channel.mention}\nПідозрілі домени: `{', '.join(bad)}`")
            return

        # 3) Заборонені слова
        if has_bad_word(message.content):
            if await remove_message(message, f"{message.author.mention}, стеж за мовою 🚫"):
                await send_log(message.guild,
                               f"🤬 Видалено повідомлення від {message.author.mention} "
                               f"у {message.channel.mention} (заборонене слово)")
            return

    # 4) XP та рівні
    result = add_xp(message.guild.id, message.author.id)
    if result and result[1] > result[0]:
        await message.channel.send(
            f"🎉 {message.author.mention} піднявся до **{result[1]}** рівня!")


# ============================================================
#  КОМАНДИ: ЗАГАЛЬНІ
# ============================================================
@bot.tree.command(name="help", description="Список команд бота")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="Команди бота", color=discord.Color.blurple())
    embed.add_field(name="Загальні", value="`/ping` `/help` `/userinfo` `/serverinfo` `/avatar`", inline=False)
    embed.add_field(name="Рівні", value="`/rank` `/top`", inline=False)
    embed.add_field(name="Економіка", value="`/daily` `/balance` `/pay` `/richest`", inline=False)
    embed.add_field(name="Розваги", value="`/8ball` `/roll` `/coinflip` `/poll`", inline=False)
    embed.add_field(name="Модерація",
                    value="`/warn` `/warnings` `/clearwarns` `/timeout` `/kick` `/ban` `/unban` "
                          "`/clear` `/slowmode` `/lock` `/unlock`", inline=False)
    embed.add_field(name="Адмін", value="`/setup_verify` `/setup_tickets` `/announce`", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="ping", description="Перевірити, чи живий бот")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! 🏓 {round(bot.latency * 1000)} мс")


@bot.tree.command(name="userinfo", description="Інформація про учасника")
@app_commands.describe(member="Про кого показати інформацію")
@app_commands.guild_only()
async def userinfo(interaction: discord.Interaction, member: discord.Member):
    embed = discord.Embed(title=str(member), color=member.color)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ID", value=member.id)
    embed.add_field(name="Акаунт створено", value=discord.utils.format_dt(member.created_at, "R"))
    embed.add_field(name="Зайшов на сервер", value=discord.utils.format_dt(member.joined_at, "R"))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="serverinfo", description="Інформація про сервер")
@app_commands.guild_only()
async def serverinfo(interaction: discord.Interaction):
    g = interaction.guild
    embed = discord.Embed(title=g.name, color=discord.Color.blurple())
    embed.add_field(name="Учасників", value=g.member_count)
    embed.add_field(name="Каналів", value=len(g.channels))
    embed.add_field(name="Ролей", value=len(g.roles))
    embed.add_field(name="Створено", value=discord.utils.format_dt(g.created_at, "D"))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="avatar", description="Показати аватар учасника")
@app_commands.describe(member="Чий аватар показати")
@app_commands.guild_only()
async def avatar(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    embed = discord.Embed(title=f"Аватар {member.display_name}", color=member.color)
    embed.set_image(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)


# ============================================================
#  КОМАНДИ: РІВНІ
# ============================================================
@bot.tree.command(name="rank", description="Показати рівень і досвід")
@app_commands.describe(member="Чий рівень показати")
@app_commands.guild_only()
async def rank(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    row = db.execute("SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
                     (interaction.guild.id, member.id)).fetchone()
    xp = row["xp"] if row else 0
    level = level_from_xp(xp)
    floor_xp, next_xp = xp_for_level(level), xp_for_level(level + 1)
    filled = int(10 * (xp - floor_xp) / (next_xp - floor_xp))
    bar = "█" * filled + "░" * (10 - filled)
    place = db.execute("SELECT COUNT(*) + 1 AS p FROM levels WHERE guild_id=? AND xp > ?",
                       (interaction.guild.id, xp)).fetchone()["p"]
    embed = discord.Embed(title=f"Рівень {member.display_name}", color=member.color)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Рівень", value=level)
    embed.add_field(name="Місце", value=f"#{place}")
    embed.add_field(name="Досвід", value=f"{xp} / {next_xp}", inline=False)
    embed.add_field(name="Прогрес", value=f"`{bar}`", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="top", description="Таблиця лідерів за рівнем")
@app_commands.guild_only()
async def top(interaction: discord.Interaction):
    rows = db.execute("SELECT user_id, xp FROM levels WHERE guild_id=? ORDER BY xp DESC LIMIT 10",
                      (interaction.guild.id,)).fetchall()
    if not rows:
        await interaction.response.send_message("Поки що ніхто не набрав досвіду.")
        return
    lines = [f"**{i}.** <@{r['user_id']}> — рівень {level_from_xp(r['xp'])} ({r['xp']} XP)"
             for i, r in enumerate(rows, 1)]
    embed = discord.Embed(title="🏆 Топ за рівнем", description="\n".join(lines),
                          color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)


# ============================================================
#  КОМАНДИ: ЕКОНОМІКА
# ============================================================
@bot.tree.command(name="daily", description="Отримати щоденну нагороду")
@app_commands.guild_only()
async def daily(interaction: discord.Interaction):
    gid, uid = interaction.guild.id, interaction.user.id
    row = get_eco(gid, uid)
    now = time.time()
    ready_at = row["last_daily"] + 86400
    if now < ready_at:
        when = discord.utils.format_dt(discord.utils.utcnow() + timedelta(seconds=ready_at - now), "R")
        await interaction.response.send_message(
            f"Ти вже забирав нагороду. Наступна буде {when}.", ephemeral=True)
        return
    db.execute("UPDATE economy SET balance = balance + ?, last_daily = ? WHERE guild_id=? AND user_id=?",
               (DAILY_REWARD, now, gid, uid))
    db.commit()
    await interaction.response.send_message(
        f"💰 Ти отримав **{DAILY_REWARD}** {CURRENCY}! Баланс: **{row['balance'] + DAILY_REWARD}**")


@bot.tree.command(name="balance", description="Показати баланс")
@app_commands.describe(member="Чий баланс показати")
@app_commands.guild_only()
async def balance(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    row = get_eco(interaction.guild.id, member.id)
    db.commit()
    await interaction.response.send_message(f"💰 Баланс {member.display_name}: **{row['balance']}** {CURRENCY}")


@bot.tree.command(name="pay", description="Передати монети іншому учаснику")
@app_commands.describe(member="Кому передати", amount="Скільки")
@app_commands.guild_only()
async def pay(interaction: discord.Interaction, member: discord.Member,
              amount: app_commands.Range[int, 1, 1_000_000]):
    if member == interaction.user or member.bot:
        await interaction.response.send_message("Так не можна 🙂", ephemeral=True)
        return
    gid = interaction.guild.id
    sender = get_eco(gid, interaction.user.id)
    get_eco(gid, member.id)
    if sender["balance"] < amount:
        await interaction.response.send_message(
            f"Не вистачає монет. У тебе: {sender['balance']}", ephemeral=True)
        return
    db.execute("UPDATE economy SET balance = balance - ? WHERE guild_id=? AND user_id=?",
               (amount, gid, interaction.user.id))
    db.execute("UPDATE economy SET balance = balance + ? WHERE guild_id=? AND user_id=?",
               (amount, gid, member.id))
    db.commit()
    await interaction.response.send_message(
        f"✅ {interaction.user.mention} передав {member.mention} **{amount}** {CURRENCY}")


@bot.tree.command(name="richest", description="Найбагатші учасники")
@app_commands.guild_only()
async def richest(interaction: discord.Interaction):
    rows = db.execute("SELECT user_id, balance FROM economy WHERE guild_id=? AND balance > 0 "
                      "ORDER BY balance DESC LIMIT 10", (interaction.guild.id,)).fetchall()
    if not rows:
        await interaction.response.send_message("Поки що ні в кого немає монет. Спробуй `/daily`.")
        return
    lines = [f"**{i}.** <@{r['user_id']}> — {r['balance']} {CURRENCY}" for i, r in enumerate(rows, 1)]
    await interaction.response.send_message(
        embed=discord.Embed(title="💰 Найбагатші", description="\n".join(lines),
                            color=discord.Color.gold()))


# ============================================================
#  КОМАНДИ: РОЗВАГИ
# ============================================================
EIGHT_BALL = ["Так", "Ні", "Скоріше за все так", "Скоріше за все ні", "Запитай пізніше",
              "Без сумнівів", "Навряд чи", "Знаки кажуть «так»", "Не розраховуй на це"]


@bot.tree.command(name="8ball", description="Магічна куля відповість на твоє питання")
@app_commands.describe(question="Твоє питання")
async def eight_ball(interaction: discord.Interaction, question: str):
    await interaction.response.send_message(f"🎱 **{question}**\n{random.choice(EIGHT_BALL)}")


@bot.tree.command(name="roll", description="Кинути кубик")
@app_commands.describe(sides="Скільки граней (за замовчуванням 6)")
async def roll(interaction: discord.Interaction, sides: app_commands.Range[int, 2, 1000] = 6):
    await interaction.response.send_message(f"🎲 Випало: **{random.randint(1, sides)}** (з {sides})")


@bot.tree.command(name="coinflip", description="Підкинути монетку")
async def coinflip(interaction: discord.Interaction):
    await interaction.response.send_message(f"🪙 {random.choice(['Орел', 'Решка'])}!")


@bot.tree.command(name="poll", description="Створити опитування")
@app_commands.describe(question="Питання", option1="Варіант 1", option2="Варіант 2",
                       option3="Варіант 3 (необов'язково)", option4="Варіант 4 (необов'язково)")
@app_commands.guild_only()
async def poll(interaction: discord.Interaction, question: str, option1: str, option2: str,
               option3: Optional[str] = None, option4: Optional[str] = None):
    options = [o for o in (option1, option2, option3, option4) if o]
    emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
    lines = [f"{emojis[i]} {text}" for i, text in enumerate(options)]
    embed = discord.Embed(title=f"📊 {question}", description="\n".join(lines),
                          color=discord.Color.blurple())
    embed.set_footer(text=f"Опитування від {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)
    msg = await interaction.original_response()
    for i in range(len(options)):
        await msg.add_reaction(emojis[i])


# ============================================================
#  КОМАНДИ: МОДЕРАЦІЯ
# ============================================================
@bot.tree.command(name="warn", description="Видати попередження")
@app_commands.describe(member="Кому", reason="Причина")
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    problem = can_moderate(interaction, member)
    if problem:
        await interaction.response.send_message(problem, ephemeral=True)
        return
    gid = interaction.guild.id
    db.execute("INSERT INTO warnings (guild_id, user_id, mod_id, reason, created) VALUES (?,?,?,?,?)",
               (gid, member.id, interaction.user.id, reason, time.time()))
    db.commit()
    count = db.execute("SELECT COUNT(*) AS c FROM warnings WHERE guild_id=? AND user_id=?",
                       (gid, member.id)).fetchone()["c"]

    msg = f"⚠️ {member.mention} отримав попередження ({count}/{WARN_LIMIT}). Причина: {reason}"
    if count >= WARN_LIMIT:
        try:
            await member.timeout(timedelta(minutes=WARN_TIMEOUT_MIN),
                                 reason=f"{WARN_LIMIT} попереджень")
            msg += f"\n🔇 Автоматичний тайм-аут на {WARN_TIMEOUT_MIN} хв."
        except discord.Forbidden:
            msg += "\n(Не вдалося видати тайм-аут: не вистачає прав.)"
    await interaction.response.send_message(msg)
    try:
        await member.send(f"⚠️ Ти отримав попередження на сервері **{interaction.guild.name}**: {reason}")
    except discord.HTTPException:
        pass
    await send_log(interaction.guild,
                   f"⚠️ {interaction.user.mention} попередив {member.mention} ({count}/{WARN_LIMIT})\n"
                   f"Причина: {reason}")


@bot.tree.command(name="warnings", description="Показати попередження учасника")
@app_commands.describe(member="Чиї попередження показати")
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def warnings(interaction: discord.Interaction, member: discord.Member):
    rows = db.execute("SELECT id, reason, created FROM warnings WHERE guild_id=? AND user_id=? "
                      "ORDER BY id DESC LIMIT 10", (interaction.guild.id, member.id)).fetchall()
    if not rows:
        await interaction.response.send_message(f"У {member.display_name} немає попереджень ✅",
                                                ephemeral=True)
        return
    lines = [f"`#{r['id']}` {r['reason']} — <t:{int(r['created'])}:R>" for r in rows]
    await interaction.response.send_message(
        embed=discord.Embed(title=f"Попередження {member.display_name}",
                            description="\n".join(lines), color=discord.Color.orange()),
        ephemeral=True)


@bot.tree.command(name="clearwarns", description="Очистити всі попередження учасника")
@app_commands.describe(member="Кому очистити")
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def clearwarns(interaction: discord.Interaction, member: discord.Member):
    db.execute("DELETE FROM warnings WHERE guild_id=? AND user_id=?",
               (interaction.guild.id, member.id))
    db.commit()
    await interaction.response.send_message(f"🧽 Попередження {member.mention} очищено.")
    await send_log(interaction.guild,
                   f"🧽 {interaction.user.mention} очистив попередження {member.mention}")


@bot.tree.command(name="timeout", description="Заглушити учасника на певний час")
@app_commands.describe(member="Кого", minutes="На скільки хвилин (1-10080)", reason="Причина")
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout(interaction: discord.Interaction, member: discord.Member,
                  minutes: app_commands.Range[int, 1, 10080], reason: str = "без причини"):
    problem = can_moderate(interaction, member)
    if problem:
        await interaction.response.send_message(problem, ephemeral=True)
        return
    await member.timeout(timedelta(minutes=minutes), reason=f"{interaction.user}: {reason}")
    await interaction.response.send_message(
        f"🔇 {member.mention} заглушено на {minutes} хв. Причина: {reason}")
    await send_log(interaction.guild,
                   f"🔇 {interaction.user.mention} заглушив {member.mention} на {minutes} хв\n"
                   f"Причина: {reason}")


@bot.tree.command(name="kick", description="Вигнати учасника")
@app_commands.describe(member="Кого", reason="Причина")
@app_commands.guild_only()
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "без причини"):
    problem = can_moderate(interaction, member)
    if problem:
        await interaction.response.send_message(problem, ephemeral=True)
        return
    await member.kick(reason=f"{interaction.user}: {reason}")
    await interaction.response.send_message(f"👢 {member} вигнано. Причина: {reason}")
    await send_log(interaction.guild, f"👢 {interaction.user.mention} вигнав {member}\nПричина: {reason}")


@bot.tree.command(name="ban", description="Забанити учасника")
@app_commands.describe(member="Кого", reason="Причина")
@app_commands.guild_only()
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "без причини"):
    problem = can_moderate(interaction, member)
    if problem:
        await interaction.response.send_message(problem, ephemeral=True)
        return
    await member.ban(reason=f"{interaction.user}: {reason}")
    await interaction.response.send_message(f"🔨 {member} забанено. Причина: {reason}")
    await send_log(interaction.guild, f"🔨 {interaction.user.mention} забанив {member}\nПричина: {reason}",
                   discord.Color.red())


@bot.tree.command(name="unban", description="Розбанити за ID користувача")
@app_commands.describe(user_id="ID користувача (число)")
@app_commands.guild_only()
@app_commands.checks.has_permissions(ban_members=True)
async def unban(interaction: discord.Interaction, user_id: str):
    try:
        uid = int(user_id.strip())
    except ValueError:
        await interaction.response.send_message("ID має бути числом.", ephemeral=True)
        return
    try:
        await interaction.guild.unban(discord.Object(id=uid))
    except discord.NotFound:
        await interaction.response.send_message("Такого користувача немає в списку банів.", ephemeral=True)
        return
    await interaction.response.send_message(f"✅ Користувача `{uid}` розбанено.")
    await send_log(interaction.guild, f"✅ {interaction.user.mention} розбанив `{uid}`", discord.Color.green())


@bot.tree.command(name="clear", description="Видалити останні повідомлення в чаті")
@app_commands.describe(amount="Скільки повідомлень видалити (1-100)")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_messages=True)
async def clear(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"Видалено {len(deleted)} повідомлень.", ephemeral=True)
    await send_log(interaction.guild,
                   f"🧹 {interaction.user.mention} видалив {len(deleted)} повідомлень "
                   f"у {interaction.channel.mention}")


@bot.tree.command(name="slowmode", description="Встановити повільний режим у чаті")
@app_commands.describe(seconds="Пауза між повідомленнями в секундах (0 = вимкнути)")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True)
async def slowmode(interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
    await interaction.channel.edit(slowmode_delay=seconds)
    text = "вимкнено" if seconds == 0 else f"{seconds} с"
    await interaction.response.send_message(f"🐌 Повільний режим: **{text}**")
    await send_log(interaction.guild,
                   f"🐌 {interaction.user.mention} встановив slowmode {text} у {interaction.channel.mention}")


@bot.tree.command(name="lock", description="Закрити чат для звичайних учасників")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True)
async def lock(interaction: discord.Interaction):
    everyone = interaction.guild.default_role
    ow = interaction.channel.overwrites_for(everyone)
    ow.send_messages = False
    await interaction.channel.set_permissions(everyone, overwrite=ow)
    await interaction.response.send_message("🔒 Чат закрито.")
    await send_log(interaction.guild, f"🔒 {interaction.user.mention} закрив {interaction.channel.mention}")


@bot.tree.command(name="unlock", description="Відкрити чат знову")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True)
async def unlock(interaction: discord.Interaction):
    everyone = interaction.guild.default_role
    ow = interaction.channel.overwrites_for(everyone)
    ow.send_messages = None
    await interaction.channel.set_permissions(everyone, overwrite=ow)
    await interaction.response.send_message("🔓 Чат відкрито.")
    await send_log(interaction.guild, f"🔓 {interaction.user.mention} відкрив {interaction.channel.mention}")


# ============================================================
#  КОМАНДИ: АДМІН
# ============================================================
@bot.tree.command(name="setup_verify", description="Створити повідомлення з кнопкою верифікації")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_verify(interaction: discord.Interaction):
    embed = discord.Embed(title="Верифікація",
                          description="Натисни кнопку нижче, щоб отримати доступ до сервера.",
                          color=discord.Color.green())
    await interaction.channel.send(embed=embed, view=VerifyView())
    await interaction.response.send_message("Готово ✅", ephemeral=True)


@bot.tree.command(name="setup_tickets", description="Створити повідомлення з кнопкою тікетів")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_tickets(interaction: discord.Interaction):
    embed = discord.Embed(title="Підтримка",
                          description="Є питання або проблема? Натисни кнопку, і для тебе "
                                      "створиться приватний чат із модераторами.",
                          color=discord.Color.blue())
    await interaction.channel.send(embed=embed, view=TicketOpenView())
    await interaction.response.send_message("Готово ✅", ephemeral=True)


@bot.tree.command(name="announce", description="Надіслати оголошення в канал")
@app_commands.describe(channel="Куди надіслати", title="Заголовок", text="Текст оголошення")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def announce(interaction: discord.Interaction, channel: discord.TextChannel,
                   title: str, text: str):
    embed = discord.Embed(title=title, description=text, color=discord.Color.gold(),
                          timestamp=discord.utils.utcnow())
    embed.set_footer(text=f"Від {interaction.user.display_name}")
    await channel.send(embed=embed)
    await interaction.response.send_message(f"Оголошення надіслано в {channel.mention} ✅", ephemeral=True)


# ============================================================
#  ОБРОБКА ПОМИЛОК
# ============================================================
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "У тебе немає прав для цієї команди."
    elif isinstance(error, app_commands.CommandInvokeError) and isinstance(error.original, discord.Forbidden):
        msg = "У бота не вистачає прав для цієї дії. Перевір права та роль бота."
    else:
        log.error("Помилка команди", exc_info=error)
        msg = "Щось пішло не так. Помилку записано в консоль."

    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


bot.run(TOKEN)