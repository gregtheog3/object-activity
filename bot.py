"""
InactivityGuard - Discord Bot
Tracks per-user activity (messages + voice joins) and kicks after a configurable idle period.
Uses slash commands (/) and stores state in activity_data.json.

Requirements:
    pip install discord.py

Setup:
    1. Create a bot at https://discord.com/developers/applications
    2. Enable SERVER MEMBERS INTENT and MESSAGE CONTENT INTENT under Bot → Privileged Gateway Intents
    3. Invite the bot with scopes: bot + applications.commands
       and permissions: Kick Members, View Channels, Read Message History
    4. Paste your bot token below (or set the DISCORD_TOKEN env var)
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks
import json
import os
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─── CONFIG ──────────────────────────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_TOKEN", "MTQ4MTczNjk3NjQzNTc3NzY2Ng.GJeawC.26mzqO6r4fj_i7zWx8AzNcTDPvUO-cI2N-Y8Js")
DATA_FILE = Path("activity_data.json")

# Default inactivity threshold (days) – changeable per-guild via /setup
DEFAULT_INACTIVITY_DAYS = 30

# How often (in hours) the bot automatically checks for inactive members
AUTO_CHECK_INTERVAL_HOURS = 24
# ─────────────────────────────────────────────────────────────────────────────


# ─── PERSISTENCE ─────────────────────────────────────────────────────────────
def load_data() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}


def save_data(data: dict) -> None:
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


# Schema:
# {
#   "<guild_id>": {
#     "inactivity_days": 30,
#     "log_channel": null,          # channel id or null
#     "exempt_roles": [],           # list of role ids never kicked
#     "tracking_since": "ISO",      # when we started tracking this guild
#     "users": {
#       "<user_id>": "ISO datetime" # last_seen timestamp
#     }
#   }
# }

def guild_data(data: dict, guild_id: int) -> dict:
    key = str(guild_id)
    if key not in data:
        data[key] = {
            "inactivity_days": DEFAULT_INACTIVITY_DAYS,
            "log_channel": None,
            "exempt_roles": [],
            "tracking_since": utcnow_iso(),
            "users": {},
        }
    return data[key]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso_to_dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)
# ─────────────────────────────────────────────────────────────────────────────


# ─── BOT SETUP ───────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True          # needed to iterate members & catch joins
intents.message_content = True  # needed to read messages for activity

bot = commands.Bot(command_prefix="!", intents=intents)
data: dict = {}
# ─────────────────────────────────────────────────────────────────────────────


# ─── HELPERS ─────────────────────────────────────────────────────────────────
def update_user_activity(guild_id: int, user_id: int) -> None:
    gd = guild_data(data, guild_id)
    gd["users"][str(user_id)] = utcnow_iso()
    save_data(data)


def is_exempt(member: discord.Member, exempt_role_ids: list) -> bool:
    """Returns True if the member should never be kicked."""
    if member.bot:
        return True
    if member.guild_permissions.administrator:
        return True
    member_role_ids = {r.id for r in member.roles}
    return bool(member_role_ids & set(exempt_role_ids))


async def get_inactive_members(guild: discord.Guild) -> list[tuple[discord.Member, datetime]]:
    """Returns list of (member, last_seen_dt) for members past the threshold."""
    gd = guild_data(data, guild.id)
    threshold_days = gd["inactivity_days"]
    exempt_roles = gd["exempt_roles"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=threshold_days)
    tracking_since = iso_to_dt(gd["tracking_since"])

    inactive = []
    for member in guild.members:
        if is_exempt(member, exempt_roles):
            continue

        last_seen_iso = gd["users"].get(str(member.id))
        if last_seen_iso:
            last_seen = iso_to_dt(last_seen_iso)
        else:
            # Never seen — use whichever is later: join date or tracking start
            join = member.joined_at or tracking_since
            last_seen = max(join, tracking_since)

        if last_seen < cutoff:
            inactive.append((member, last_seen))

    inactive.sort(key=lambda x: x[1])  # oldest first
    return inactive


async def send_log(guild: discord.Guild, message: str) -> None:
    gd = guild_data(data, guild.id)
    if gd["log_channel"]:
        ch = guild.get_channel(int(gd["log_channel"]))
        if ch:
            try:
                await ch.send(message)
            except discord.Forbidden:
                pass
# ─────────────────────────────────────────────────────────────────────────────


# ─── EVENTS ──────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    global data
    data = load_data()
    print(f"[InactivityGuard] Logged in as {bot.user} (ID: {bot.user.id})")

    # Seed join dates for members we've never seen
    for guild in bot.guilds:
        gd = guild_data(data, guild.id)
        changed = False
        for member in guild.members:
            if member.bot:
                continue
            uid = str(member.id)
            if uid not in gd["users"] and member.joined_at:
                gd["users"][uid] = member.joined_at.isoformat()
                changed = True
        if changed:
            save_data(data)
        print(f"  • {guild.name} — tracking {len(gd['users'])} members, "
              f"threshold: {gd['inactivity_days']}d")

    try:
        synced = await bot.tree.sync()
        print(f"[InactivityGuard] Synced {len(synced)} slash commands globally.")
    except Exception as e:
        print(f"[InactivityGuard] Sync error: {e}")

    auto_kick_check.start()


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    update_user_activity(message.guild.id, message.author.id)
    await bot.process_commands(message)


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Fires whenever someone's voice state changes. Joining/switching counts as activity."""
    if member.bot:
        return
    if after.channel is not None:  # they are now in a channel (joined or moved)
        update_user_activity(member.guild.id, member.id)


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        return
    update_user_activity(member.guild.id, member.id)


