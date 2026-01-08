import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
import asyncio
from datetime import datetime, timezone
from typing import Optional
import json
import os
from collections import defaultdict

# Bot configuration
intents = discord.Intents.default()
intents.presences = True  # Required to track game activity
intents.members = True    # Required to see member presence updates
intents.message_content = True
intents.voice_states = True  # Required to track voice channel membership

bot = commands.Bot(command_prefix="!", intents=intents)

# Store user Riot IDs
# Format: {discord_user_id: {"riot_name": "Name", "riot_tag": "TAG", ...}}
user_data = {}

# Track active gaming sessions
# Format: {discord_user_id: {"member": Member, "last_match_id": str, "voice_channel_id": int, "guild_id": int, "started_at": datetime}}
active_sessions = {}

# Only track these game modes (set to None to track all modes)
ALLOWED_MODES = ["competitive", "swiftplay"]

# Rate limit: 30 requests/min - polling every 30 seconds to stay safe
POLL_INTERVAL = 30.0

# How long to wait for squad members to finish before announcing
GROUP_WAIT_TIME = 15

# Track which group to poll next (round-robin through unique VC groups)
poll_index = 0

# Track players who just finished, grouped by voice channel for squad announcements
# Format: {(guild_id, voice_channel_id): {"players": [...], "task": Task}}
pending_announcements = {}
announcement_lock = asyncio.Lock()

