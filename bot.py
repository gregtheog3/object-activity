"""
InactivityGuard - Discord Bot
Tracks per-user activity (messages + voice joins) and kicks after a configurable idle period.
Uses slash commands (/) and stores state in Supabase (PostgreSQL).

Requirements:
    pip install discord.py supabase

Env vars needed:
    DISCORD_TOKEN   — your bot token
    SUPABASE_URL    — from Supabase project settings
    SUPABASE_KEY    — service_role key from Supabase project settings
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import asyncio
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client

# ─── CONFIG ──────────────────────────────────────────────────────────────────
TOKEN           = os.getenv("DISCORD_TOKEN", "YOUR_BOT_TOKEN_HERE")
SUPABASE_URL    = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY    = os.getenv("SUPABASE_KEY", "")

DEFAULT_INACTIVITY_DAYS  = 30
AUTO_CHECK_INTERVAL_HOURS = 24
# ─────────────────────────────────────────────────────────────────────────────


# ─── SUPABASE CLIENT ─────────────────────────────────────────────────────────
sb: Client = None

def get_sb() -> Client:
    global sb
    if sb is None:
        sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    return sb
# ─────────────────────────────────────────────────────────────────────────────


# ─── DB HELPERS ──────────────────────────────────────────────────────────────

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def utcnow_iso() -> str:
    return utcnow().isoformat()


def get_guild_settings(guild_id: int) -> dict:
    res = get_sb().table("guilds").select("*").eq("guild_id", str(guild_id)).execute()
    if res.data:
        return res.data[0]
    row = {
        "guild_id": str(guild_id),
        "inactivity_days": DEFAULT_INACTIVITY_DAYS,
        "log_channel": None,
        "exempt_roles": [],
        "tracking_since": utcnow_iso(),
    }
    get_sb().table("guilds").insert(row).execute()
    return row


def update_guild_settings(guild_id: int, **kwargs) -> None:
    get_sb().table("guilds").upsert({"guild_id": str(guild_id), **kwargs}).execute()


def set_last_seen(guild_id: int, user_id: int, dt: datetime = None) -> None:
    iso = (dt or utcnow()).isoformat()
    get_sb().table("activity").upsert({
        "guild_id": str(guild_id),
        "user_id":  str(user_id),
        "last_seen": iso,
    }).execute()


def get_last_seen(guild_id: int, user_id: int) -> datetime | None:
    res = (get_sb().table("activity")
           .select("last_seen")
           .eq("guild_id", str(guild_id))
           .eq("user_id",  str(user_id))
           .execute())
    if res.data:
        return datetime.fromisoformat(res.data[0]["last_seen"])
    return None


def get_all_activity(guild_id: int) -> dict:
    res = (get_sb().table("activity")
           .select("user_id,last_seen")
           .eq("guild_id", str(guild_id))
           .execute())
    return {row["user_id"]: datetime.fromisoformat(row["last_seen"]) for row in res.data}


def delete_user_activity(guild_id: int, user_id: int) -> None:
    (get_sb().table("activity")
     .delete()
     .eq("guild_id", str(guild_id))
     .eq("user_id",  str(user_id))
     .execute())
# ─────────────────────────────────────────────────────────────────────────────


# ─── BOT SETUP ───────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
# ─────────────────────────────────────────────────────────────────────────────


# ─── HELPERS ─────────────────────────────────────────────────────────────────
def is_exempt(member: discord.Member, exempt_role_ids: list) -> bool:
    if member.bot:
        return True
    if member.guild_permissions.administrator:
        return True
    member_role_ids = {r.id for r in member.roles}
    return bool(member_role_ids & set(int(r) for r in exempt_role_ids))


async def get_inactive_members(guild: discord.Guild) -> list:
    settings = get_guild_settings(guild.id)
    threshold_days = settings["inactivity_days"]
    exempt_roles   = settings.get("exempt_roles") or []
    cutoff         = utcnow() - timedelta(days=threshold_days)
    tracking_since = datetime.fromisoformat(settings["tracking_since"])

    activity = get_all_activity(guild.id)
    inactive = []

    for member in guild.members:
        if is_exempt(member, exempt_roles):
            continue
        last_seen = activity.get(str(member.id))
        if not last_seen:
            join = member.joined_at or tracking_since
            last_seen = max(join, tracking_since)
        if last_seen < cutoff:
            inactive.append((member, last_seen))

    inactive.sort(key=lambda x: x[1])
    return inactive


async def send_log(guild: discord.Guild, message: str) -> None:
    settings = get_guild_settings(guild.id)
    if settings.get("log_channel"):
        ch = guild.get_channel(int(settings["log_channel"]))
        if ch:
            try:
                await ch.send(message)
            except discord.Forbidden:
                pass
# ─────────────────────────────────────────────────────────────────────────────


# ─── EVENTS ──────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"[InactivityGuard] Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"[InactivityGuard] Guilds visible: {len(bot.guilds)}")
    for guild in bot.guilds:
        try:
            try:
                await asyncio.wait_for(guild.chunk(), timeout=30)
            except asyncio.TimeoutError:
                print(f"  • {guild.name} — chunk timed out, continuing anyway")
            activity = get_all_activity(guild.id)
            seeded = 0
            for member in guild.members:
                if member.bot:
                    continue
                if str(member.id) not in activity and member.joined_at:
                    set_last_seen(guild.id, member.id, member.joined_at)
                    seeded += 1
            settings = get_guild_settings(guild.id)
            print(f"  • {guild.name} — seeded {seeded} members, threshold: {settings['inactivity_days']}d")
        except Exception as e:
            print(f"  • ERROR in {guild.name}: {e}")
    try:
        synced = await bot.tree.sync()
        print(f"[InactivityGuard] Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"[InactivityGuard] Sync error: {e}")
    auto_kick_check.start()


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    set_last_seen(message.guild.id, message.author.id)
    await bot.process_commands(message)


@bot.event
async def on_voice_state_update(member: discord.Member, before, after):
    if member.bot:
        return
    if after.channel is not None:
        set_last_seen(member.guild.id, member.id)


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        return
    set_last_seen(member.guild.id, member.id)


@bot.event
async def on_member_remove(member: discord.Member):
    if member.guild:
        delete_user_activity(member.guild.id, member.id)
# ─────────────────────────────────────────────────────────────────────────────


# ─── BACKGROUND TASK ─────────────────────────────────────────────────────────
@tasks.loop(hours=AUTO_CHECK_INTERVAL_HOURS)
async def auto_kick_check():
    await bot.wait_until_ready()
    for guild in bot.guilds:
        settings = get_guild_settings(guild.id)
        if not settings.get("log_channel"):
            continue
        inactive = await get_inactive_members(guild)
        if not inactive:
            continue
        kicked, failed = 0, 0
        for member, last_seen in inactive:
            days_ago = (utcnow() - last_seen).days
            try:
                await member.kick(reason=f"[InactivityGuard] Inactive for {days_ago} days.")
                kicked += 1
                await send_log(guild,
                    f"👢 Auto-kicked **{member}** — last seen **{days_ago}d ago** ({last_seen.strftime('%Y-%m-%d')})")
            except discord.Forbidden:
                failed += 1
            except Exception as e:
                failed += 1
                print(f"Auto-kick error ({member}): {e}")
        await send_log(guild,
            f"✅ Auto-check complete — kicked **{kicked}**" + (f", failed **{failed}**" if failed else "."))
# ─────────────────────────────────────────────────────────────────────────────


# ─── ERROR HANDLER ───────────────────────────────────────────────────────────
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You need **Administrator** permission to use this command.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ Error: {error}", ephemeral=True)
        raise error
# ─────────────────────────────────────────────────────────────────────────────


# ─── SLASH COMMANDS ──────────────────────────────────────────────────────────

@bot.tree.command(name="setup", description="Configure InactivityGuard for this server.")
@app_commands.describe(
    inactivity_days="Days of inactivity before a member is eligible for kicking.",
    log_channel="Channel where kick logs are posted (also enables auto-kicking).",
)
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction,
                inactivity_days: int = DEFAULT_INACTIVITY_DAYS,
                log_channel: discord.TextChannel = None):
    update_guild_settings(
        interaction.guild_id,
        inactivity_days=max(1, inactivity_days),
        log_channel=str(log_channel.id) if log_channel else None,
    )
    settings = get_guild_settings(interaction.guild_id)
    lines = [
        "✅ **InactivityGuard configured!**",
        f"• Inactivity threshold: **{settings['inactivity_days']} days**",
        f"• Log channel: {log_channel.mention if log_channel else '*(none — auto-kick disabled)*'}",
        "",
        "Use `/exempt_role` to protect roles from being kicked.",
        "Use `/check_inactive` to preview who would be kicked.",
        "Use `/kick_inactive` to manually kick inactive members.",
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="set_threshold", description="Change the inactivity kick threshold.")
@app_commands.describe(days="Kick members inactive for this many days.")
@app_commands.checks.has_permissions(administrator=True)
async def set_threshold(interaction: discord.Interaction, days: int):
    if days < 1:
        await interaction.response.send_message("❌ Must be at least 1 day.", ephemeral=True)
        return
    update_guild_settings(interaction.guild_id, inactivity_days=days)
    await interaction.response.send_message(f"✅ Threshold updated to **{days} days**.", ephemeral=True)


@bot.tree.command(name="set_log_channel", description="Set or clear the log channel.")
@app_commands.describe(channel="Text channel for logs. Leave empty to disable auto-kicking.")
@app_commands.checks.has_permissions(administrator=True)
async def set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel = None):
    update_guild_settings(interaction.guild_id, log_channel=str(channel.id) if channel else None)
    if channel:
        await interaction.response.send_message(
            f"✅ Log channel set to {channel.mention}. Auto-kicking **enabled**.", ephemeral=True)
    else:
        await interaction.response.send_message(
            "✅ Log channel cleared. Auto-kicking **disabled**.", ephemeral=True)


@bot.tree.command(name="exempt_role", description="Add or remove a role from the kick-exempt list.")
@app_commands.describe(role="The role to toggle.", action="Add or remove the exemption.")
@app_commands.choices(action=[
    app_commands.Choice(name="add", value="add"),
    app_commands.Choice(name="remove", value="remove"),
])
@app_commands.checks.has_permissions(administrator=True)
async def exempt_role(interaction: discord.Interaction, role: discord.Role, action: str):
    settings = get_guild_settings(interaction.guild_id)
    exempt = list(settings.get("exempt_roles") or [])
    rid = str(role.id)
    if action == "add":
        if rid not in exempt:
            exempt.append(rid)
        msg = f"✅ {role.mention} is now **exempt** from inactivity kicks."
    else:
        exempt = [r for r in exempt if r != rid]
        msg = f"✅ {role.mention} is **no longer exempt**."
    update_guild_settings(interaction.guild_id, exempt_roles=exempt)
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="status", description="Show InactivityGuard's current configuration.")
async def status(interaction: discord.Interaction):
    settings = get_guild_settings(interaction.guild_id)
    guild = interaction.guild
    log_ch = guild.get_channel(int(settings["log_channel"])) if settings.get("log_channel") else None
    exempt_mentions = []
    for rid in (settings.get("exempt_roles") or []):
        r = guild.get_role(int(rid))
        if r:
            exempt_mentions.append(r.mention)
    activity = get_all_activity(interaction.guild_id)
    lines = [
        "📊 **InactivityGuard Status**",
        f"• Threshold: **{settings['inactivity_days']} days**",
        f"• Log channel: {log_ch.mention if log_ch else '*(none)*'}",
        f"• Auto-kick: {'✅ enabled' if log_ch else '❌ disabled'}",
        f"• Exempt roles: {', '.join(exempt_mentions) if exempt_mentions else '*(none)*'}",
        f"• Members tracked: **{len(activity)}**",
        f"• Tracking since: `{settings['tracking_since'][:10]}`",
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="check_inactive", description="Preview who would be kicked right now (no action taken).")
@app_commands.describe(days="Override threshold for this check only (optional).")
@app_commands.checks.has_permissions(administrator=True)
async def check_inactive(interaction: discord.Interaction, days: int = None):
    await interaction.response.defer(ephemeral=True)
    settings = get_guild_settings(interaction.guild_id)
    override = days or settings["inactivity_days"]
    original = settings["inactivity_days"]
    update_guild_settings(interaction.guild_id, inactivity_days=override)
    inactive = await get_inactive_members(interaction.guild)
    update_guild_settings(interaction.guild_id, inactivity_days=original)
    if not inactive:
        await interaction.followup.send(
            f"✅ No members inactive for **{override}+ days**. Server is clean!", ephemeral=True)
        return
    lines = [f"⚠️ **{len(inactive)} member(s) inactive for {override}+ days:**\n"]
    for member, last_seen in inactive[:25]:
        days_ago = (utcnow() - last_seen).days
        lines.append(f"• {member.mention} — last seen **{days_ago}d ago** (`{last_seen.strftime('%Y-%m-%d')}`)")
    if len(inactive) > 25:
        lines.append(f"…and **{len(inactive) - 25}** more.")
    lines.append("\nUse `/kick_inactive` to kick them.")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@bot.tree.command(name="kick_inactive", description="Kick all members inactive past the threshold.")
@app_commands.describe(
    days="Override threshold for this kick only (optional).",
    dry_run="Show what would happen without actually kicking anyone.",
)
@app_commands.checks.has_permissions(administrator=True)
async def kick_inactive(interaction: discord.Interaction, days: int = None, dry_run: bool = False):
    await interaction.response.defer(ephemeral=True)
    settings = get_guild_settings(interaction.guild_id)
    override = days or settings["inactivity_days"]
    original = settings["inactivity_days"]
    update_guild_settings(interaction.guild_id, inactivity_days=override)
    inactive = await get_inactive_members(interaction.guild)
    update_guild_settings(interaction.guild_id, inactivity_days=original)
    if not inactive:
        await interaction.followup.send(
            f"✅ No members inactive for **{override}+ days**. Nothing to do!", ephemeral=True)
        return
    if dry_run:
        lines = [f"🔍 **Dry run — {len(inactive)} would be kicked ({override}+ days):**\n"]
        for member, last_seen in inactive[:25]:
            days_ago = (utcnow() - last_seen).days
            lines.append(f"• {member.mention} — {days_ago}d ago")
        if len(inactive) > 25:
            lines.append(f"…and **{len(inactive) - 25}** more.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
        return
    kicked, failed, fail_list = 0, 0, []
    for member, last_seen in inactive:
        days_ago = (utcnow() - last_seen).days
        try:
            await member.kick(reason=f"[InactivityGuard] Inactive for {days_ago} days.")
            kicked += 1
            await send_log(interaction.guild,
                f"👢 Kicked **{member}** — last seen **{days_ago}d ago** "
                f"({last_seen.strftime('%Y-%m-%d')}) — by {interaction.user.mention}")
        except discord.Forbidden:
            failed += 1
            fail_list.append(str(member))
        except Exception as e:
            failed += 1
            fail_list.append(f"{member} ({e})")
        await asyncio.sleep(0.5)
    lines = [f"✅ Kicked **{kicked}** member(s) inactive for **{override}+ days**."]
    if failed:
        lines.append(f"⚠️ Failed to kick **{failed}**: {', '.join(fail_list[:5])}")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@bot.tree.command(name="last_seen", description="Check when a specific member was last active.")
@app_commands.describe(member="The member to look up.")
@app_commands.checks.has_permissions(administrator=True)
async def last_seen_cmd(interaction: discord.Interaction, member: discord.Member):
    dt = get_last_seen(interaction.guild_id, member.id)
    if dt:
        days_ago = (utcnow() - dt).days
        await interaction.response.send_message(
            f"🕒 **{member}** was last seen **{days_ago}d ago** (`{dt.strftime('%Y-%m-%d %H:%M UTC')}`).",
            ephemeral=True)
    elif member.joined_at:
        dt = member.joined_at
        days_ago = (utcnow() - dt).days
        set_last_seen(interaction.guild_id, member.id, dt)
        await interaction.response.send_message(
            f"🕒 **{member}** — no tracked activity yet. "
            f"Joined **{days_ago}d ago** (`{dt.strftime('%Y-%m-%d')}`) — using join date as baseline.",
            ephemeral=True)
    else:
        await interaction.response.send_message(f"❓ No data available for **{member}**.", ephemeral=True)


@bot.tree.command(name="reset_activity", description="Manually reset a member's last-seen timestamp to now.")
@app_commands.describe(member="The member whose activity to reset.")
@app_commands.checks.has_permissions(administrator=True)
async def reset_activity(interaction: discord.Interaction, member: discord.Member):
    set_last_seen(interaction.guild_id, member.id)
    await interaction.response.send_message(f"✅ Reset **{member}**'s last-seen to now.", ephemeral=True)


@bot.tree.command(name="help_guard", description="Show all InactivityGuard commands.")
async def help_guard(interaction: discord.Interaction):
    lines = [
        "🛡️ **InactivityGuard — Command Reference**\n",
        "**Setup**",
        "`/setup [inactivity_days] [log_channel]` — Initial configuration.",
        "`/set_threshold <days>` — Change the inactivity threshold.",
        "`/set_log_channel [channel]` — Set or clear the log channel.",
        "`/exempt_role <role> <add|remove>` — Protect a role from kicks.",
        "",
        "**Inspection**",
        "`/status` — Show current configuration.",
        "`/check_inactive [days]` — Preview who would be kicked.",
        "`/last_seen <member>` — Check a member's last activity.",
        "",
        "**Action**",
        "`/kick_inactive [days] [dry_run]` — Kick inactive members now.",
        "`/reset_activity <member>` — Mark a member as active right now.",
        "",
        "**Activity tracking**",
        "• Sending any message → resets timer",
        "• Joining / switching voice channel → resets timer",
        f"• Auto-check runs every **{AUTO_CHECK_INTERVAL_HOURS}h** when a log channel is set.",
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)
# ─────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("⚠️  Set SUPABASE_URL and SUPABASE_KEY env vars before running.")
    elif TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("⚠️  Set DISCORD_TOKEN env var before running.")
    else:
        bot.run(TOKEN)