@bot.event
async def on_member_remove(member: discord.Member):
    """Clean up data when someone leaves."""
    if member.guild:
        gd = guild_data(data, member.guild.id)
        gd["users"].pop(str(member.id), None)
        save_data(data)
# ─────────────────────────────────────────────────────────────────────────────


# ─── BACKGROUND TASK ─────────────────────────────────────────────────────────
@tasks.loop(hours=AUTO_CHECK_INTERVAL_HOURS)
async def auto_kick_check():
    """Runs every AUTO_CHECK_INTERVAL_HOURS and kicks inactive members in all guilds."""
    await bot.wait_until_ready()
    for guild in bot.guilds:
        gd = guild_data(data, guild.id)
        if not gd.get("log_channel"):
            continue  # don't auto-kick unless a log channel is configured

        inactive = await get_inactive_members(guild)
        if not inactive:
            continue

        kicked, failed = 0, 0
        for member, last_seen in inactive:
            days_ago = (datetime.now(timezone.utc) - last_seen).days
            try:
                await member.kick(reason=f"[InactivityGuard] Inactive for {days_ago} days.")
                kicked += 1
                await send_log(guild,
                    f"👢 Auto-kicked **{member}** — last seen **{days_ago}d ago** "
                    f"({last_seen.strftime('%Y-%m-%d')})")
            except discord.Forbidden:
                failed += 1
            except Exception as e:
                failed += 1
                print(f"Auto-kick error ({member}): {e}")

        await send_log(guild,
            f"✅ Auto-check complete — kicked **{kicked}** member(s)"
            + (f", failed to kick **{failed}** (permissions?)" if failed else "."))
# ─────────────────────────────────────────────────────────────────────────────


# ─── PERMISSION CHECK ────────────────────────────────────────────────────────
# Use the built-in decorator — applied per-command as:
#   @app_commands.checks.has_permissions(administrator=True)
# Error responses are handled by the global error handler below.

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You need **Administrator** permission to use this command.", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"❌ An error occurred: {error}", ephemeral=True)
        raise error
# ─────────────────────────────────────────────────────────────────────────────


# ─── SLASH COMMANDS ───────────────────────────────────────────────────────────

