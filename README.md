# Valorant Discord Bot 🎮

A Discord bot that tracks when users are playing or streaming Valorant and automatically reports their **Competitive match** results. **Supports grouped announcements** - when multiple squad members finish a game together, their results are combined into a single announcement!

## Features

- **Competitive Only**: Only tracks and announces Competitive matches (ignores Unrated, Spike Rush, etc.)
- **Server Isolation**: Registrations are per-server - users in Server A won't see results in Server B
- **Auto-detection**: Detects when registered users start/stop playing Valorant
- **Streaming detection**: Notes when users are streaming Valorant
- **Squad announcements**: Combines results for players in the same voice channel playing together
- **Match verification**: Confirms players were in the same match via match ID before grouping
- **Rich embeds**: Beautiful Discord embeds showing K/D/A, map, agent, score, etc.
- **Slash commands**: Modern Discord slash command interface
- **Multi-server support**: Works across multiple Discord servers with complete isolation

## Example Announcement

When 3 squad members finish a match together:

```
🎮 Squad Match Complete! (3 players)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Map: Haven | Mode: Competitive | Score: 🔴 13 - 11 🔵

Player Stats
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@Player1
🏆 🔴 Jett | K/D/A: 24/15/6 (KDA: 2.00)

@Player2  
🏆 🔴 Sage | K/D/A: 18/12/14 (KDA: 2.67)

@Player3
🏆 🔴 Omen | K/D/A: 15/14/8 (KDA: 1.64)
```

## Commands

| Command | Description |
|---------|-------------|
| `/register <riot_name> <riot_tag> [region]` | Register your Riot ID for tracking |
| `/unregister` | Stop tracking your games |
| `/stats` | View your recent Valorant stats |
| `/lastmatch` | Get details about your last match |
| `/setchannel <channel>` | Set where match announcements are posted (Admin) |

## Setup

### 1. Create a Discord Bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Click "New Application" and give it a name
3. Go to the "Bot" section and click "Add Bot"
4. Enable these **Privileged Gateway Intents**:
   - ✅ Presence Intent (to detect game activity)
   - ✅ Server Members Intent (to see member updates)
   - ✅ Message Content Intent (optional, for prefix commands)
5. Copy the bot token

### 2. Invite the Bot to Your Server

1. Go to "OAuth2" → "URL Generator"
2. Select scopes: `bot`, `applications.commands`
3. Select permissions:
   - Send Messages
   - Embed Links
   - Read Message History
   - Use Slash Commands
4. Copy the generated URL and open it to invite the bot

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Set Environment Variables

```bash
# Required
export DISCORD_BOT_TOKEN="your_discord_bot_token"

# Optional - for higher API rate limits
export VALORANT_API_KEY="your_henrikdev_api_key"
```

### 5. Run the Bot

```bash
python bot.py
```

## API Information

This bot uses [Henrik's Valorant API](https://docs.henrikdev.xyz/) which is free to use. 

**Rate Limits (without API key):**
- 30 requests per minute

**Get an API Key (optional, for higher limits):**
1. Join the [Henrik Dev Discord](https://discord.gg/X3GaVkX2YN)
2. Request an API key in the appropriate channel

## How It Works

1. Users register their Riot ID with `/register`
2. Bot monitors presence updates for all registered users
3. When a user starts playing Valorant, the bot notes the time and their voice channel
4. When they stop playing, the bot waits 30 seconds for other squad members to also finish
5. Bot then waits ~60 seconds for the match to be recorded in the API
6. Bot fetches match data and **verifies players were in the same match** via match ID
7. Creates a single combined announcement for all squad members

### Squad Detection Logic

- Players are grouped by **voice channel** - if you're in the same VC, you're likely playing together
- The bot **verifies match IDs** - even if players are in the same VC, they'll only be grouped if they were actually in the same match
- Players not in voice chat get individual announcements
- If some VC members were in different matches (e.g., one was spectating), separate announcements are created

### Server Isolation

- **Registrations are per-server**: If you're in multiple Discord servers with this bot, you need to `/register` in each one
- **Results stay in their server**: A match played while registered in Server A will only be announced in Server A
- **No cross-server leakage**: Even if you're registered in multiple servers, your results won't appear in servers where you didn't register
- **Separate settings**: Each server has its own announcement channel and settings

## Configuration

Edit these values in `bot.py` if needed:

```python
# Only track competitive matches (set to False to track all modes)
COMPETITIVE_ONLY = True  # Default: True

# How long to wait for squad members to finish before processing (seconds)
GROUP_WAIT_TIME = 30  # Default: 30 seconds

# How long to wait after games end for API to update (seconds)
API_WAIT_TIME = 60  # Default: 60 seconds
```

## File Structure

```
valorant-discord-bot/
├── bot.py              # Main bot code
├── requirements.txt    # Python dependencies
├── user_data.json      # Persistent user registrations (auto-created)
├── settings.json       # Server settings like announcement channels (auto-created)
└── README.md           # This file
```

## Troubleshooting

**Bot doesn't detect game activity:**
- Ensure Presence Intent is enabled in Discord Developer Portal
- Make sure the user has "Display current activity as a status message" enabled in Discord settings

**Can't find Riot account:**
- Check the Riot ID spelling (case-insensitive)
- Make sure the tag is correct (without the #)
- Verify the region is correct

**No match results appearing:**
- The API may take a few minutes to update after a match
- Custom games may not appear in the API
- Deathmatch and other modes should work

## Contributing

Feel free to open issues or submit pull requests!

## License

MIT License - feel free to use and modify as needed.