# Data directory (use /app/data for Railway with volume, or local directory)
DATA_DIR = os.getenv("DATA_DIR", ".")
DATA_FILE = os.path.join(DATA_DIR, "user_data.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

# Channel IDs where win/loss announcements will be posted (per guild)
announcement_channels = {}


def load_user_data():
    """Load user data from file."""
    global user_data
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            user_data = json.load(f)
    else:
        user_data = {}


def save_user_data():
    """Save user data to file."""
    with open(DATA_FILE, "w") as f:
        json.dump(user_data, f, indent=2)


def load_settings():
    """Load settings from file."""
    global announcement_channels
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r") as f:
            data = json.load(f)
            announcement_channels = {int(k): v for k, v in data.get("announcement_channels", {}).items()}
    else:
        announcement_channels = {}


def save_settings():
    """Save settings to file."""
    with open(SETTINGS_FILE, "w") as f:
        json.dump({"announcement_channels": announcement_channels}, f, indent=2)


class ValorantAPI:
    """Wrapper for Henrik's Valorant API."""
    
    BASE_URL = "https://api.henrikdev.xyz"
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key
        self.headers = {}
        if api_key:
            self.headers["Authorization"] = api_key
    
    async def get_account(self, name: str, tag: str) -> Optional[dict]:
        """Get account info by name and tag."""
        async with aiohttp.ClientSession() as session:
            url = f"{self.BASE_URL}/valorant/v1/account/{name}/{tag}"
            print(f"🔍 API: GET {url}")
            async with session.get(url, headers=self.headers) as resp:
                print(f"📡 API: {resp.status}")
                if resp.status == 200:
                    data = await resp.json()
                    print(f"✅ API: Found account {name}#{tag}")
                    return data.get("data")
                else:
                    text = await resp.text()
                    print(f"❌ API: Error - {text[:200]}")
                return None
    
    async def get_recent_matches(self, name: str, tag: str, region: str = "na") -> Optional[list]:
        """Get recent matches for a player."""
        async with aiohttp.ClientSession() as session:
            url = f"{self.BASE_URL}/valorant/v3/matches/{region}/{name}/{tag}"
            print(f"🔍 API: GET {url}")
            async with session.get(url, headers=self.headers) as resp:
                print(f"📡 API: {resp.status}")
                if resp.status == 200:
                    data = await resp.json()
                    matches = data.get("data", [])
                    print(f"✅ API: Got {len(matches)} matches for {name}#{tag}")
                    return matches
                else:
                    text = await resp.text()
                    print(f"❌ API: Error - {text[:200]}")
                return None
    
    async def get_last_match(self, name: str, tag: str, region: str = "na") -> Optional[dict]:
        """Get the most recent match for a player."""
        matches = await self.get_recent_matches(name, tag, region)
        if matches and len(matches) > 0:
            match = matches[0]
            mode = match["metadata"]["mode"]
            map_name = match["metadata"]["map"]
            match_id = match["metadata"]["matchid"][:8]
            print(f"📋 API: Last match - {mode} on {map_name} (ID: {match_id}...)")
            return match
        return None
    
    async def get_last_match_id(self, name: str, tag: str, region: str = "na") -> Optional[str]:
        """Get just the match ID of the most recent match."""
        match = await self.get_last_match(name, tag, region)
        if match:
            return match["metadata"]["matchid"]
        return None


valorant_api = ValorantAPI(os.getenv("VALORANT_API_KEY"))


@bot.event
async def on_ready():
    load_user_data()
    load_settings()
    print(f"{'='*50}")
    print(f"✅ {bot.user} is online!")
    print(f"{'='*50}")
    print(f"📂 Data directory: {DATA_DIR}")
    print(f"📂 Data file: {DATA_FILE}")
    print(f"📂 Settings file: {SETTINGS_FILE}")
    print(f"📊 Registered users: {len(user_data)}")
    for uid, info in user_data.items():
        print(f"   └─ {uid}: {info.get('riot_name')}#{info.get('riot_tag')} ({info.get('region')})")
    print(f"📢 Announcement channels: {len(announcement_channels)}")
    for gid, cid in announcement_channels.items():
        print(f"   └─ Guild {gid}: Channel {cid}")
    print(f"🎮 Tracking modes: {ALLOWED_MODES if ALLOWED_MODES else 'all'}")
    print(f"⏱️ Poll interval: {POLL_INTERVAL}s")
    print(f"🔗 Connected to {len(bot.guilds)} guild(s):")
    for guild in bot.guilds:
        print(f"   └─ {guild.name} ({guild.id}) - {guild.member_count} members")
    print(f"{'='*50}")
    
    # Start the polling loop
    if not match_poller.is_running():
        match_poller.start()
        print(f"🔄 Match poller started")
    
    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        print(f"🔄 Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"❌ Failed to sync commands: {e}")
    
    print(f"{'='*50}")
    
    # Scan for registered users already playing Valorant
    print(f"🔍 Scanning for users already playing Valorant...")
    for guild in bot.guilds:
        for member in guild.members:
            user_id = str(member.id)
            if user_id in user_data and user_id not in active_sessions:
                if get_valorant_activity(member):
                    print(f"🎮 Found {member.display_name} already playing Valorant!")
                    await start_tracking(member)
    
    if active_sessions:
        print(f"✅ Now tracking {len(active_sessions)} player(s) from startup scan")
    else:
        print(f"✅ No registered users currently playing Valorant")


@bot.event
async def on_presence_update(before: discord.Member, after: discord.Member):
    """Triggered when a member's presence changes."""
    user_id = str(after.id)
    
    # Log ALL presence changes with full details
    before_activities = [f"{type(a).__name__}:{getattr(a, 'name', '?')}" for a in before.activities]
    after_activities = [f"{type(a).__name__}:{getattr(a, 'name', '?')}" for a in after.activities]
    
    if before_activities != after_activities:
        print(f"👀 Presence: {after.display_name} ({user_id})")
        print(f"   Before: {before_activities if before_activities else 'none'}")
        print(f"   After:  {after_activities if after_activities else 'none'}")
        print(f"   Registered: {user_id in user_data}")
    
    # Check if user is registered
    if user_id not in user_data:
        return
    
    before_valorant = get_valorant_activity(before)
    after_valorant = get_valorant_activity(after)
    
    print(f"   Valorant before: {before_valorant}")
    print(f"   Valorant after:  {after_valorant}")
    
    # User started playing Valorant
    if not before_valorant and after_valorant:
        print(f"🎮 VALORANT STARTED: {after.display_name}")
        await start_tracking(after)
    
    # User stopped playing Valorant
    elif before_valorant and not after_valorant:
        print(f"🛑 VALORANT STOPPED: {after.display_name}")
        if user_id in active_sessions:
            print(f"   └─ Still in active_sessions, waiting for poller to detect match end")


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Track voice channel changes for active players."""
    user_id = str(member.id)
    
    if user_id in active_sessions:
        new_vc = after.channel.id if after.channel else None
        active_sessions[user_id]["voice_channel_id"] = new_vc
        print(f"🔊 {member.display_name} moved to VC: {new_vc}")


async def start_tracking(member: discord.Member):
    """Start tracking a player's match."""
    user_id = str(member.id)
    user_info = user_data.get(user_id)
    
    if not user_info:
        return
    
    print(f"🎮 {member.display_name} started Valorant - fetching last match ID...")
    
    # Get their current last match ID (so we know when a NEW one appears)
    last_match_id = await valorant_api.get_last_match_id(
        user_info["riot_name"],
        user_info["riot_tag"],
        user_info.get("region", "na")
    )
    
    voice_channel_id = None
    if member.voice and member.voice.channel:
        voice_channel_id = member.voice.channel.id
    
    active_sessions[user_id] = {
        "member": member,
        "last_match_id": last_match_id,
        "voice_channel_id": voice_channel_id,
        "guild_id": member.guild.id,
        "started_at": datetime.now(timezone.utc)
    }
    
    print(f"✅ Now tracking {member.display_name} | Last match: {last_match_id[:8] if last_match_id else 'None'}... | Active sessions: {len(active_sessions)}")


@tasks.loop(seconds=POLL_INTERVAL)
async def match_poller():
    """Poll one player at a time, but scan ALL registered players in the returned match data."""
    global poll_index
    
    if not active_sessions:
        return
    
    user_ids = list(active_sessions.keys())
    if not user_ids:
        return
    
    # Round-robin: pick the next player to query
    poll_index = poll_index % len(user_ids)
    user_id = user_ids[poll_index]
    poll_index += 1
    
    if user_id not in active_sessions:
        return
    
    user_info = user_data.get(user_id)
    if not user_info:
        return
    
    session = active_sessions[user_id]
    member = session["member"]
    print(f"🔄 Polling {member.display_name} ({poll_index}/{len(user_ids)}) | Looking for match newer than {session['last_match_id'][:8] if session['last_match_id'] else 'None'}...")
    
    try:
        # Get this player's last match (returns all 10 players in the match)
        current_match = await valorant_api.get_last_match(
            user_info["riot_name"],
            user_info["riot_tag"],
            user_info.get("region", "na")
        )
        
        if not current_match:
            return
        
        current_match_id = current_match["metadata"]["matchid"]
        
        # Build a lookup of all players in this match (lowercase for comparison)
        match_players = {}
        for player in current_match["players"]["all_players"]:
            key = f"{player['name'].lower()}#{player['tag'].lower()}"
            match_players[key] = player
        
        # Scan ALL active sessions to find anyone in this match with a new match ID
        players_with_new_match = []
        
        for check_user_id, check_session in list(active_sessions.items()):
            check_user_info = user_data.get(check_user_id)
            if not check_user_info:
                continue
            
            # Is this player in the match we just queried?
            player_key = f"{check_user_info['riot_name'].lower()}#{check_user_info['riot_tag'].lower()}"
            if player_key not in match_players:
                continue
            
            # Is this match NEW for them?
            if current_match_id == check_session["last_match_id"]:
                continue
            
            # Found a player with a new match!
            players_with_new_match.append(check_user_id)
        
        if not players_with_new_match:
            print(f"⏸️ No new matches detected this poll")
            return
        
        # Check if it's an allowed mode
        match_mode = current_match["metadata"]["mode"].lower()
        if ALLOWED_MODES and match_mode not in ALLOWED_MODES:
            print(f"⏭️ Skipping {match_mode} match (allowed: {ALLOWED_MODES})")
            # Update last_match_id for all these players
            for uid in players_with_new_match:
                if uid in active_sessions:
                    active_sessions[uid]["last_match_id"] = current_match_id
            return
        
        print(f"🏁 New match detected: {current_match_id} ({len(players_with_new_match)} registered player(s))")
        
        # Remove from active sessions and queue for announcement
        for uid in players_with_new_match:
            if uid not in active_sessions:
                continue
            
            player_session = active_sessions.pop(uid)
            member = player_session["member"]
            
            await queue_for_announcement(member, player_session, current_match)
            
    except Exception as e:
        print(f"❌ Error polling: {e}")


@match_poller.before_loop
async def before_poller():
    await bot.wait_until_ready()


async def queue_for_announcement(member: discord.Member, session: dict, match: dict):
    """Queue a player for grouped announcement by match ID."""
    user_id = str(member.id)
    match_id = match["metadata"]["matchid"]
    guild_id = session.get("guild_id")
    
    # Group by match ID - all players in same match get one announcement
    group_key = (guild_id, match_id)
    
    player_data = {
        "user_id": user_id,
        "member": member,
        "session": session,
        "match": match,
        "user_info": user_data.get(user_id)
    }
    
    async with announcement_lock:
        if group_key not in pending_announcements:
            pending_announcements[group_key] = {
                "players": [],
                "task": None
            }
        
        pending_announcements[group_key]["players"].append(player_data)
        print(f"📋 Queued {member.display_name} for match {match_id[:8]}... ({len(pending_announcements[group_key]['players'])} player(s))")
        
        # Cancel existing timer and restart (wait for more players from same match)
        if pending_announcements[group_key]["task"]:
            pending_announcements[group_key]["task"].cancel()
        
        pending_announcements[group_key]["task"] = asyncio.create_task(
            process_group_announcement(group_key)
        )


async def process_group_announcement(group_key: tuple):
    """Wait for all players in match to be detected, then announce."""
    await asyncio.sleep(GROUP_WAIT_TIME)
    
    async with announcement_lock:
        if group_key not in pending_announcements:
            return
        group_data = pending_announcements.pop(group_key)
    
    players = group_data["players"]
    print(f"📢 Announcing match result for {len(players)} player(s)")
    
    await create_announcement(players)


async def create_announcement(players_in_match: list):
    """Create announcement embed for players in the same match."""
    if not players_in_match:
        return
    
    match = players_in_match[0]["match"]
    map_name = match["metadata"]["map"]
    game_mode = match["metadata"]["mode"]
    match_id = match["metadata"]["matchid"][:8]
    teams = match["teams"]
    red_score = teams["red"]["rounds_won"]
    blue_score = teams["blue"]["rounds_won"]
    
    print(f"📝 Creating announcement for match {match_id}...")
    print(f"   └─ {game_mode} on {map_name} | Score: {red_score}-{blue_score}")
    
    # Collect player stats
    player_stats = []
    
    for p in players_in_match:
        member = p["member"]
        user_info = p["user_info"]
        
        # Find player in match data
        player_data = None
        for player in match["players"]["all_players"]:
            if (player["name"].lower() == user_info["riot_name"].lower() and
                player["tag"].lower() == user_info["riot_tag"].lower()):
                player_data = player
                break
        
        if player_data:
            team = player_data["team"].lower()
            won = teams[team]["has_won"]
            
            player_stats.append({
                "member": member,
                "player_data": player_data,
                "team": team,
                "won": won,
                "riot_id": f"{user_info['riot_name']}#{user_info['riot_tag']}"
            })
            result = "WIN" if won else "LOSS"
            print(f"   └─ {member.display_name}: {result} | {player_data['character']} | {player_data['stats']['kills']}/{player_data['stats']['deaths']}/{player_data['stats']['assists']}")
    
    if not player_stats:
        return
    
    # Determine overall result
    overall_won = player_stats[0]["won"]
    teams_in_party = set(ps["team"] for ps in player_stats)
    mixed_teams = len(teams_in_party) > 1
    
    # Create embed
    if len(player_stats) == 1:
        title = "🎮 Valorant Match Complete!"
    else:
        title = f"🎮 Squad Match Complete! ({len(player_stats)} players)"
    
    embed = discord.Embed(
        title=title,
        color=discord.Color.gold() if mixed_teams else (discord.Color.green() if overall_won else discord.Color.red()),
        timestamp=datetime.now(timezone.utc)
    )
    
    embed.add_field(name="Map", value=map_name, inline=True)
    embed.add_field(name="Mode", value=game_mode, inline=True)
    embed.add_field(name="Score", value=f"🔴 {red_score} - {blue_score} 🔵", inline=True)
    
    embed.add_field(name="\u200b", value="**Player Stats**", inline=False)
    
    for ps in player_stats:
        player_data = ps["player_data"]
        result_emoji = "🏆" if ps["won"] else "💀"
        team_emoji = "🔴" if ps["team"] == "red" else "🔵"
        
        kills = player_data["stats"]["kills"]
        deaths = player_data["stats"]["deaths"]
        assists = player_data["stats"]["assists"]
        agent = player_data["character"]
        kda = (kills + assists) / max(deaths, 1)
        
        player_line = f"{result_emoji} {team_emoji} **{agent}** | K/D/A: **{kills}/{deaths}/{assists}** ({kda:.2f})"
        
        embed.add_field(
            name=f"{ps['member'].display_name}",
            value=player_line,
            inline=False
        )
    
    riot_ids = ", ".join(ps["riot_id"] for ps in player_stats)
    embed.set_footer(text=riot_ids)
    
    # Send to announcement channel
    guild = player_stats[0]["member"].guild
    channel_id = announcement_channels.get(guild.id)
    
    if channel_id:
        channel = guild.get_channel(channel_id)
        if channel:
            mentions = " ".join(ps["member"].mention for ps in player_stats)
            await channel.send(content=mentions, embed=embed)
            print(f"✅ Announcement sent to #{channel.name}")
        else:
            print(f"❌ Could not find announcement channel {channel_id}")
    else:
        print(f"⚠️ No announcement channel set for guild {guild.name}")
    
    # DM each player
    for ps in player_stats:
        try:
            await ps["member"].send(embed=embed)
            print(f"✅ DM sent to {ps['member'].display_name}")
        except discord.Forbidden:
            print(f"⚠️ Could not DM {ps['member'].display_name} (DMs disabled)")


def get_valorant_activity(member: discord.Member) -> Optional[discord.Activity]:
    """Check if member is playing Valorant."""
    for activity in member.activities:
        activity_name = getattr(activity, 'name', None)
        if activity_name:
            if isinstance(activity, discord.Game) and "valorant" in activity_name.lower():
                return activity
            if isinstance(activity, discord.Activity) and "valorant" in activity_name.lower():
                return activity
    return None


# Slash Commands
@bot.tree.command(name="register", description="Register your Riot ID to track Valorant games")
@app_commands.describe(
    riot_name="Your Riot username (e.g., PlayerName)",
    riot_tag="Your Riot tag (e.g., NA1)",
    region="Your region"
)
@app_commands.choices(region=[
    app_commands.Choice(name="North America", value="na"),
    app_commands.Choice(name="Europe", value="eu"),
    app_commands.Choice(name="Asia Pacific", value="ap"),
    app_commands.Choice(name="Korea", value="kr"),
])
async def register(interaction: discord.Interaction, riot_name: str, riot_tag: str, region: str = "na"):
    """Register your Riot ID for tracking."""
    await interaction.response.defer(ephemeral=True)
    
    account = await valorant_api.get_account(riot_name, riot_tag)
    
    if not account:
        await interaction.followup.send(
            f"❌ Could not find account **{riot_name}#{riot_tag}**. Please check your Riot ID and try again.",
            ephemeral=True
        )
        return
    
    user_id = str(interaction.user.id)
    user_data[user_id] = {
        "riot_name": riot_name,
        "riot_tag": riot_tag,
        "region": region,
        "registered_at": datetime.now(timezone.utc).isoformat()
    }
    save_user_data()
    
    print(f"📝 Registered: {interaction.user.display_name} -> {riot_name}#{riot_tag} ({region})")
    
    await interaction.followup.send(
        f"✅ Successfully registered **{riot_name}#{riot_tag}** ({region.upper()})!\n"
        f"I'll now track your Valorant matches ({', '.join(ALLOWED_MODES) if ALLOWED_MODES else 'all modes'}).",
        ephemeral=True
    )


@bot.tree.command(name="unregister", description="Stop tracking your Valorant games")
async def unregister(interaction: discord.Interaction):
    """Unregister from tracking."""
    user_id = str(interaction.user.id)
    
    if user_id in user_data:
        del user_data[user_id]
        save_user_data()
        if user_id in active_sessions:
            del active_sessions[user_id]
        await interaction.response.send_message("✅ Unregistered. Your games will no longer be tracked.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ You're not registered.", ephemeral=True)


@bot.tree.command(name="setchannel", description="Set the channel for match announcements (Admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    """Set the announcement channel for this server."""
    announcement_channels[interaction.guild.id] = channel.id
    save_settings()
    print(f"📢 Announcement channel set: Guild {interaction.guild.id} ({interaction.guild.name}) -> Channel {channel.id} (#{channel.name})")
    await interaction.response.send_message(f"✅ Match announcements will be posted in {channel.mention}")


@bot.tree.command(name="status", description="Check who is currently being tracked")
async def status(interaction: discord.Interaction):
    """Show currently tracked players."""
    if not active_sessions:
        await interaction.response.send_message("No one is currently being tracked.", ephemeral=True)
        return
    
    lines = []
    for user_id, session in active_sessions.items():
        member = session["member"]
        vc = "in VC" if session.get("voice_channel_id") else "solo"
        lines.append(f"• {member.display_name} ({vc})")
    
    num_players = len(active_sessions)
    poll_cycle = POLL_INTERVAL * num_players
    
    await interaction.response.send_message(
        f"**Tracking {num_players} player(s):**\n" + 
        "\n".join(lines) + 
        f"\n\n_Full poll cycle: {poll_cycle:.0f}s (1 API call detects all players in same match)_",
        ephemeral=True
    )


@bot.tree.command(name="stats", description="Check your recent Valorant stats")
async def stats(interaction: discord.Interaction):
    """Get your recent stats."""
    await interaction.response.defer()
    
    user_id = str(interaction.user.id)
    
    if user_id not in user_data:
        await interaction.followup.send("❌ You're not registered. Use `/register` first!")
        return
    
    user_info = user_data[user_id]
    matches = await valorant_api.get_recent_matches(
        user_info["riot_name"],
        user_info["riot_tag"],
        user_info.get("region", "na")
    )
    
    if not matches:
        await interaction.followup.send("❌ Could not fetch your match history.")
        return
    
    # Filter to allowed modes only
    if ALLOWED_MODES:
        filtered_matches = [m for m in matches if m["metadata"]["mode"].lower() in ALLOWED_MODES][:5]
    else:
        filtered_matches = matches[:5]
    
    if not filtered_matches:
        await interaction.followup.send(f"❌ No recent {'/'.join(ALLOWED_MODES) if ALLOWED_MODES else ''} matches found.")
        return
    
    wins = 0
    total_kills = 0
    total_deaths = 0
    total_assists = 0
    
    for match in filtered_matches:
        for player in match["players"]["all_players"]:
            if player["name"].lower() == user_info["riot_name"].lower():
                total_kills += player["stats"]["kills"]
                total_deaths += player["stats"]["deaths"]
                total_assists += player["stats"]["assists"]
                
                team = player["team"].lower()
                if match["teams"][team]["has_won"]:
                    wins += 1
                break
    
    num_matches = len(filtered_matches)
    embed = discord.Embed(
        title=f"📊 Recent Stats",
        description=f"**{user_info['riot_name']}#{user_info['riot_tag']}**",
        color=discord.Color.blurple()
    )
    embed.add_field(name=f"Last {num_matches} Games", value=f"{wins}W - {num_matches - wins}L", inline=True)
    embed.add_field(name="Total K/D/A", value=f"{total_kills}/{total_deaths}/{total_assists}", inline=True)
    embed.add_field(name="Avg K/D", value=f"{total_kills/num_matches:.1f}/{total_deaths/num_matches:.1f}", inline=True)
    
    await interaction.followup.send(embed=embed)


# Run the bot
if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        print("❌ Please set the DISCORD_BOT_TOKEN environment variable")
        exit(1)
    
    bot.run(token)