# /setup
@bot.tree.command(name="setup", description="Configure InactivityGuard for this server.")
@app_commands.describe(
    inactivity_days="Days of inactivity before a member is eligible for kicking (default 30).",
    log_channel="Channel where kick logs are posted (also enables auto-kicking).",
)
@app_commands.checks.has_permissions(administrator=True)
async def setup(
    interaction: discord.Interaction,
    inactivity_days: int = DEFAULT_INACTIVITY_DAYS,
    log_channel: discord.TextChannel = None,
):
    gd = guild_data(data, interaction.guild_id)
    gd["inactivity_days"] = max(1, inactivity_days)
    if log_channel:
        gd["log_channel"] = log_channel.id
    save_data(data)

    lines = [
        "✅ **InactivityGuard configured!**",
        f"• Inactivity threshold: **{gd['inactivity_days']} days**",
        f"• Log channel: {log_channel.mention if log_channel else '*(none — auto-kick disabled)*'}",
        f"• Tracking since: `{gd['tracking_since'][:10]}`",
        "",
        "Use `/exempt_role` to protect roles from being kicked.",
        "Use `/check_inactive` to preview who would be kicked.",
        "Use `/kick_inactive` to manually kick inactive members.",
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# /set_threshold
@bot.tree.command(name="set_threshold", description="Change the inactivity kick threshold.")
@app_commands.describe(days="Kick members inactive for this many days.")
@app_commands.checks.has_permissions(administrator=True)
async def set_threshold(interaction: discord.Interaction, days: int):
    if days < 1:
        await interaction.response.send_message("❌ Must be at least 1 day.", ephemeral=True)
        return
    gd = guild_data(data, interaction.guild_id)
    gd["inactivity_days"] = days
    save_data(data)
    await interaction.response.send_message(
        f"✅ Inactivity threshold updated to **{days} days**.", ephemeral=True)


# /set_log_channel
@bot.tree.command(name="set_log_channel", description="Set (or clear) the log channel. Setting one enables auto-kicking.")
@app_commands.describe(channel="Text channel for logs. Leave empty to disable auto-kicking.")
@app_commands.checks.has_permissions(administrator=True)
async def set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel = None):
    gd = guild_data(data, interaction.guild_id)
    gd["log_channel"] = channel.id if channel else None
    save_data(data)
    if channel:
        await interaction.response.send_message(
            f"✅ Log channel set to {channel.mention}. Auto-kicking is **enabled**.", ephemeral=True)
    else:
        await interaction.response.send_message(
            "✅ Log channel cleared. Auto-kicking is **disabled**.", ephemeral=True)


# /exempt_role
@bot.tree.command(name="exempt_role", description="Add or remove a role from the kick-exempt list.")
@app_commands.describe(role="The role to toggle.", action="Add or remove the exemption.")
@app_commands.choices(action=[
    app_commands.Choice(name="add", value="add"),
    app_commands.Choice(name="remove", value="remove"),
])
@app_commands.checks.has_permissions(administrator=True)
async def exempt_role(interaction: discord.Interaction, role: discord.Role, action: str):
    gd = guild_data(data, interaction.guild_id)
    exempt = gd["exempt_roles"]
    rid = role.id
    if action == "add":
        if rid not in exempt:
            exempt.append(rid)
        msg = f"✅ {role.mention} is now **exempt** from inactivity kicks."
    else:
        exempt[:] = [r for r in exempt if r != rid]
        msg = f"✅ {role.mention} is **no longer exempt**."
    save_data(data)
    await interaction.response.send_message(msg, ephemeral=True)


