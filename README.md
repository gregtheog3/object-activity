# InactivityGuard — Discord Inactivity Kick Bot

Tracks real **server-side** activity (messages + voice joins) and kicks members who go quiet for too long.

---

## Quick Start

### 1. Install dependencies
```
pip install discord.py
```

### 2. Create your bot
1. Go to https://discord.com/developers/applications → **New Application**
2. Click **Bot** → **Add Bot**
3. Under **Privileged Gateway Intents**, enable:
   - ✅ **Server Members Intent**
   - ✅ **Message Content Intent**
4. Copy your **Token** and paste it into `bot.py`:
   ```python
   TOKEN = "your-token-here"
   ```
   or set the environment variable `DISCORD_TOKEN`.

### 3. Invite the bot to your server
Use this URL template (replace CLIENT_ID):
```
https://discord.com/api/oauth2/authorize?client_id=CLIENT_ID&permissions=268435462&scope=bot+applications.commands
```
Permissions included: **Kick Members**, **View Channels**, **Read Message History**.

### 4. Run the bot
```
python bot.py
```

Wait a few seconds for slash commands to sync globally (can take up to 1 hour to show in Discord).

### 5. Configure in your server
```
/setup inactivity_days:30 log_channel:#mod-logs
```

---

## All Commands

| Command | Description |
|---|---|
| `/setup` | Initial config — set threshold + log channel |
| `/set_threshold <days>` | Change the inactivity period |
| `/set_log_channel [#channel]` | Set/clear log channel (enables auto-kick) |
| `/exempt_role <role> add\|remove` | Protect a role from ever being kicked |
| `/status` | View current configuration |
| `/check_inactive [days]` | Preview who would be kicked (no action taken) |
| `/kick_inactive [days] [dry_run]` | Manually kick inactive members |
| `/last_seen <member>` | See when a member was last active |
| `/reset_activity <member>` | Mark a member as active right now |
| `/help_guard` | Show all commands |

---

## What counts as "activity"?
- ✉️ Sending a message in **any** channel the bot can see
- 🔊 **Joining** or **switching** voice channels
- 🙋 A member **joining** the server (join date is their starting timestamp)

## Auto-kick
Once a **log channel** is set, the bot automatically runs the inactivity check every **24 hours** and kicks eligible members, posting a log entry for each.

## Notes
- Admins and bots are **always** exempt
- Activity data is stored in `activity_data.json` (same folder as the bot)
- The bot seeds join dates on startup for members it hasn't seen yet