# /status
@bot.tree.command(name="status", description="Show InactivityGuard's current configuration.")
async def status(interaction: discord.Interaction):
    gd = guild_data(data, interaction.guild_id)
    guild = interaction.guild

    log_ch = guild.get_channel(int(gd["log_channel"])) if gd["log_channel"] else None
    exempt_mentions = []
    for rid in gd["exempt_roles"]:
        r = guild.get_role(rid)
        if r:
            exempt_mentions.append(r.mention)

    lines = [
        "📊 **InactivityGuard Status**",
        f"• Threshold: **{gd['inactivity_days']} days**",
        f"• Log channel: {log_ch.mention if log_ch else '*(none)*'}",
        f"• Auto-kick: {'✅ enabled' if log_ch else '❌ disabled'}",
        f"• Exempt roles: {', '.join(exempt_mentions) if exempt_mentions else '*(none)*'}",
        f"• Members tracked: **{len(gd['users'])}**",
        f"• Tracking since: `{gd['tracking_since'][:10]}`",
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# /check_inactive
@bot.tree.command(name="check_inactive", description="Preview who would be kicked right now (no one is actually kicked).")
@app_commands.describe(days="Override threshold for this check only (optional).")
@app_commands.checks.has_permissions(administrator=True)
async def check_inactive(interaction: discord.Interaction, days: int = None):
    await interaction.response.defer(ephemeral=True)

    gd = guild_data(data, interaction.guild_id)
    override = days or gd["inactivity_days"]

    # Temporarily override for the check
    original = gd["inactivity_days"]
    gd["inactivity_days"] = override
    inactive = await get_inactive_members(interaction.guild)
    gd["inactivity_days"] = original

    if not inactive:
        await interaction.followup.send(
            f"✅ No members inactive for **{override}+ days**. Server is clean!", ephemeral=True)
        return

    lines = [f"⚠️ **{len(inactive)} member(s) inactive for {override}+ days:**\n"]
    for member, last_seen in inactive[:25]:   # cap at 25 for readability
        days_ago = (datetime.now(timezone.utc) - last_seen).days
        lines.append(f"• {member.mention} — last seen **{days_ago}d ago** (`{last_seen.strftime('%Y-%m-%d')}`)")
    if len(inactive) > 25:
        lines.append(f"…and **{len(inactive) - 25}** more.")
    lines.append("\nUse `/kick_inactive` to kick them.")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


# /kick_inactive
@bot.tree.command(name="kick_inactive", description="Kick all members who have been inactive past the threshold.")
@app_commands.describe(
    days="Override threshold for this kick only (optional).",
    dry_run="If True, show what would happen without actually kicking anyone.",
)
@app_commands.checks.has_permissions(administrator=True)
async def kick_inactive(
    interaction: discord.Interaction,
    days: int = None,
    dry_run: bool = False,
):
    await interaction.response.defer(ephemeral=True)

    gd = guild_data(data, interaction.guild_id)
    override = days or gd["inactivity_days"]

    original = gd["inactivity_days"]
    gd["inactivity_days"] = override
    inactive = await get_inactive_members(interaction.guild)
    gd["inactivity_days"] = original

    if not inactive:
        await interaction.followup.send(
            f"✅ No members inactive for **{override}+ days**. Nothing to do!", ephemeral=True)
        return

    if dry_run:
        lines = [f"🔍 **Dry run — {len(inactive)} would be kicked ({override}+ days inactive):**\n"]
        for member, last_seen in inactive[:25]:
            days_ago = (datetime.now(timezone.utc) - last_seen).days
            lines.append(f"• {member.mention} — {days_ago}d ago")
        if len(inactive) > 25:
            lines.append(f"…and **{len(inactive) - 25}** more.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
        return

    kicked, failed = 0, 0
    fail_list = []
    for member, last_seen in inactive:
        days_ago = (datetime.now(timezone.utc) - last_seen).days
        try:
            await member.kick(reason=f"[InactivityGuard] Inactive for {days_ago} days.")
            kicked += 1
            await send_log(interaction.guild,
                f"👢 Kicked **{member}** — last seen **{days_ago}d ago** "
                f"({last_seen.strftime('%Y-%m-%d')}) — kicked by {interaction.user.mention}")
        except discord.Forbidden:
            failed += 1
            fail_list.append(str(member))
        except Exception as e:
            failed += 1
            fail_list.append(f"{member} ({e})")
        await asyncio.sleep(0.5)  # rate-limit safety

    lines = [f"✅ Kicked **{kicked}** member(s) inactive for **{override}+ days**."]
    if failed:
        lines.append(f"⚠️ Failed to kick **{failed}**: {', '.join(fail_list[:5])}")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


# /last_seen
@bot.tree.command(name="last_seen", description="Check when a specific member was last active.")
@app_commands.describe(member="The member to look up.")
@app_commands.checks.has_permissions(administrator=True)
async def last_seen_cmd(interaction: discord.Interaction, member: discord.Member):
    gd = guild_data(data, interaction.guild_id)
    iso = gd["users"].get(str(member.id))
    if iso:
        dt = iso_to_dt(iso)
        days_ago = (datetime.now(timezone.utc) - dt).days
        await interaction.response.send_message(
            f"🕒 **{member}** was last seen **{days_ago}d ago** (`{dt.strftime('%Y-%m-%d %H:%M UTC')}`).",
            ephemeral=True)
    else:
        await interaction.response.send_message(
            f"❓ No activity recorded for **{member}** since tracking began.", ephemeral=True)


# /reset_activity
@bot.tree.command(name="reset_activity", description="Manually reset a member's last-seen timestamp to now.")
@app_commands.describe(member="The member whose activity to reset.")
@app_commands.checks.has_permissions(administrator=True)
async def reset_activity(interaction: discord.Interaction, member: discord.Member):
    update_user_activity(interaction.guild_id, member.id)
    await interaction.response.send_message(
        f"✅ Reset **{member}**'s last-seen to now.", ephemeral=True)


# /help_guard
@bot.tree.command(name="help_guard", description="Show all InactivityGuard commands.")
async def help_guard(interaction: discord.Interaction):
    lines = [
        "🛡️ **InactivityGuard — Command Reference**\n",
        "**Setup**",
        "`/setup [inactivity_days] [log_channel]` — Initial configuration.",
        "`/set_threshold <days>` — Change the inactivity threshold.",
        "`/set_log_channel [channel]` — Set or clear the log channel (enables auto-kick).",
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
        "• Bot joins / member joins → recorded",
        f"• Auto-check runs every **{AUTO_CHECK_INTERVAL_HOURS}h** when a log channel is set.",
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)
# ─────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    if TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("⚠️  Set your bot token in the TOKEN variable or DISCORD_TOKEN env var before running.")
    else:
        bot.run(TOKEN)
