import discord
from discord.ext import commands
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

# Store user Riot IDs (PER GUILD - users must register in each server separately)
# Format: {"guild_id:user_id": {"riot_name": "Name", "riot_tag": "TAG", ...}}
user_data = {}

# Track active gaming sessions (PER GUILD)
# Format: {"guild_id:user_id": {"start_time": datetime, "is_streaming": bool, "voice_channel_id": int, "guild_id": int}}
active_sessions = {}

# Only track competitive matches
COMPETITIVE_ONLY = True

# Track users who recently finished games, grouped by voice channel
# Format: {(guild_id, voice_channel_id): {"users": [{"user_id": str, "member": Member, "session": dict}], "first_end_time": datetime, "task": Task}}
pending_announcements = {}

# Lock for thread-safe operations on pending_announcements
announcement_lock = asyncio.Lock()

# How long to wait for other players to finish before announcing (seconds)
GROUP_WAIT_TIME = 30

# How long to wait for match data to appear in API (seconds)
API_WAIT_TIME = 60

# File to persist user registrations
DATA_FILE = "user_data.json"
SETTINGS_FILE = "settings.json"

# Channel IDs where win/loss announcements will be posted (per guild)
# Format: {guild_id: channel_id}
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
            async with session.get(url, headers=self.headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data")
                return None
    
    async def get_recent_matches(self, name: str, tag: str, region: str = "na") -> Optional[list]:
        """Get recent matches for a player."""
        async with aiohttp.ClientSession() as session:
            url = f"{self.BASE_URL}/valorant/v3/matches/{region}/{name}/{tag}"
            async with session.get(url, headers=self.headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data", [])
                return None
    
    async def get_last_match(self, name: str, tag: str, region: str = "na") -> Optional[dict]:
        """Get the most recent match for a player."""
        matches = await self.get_recent_matches(name, tag, region)
        if matches and len(matches) > 0:
            return matches[0]
        return None


valorant_api = ValorantAPI(os.getenv("VALORANT_API_KEY"))  # Optional API key


@bot.event
async def on_ready():
    load_user_data()
    load_settings()
    print(f"✅ {bot.user} is online and tracking Valorant games!")
    print(f"📊 Tracking {len(user_data)} registered users")
    print(f"📢 Announcement channels set for {len(announcement_channels)} guilds")
    
    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        print(f"🔄 Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"Failed to sync commands: {e}")


@bot.event
async def on_presence_update(before: discord.Member, after: discord.Member):
    """Triggered when a member's presence changes (including game activity)."""
    # Create guild-scoped key for isolation between servers
    guild_user_key = f"{after.guild.id}:{after.id}"
    
    # Check if user is registered in THIS guild
    if guild_user_key not in user_data:
        return
    
    # Get Valorant activity
    before_valorant = get_valorant_activity(before)
    after_valorant = get_valorant_activity(after)
    
    # User started playing Valorant
    if not before_valorant and after_valorant:
        # Get their current voice channel (if any)
        voice_channel_id = None
        if after.voice and after.voice.channel:
            voice_channel_id = after.voice.channel.id
        
        active_sessions[guild_user_key] = {
            "start_time": datetime.now(timezone.utc),
            "is_streaming": is_streaming_valorant(after),
            "voice_channel_id": voice_channel_id,
            "guild_id": after.guild.id
        }
        print(f"🎮 {after.display_name} started playing Valorant in {after.guild.name} (VC: {voice_channel_id})")
    
    # User stopped playing Valorant
    elif before_valorant and not after_valorant:
        if guild_user_key in active_sessions:
            session = active_sessions.pop(guild_user_key)
            await queue_for_announcement(after, session, guild_user_key)


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Track voice channel changes for active players."""
    guild_user_key = f"{member.guild.id}:{member.id}"
    
    # Update voice channel if user is in an active session
    if guild_user_key in active_sessions:
        new_vc = after.channel.id if after.channel else None
        active_sessions[guild_user_key]["voice_channel_id"] = new_vc
        print(f"🔊 {member.display_name} moved to VC: {new_vc}")


def get_valorant_activity(member: discord.Member) -> Optional[discord.Activity]:
    """Check if member is playing Valorant."""
    for activity in member.activities:
        if isinstance(activity, discord.Game) and "valorant" in activity.name.lower():
            return activity
        if isinstance(activity, discord.Activity) and activity.name and "valorant" in activity.name.lower():
            return activity
    return None


def is_streaming_valorant(member: discord.Member) -> bool:
    """Check if member is streaming Valorant."""
    for activity in member.activities:
        if isinstance(activity, discord.Streaming):
            if activity.game and "valorant" in activity.game.lower():
                return True
    return False


async def queue_for_announcement(member: discord.Member, session: dict, guild_user_key: str):
    """Queue a player for grouped announcement based on voice channel."""
    voice_channel_id = session.get("voice_channel_id")
    guild_id = session.get("guild_id")
    
    # Create a group key - use voice channel if available, otherwise use a unique key per user
    if voice_channel_id:
        group_key = (guild_id, voice_channel_id)
    else:
        # Solo player not in voice - use unique key
        group_key = (guild_id, f"solo_{guild_user_key}")
    
    async with announcement_lock:
        if group_key not in pending_announcements:
            # First player in this group to finish
            pending_announcements[group_key] = {
                "users": [],
                "first_end_time": datetime.now(timezone.utc),
                "task": None
            }
        
        # Add this player to the group
        pending_announcements[group_key]["users"].append({
            "guild_user_key": guild_user_key,
            "member": member,
            "session": session
        })
        
        print(f"📋 Queued {member.display_name} for announcement (group: {group_key}, total: {len(pending_announcements[group_key]['users'])})")
        
        # Cancel existing task if any (we'll restart the timer)
        if pending_announcements[group_key]["task"]:
            pending_announcements[group_key]["task"].cancel()
        
        # Start/restart the countdown for this group
        pending_announcements[group_key]["task"] = asyncio.create_task(
            process_group_announcement(group_key)
        )


async def process_group_announcement(group_key: tuple):
    """Wait for group to assemble, then process announcement."""
    # Wait for other players to finish their games
    await asyncio.sleep(GROUP_WAIT_TIME)
    
    async with announcement_lock:
        if group_key not in pending_announcements:
            return
        
        group_data = pending_announcements.pop(group_key)
    
    users = group_data["users"]
    print(f"⏰ Processing announcement for {len(users)} player(s)")
    
    # Wait for API to update
    await asyncio.sleep(API_WAIT_TIME)
    
    # Fetch match data for all players
    player_matches = []
    
    for user_entry in users:
        guild_user_key = user_entry["guild_user_key"]
        member = user_entry["member"]
        session = user_entry["session"]
        
        if guild_user_key not in user_data:
            continue
        
        user_info = user_data[guild_user_key]
        match = await valorant_api.get_last_match(
            user_info["riot_name"],
            user_info["riot_tag"],
            user_info.get("region", "na")
        )
        
        if match:
            # Filter for competitive only
            if COMPETITIVE_ONLY and match["metadata"]["mode"].lower() != "competitive":
                print(f"⏭️ Skipping non-competitive match for {member.display_name} (mode: {match['metadata']['mode']})")
                continue
            
            player_matches.append({
                "member": member,
                "session": session,
                "user_info": user_info,
                "match": match
            })
    
    if not player_matches:
        print("❌ No competitive match data found for any player in group")
        return
    
    # Group players by match ID
    matches_by_id = defaultdict(list)
    for pm in player_matches:
        match_id = pm["match"]["metadata"]["matchid"]
        matches_by_id[match_id].append(pm)
    
    # Create announcements for each unique match
    for match_id, players_in_match in matches_by_id.items():
        await create_group_announcement(players_in_match)


async def create_group_announcement(players_in_match: list):
    """Create a single announcement for all players in the same match."""
    if not players_in_match:
        return
    
    # Use the first player's match data for common info
    match = players_in_match[0]["match"]
    
    map_name = match["metadata"]["map"]
    game_mode = match["metadata"]["mode"]
    
    teams = match["teams"]
    red_score = teams["red"]["rounds_won"]
    blue_score = teams["blue"]["rounds_won"]
    
    # Collect player stats
    player_stats = []
    any_streaming = False
    
    for pm in players_in_match:
        member = pm["member"]
        session = pm["session"]
        user_info = pm["user_info"]
        
        if session.get("is_streaming"):
            any_streaming = True
        
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
    
    if not player_stats:
        return
    
    # Determine overall result (use first player's result for embed color)
    overall_won = player_stats[0]["won"]
    
    # Check if all players are on the same team
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
    
    # Add match info
    embed.add_field(name="Map", value=map_name, inline=True)
    embed.add_field(name="Mode", value=game_mode, inline=True)
    embed.add_field(name="Score", value=f"🔴 {red_score} - {blue_score} 🔵", inline=True)
    
    # Add each player's stats
    embed.add_field(name="\u200b", value="**Player Stats**", inline=False)
    
    for ps in player_stats:
        player_data = ps["player_data"]
        result_emoji = "🏆" if ps["won"] else "💀"
        team_emoji = "🔴" if ps["team"] == "red" else "🔵"
        
        kills = player_data["stats"]["kills"]
        deaths = player_data["stats"]["deaths"]
        assists = player_data["stats"]["assists"]
        agent = player_data["character"]
        
        # Calculate KDA
        kda = (kills + assists) / max(deaths, 1)
        
        player_line = f"{result_emoji} {team_emoji} **{agent}** | K/D/A: **{kills}/{deaths}/{assists}** (KDA: {kda:.2f})"
        
        embed.add_field(
            name=f"{ps['member'].display_name}",
            value=player_line,
            inline=False
        )
    
    # Footer
    streaming_note = " 📺 Streaming" if any_streaming else ""
    riot_ids = ", ".join(ps["riot_id"] for ps in player_stats)
    embed.set_footer(text=f"{riot_ids}{streaming_note}")
    
    # Send to announcement channel
    guild = player_stats[0]["member"].guild
    channel_id = announcement_channels.get(guild.id)
    
    if channel_id:
        channel = guild.get_channel(channel_id)
        if channel:
            # Mention all players
            mentions = " ".join(ps["member"].mention for ps in player_stats)
            await channel.send(content=mentions, embed=embed)
    
    # DM each player
    for ps in player_stats:
        try:
            await ps["member"].send(embed=embed)
        except discord.Forbidden:
            pass


async def check_match_result(member: discord.Member, session: dict):
    """Legacy function - now handled by group announcements."""
    # This is kept for compatibility but the main logic is in create_group_announcement
    pass


# Slash Commands
@bot.tree.command(name="register", description="Register your Riot ID to track Valorant games")
@app_commands.describe(
    riot_name="Your Riot username (e.g., PlayerName)",
    riot_tag="Your Riot tag (e.g., NA1)",
    region="Your region (na, eu, ap, kr)"
)
@app_commands.choices(region=[
    app_commands.Choice(name="North America", value="na"),
    app_commands.Choice(name="Europe", value="eu"),
    app_commands.Choice(name="Asia Pacific", value="ap"),
    app_commands.Choice(name="Korea", value="kr"),
])
async def register(interaction: discord.Interaction, riot_name: str, riot_tag: str, region: str = "na"):
    """Register your Riot ID for tracking in this server."""
    await interaction.response.defer(ephemeral=True)
    
    # Verify the account exists
    account = await valorant_api.get_account(riot_name, riot_tag)
    
    if not account:
        await interaction.followup.send(
            f"❌ Could not find account **{riot_name}#{riot_tag}**. Please check your Riot ID and try again.",
            ephemeral=True
        )
        return
    
    # Use guild-scoped key for server isolation
    guild_user_key = f"{interaction.guild.id}:{interaction.user.id}"
    user_data[guild_user_key] = {
        "riot_name": riot_name,
        "riot_tag": riot_tag,
        "region": region,
        "registered_at": datetime.now(timezone.utc).isoformat()
    }
    save_user_data()
    
    await interaction.followup.send(
        f"✅ Successfully registered **{riot_name}#{riot_tag}** ({region.upper()}) in **{interaction.guild.name}**!\n"
        f"I'll now track your Competitive Valorant games and report results in this server.",
        ephemeral=True
    )


@bot.tree.command(name="unregister", description="Stop tracking your Valorant games in this server")
async def unregister(interaction: discord.Interaction):
    """Unregister from tracking in this server."""
    guild_user_key = f"{interaction.guild.id}:{interaction.user.id}"
    
    if guild_user_key in user_data:
        del user_data[guild_user_key]
        save_user_data()
        await interaction.response.send_message(
            f"✅ You've been unregistered from **{interaction.guild.name}**. Your games will no longer be tracked here.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message("❌ You're not currently registered in this server.", ephemeral=True)


@bot.tree.command(name="stats", description="Check your recent Valorant Competitive stats")
async def stats(interaction: discord.Interaction):
    """Get your recent competitive stats."""
    await interaction.response.defer()
    
    guild_user_key = f"{interaction.guild.id}:{interaction.user.id}"
    
    if guild_user_key not in user_data:
        await interaction.followup.send("❌ You're not registered in this server. Use `/register` first!")
        return
    
    user_info = user_data[guild_user_key]
    matches = await valorant_api.get_recent_matches(
        user_info["riot_name"],
        user_info["riot_tag"],
        user_info.get("region", "na")
    )
    
    if not matches:
        await interaction.followup.send("❌ Could not fetch your match history.")
        return
    
    # Filter for competitive matches only
    comp_matches = [m for m in matches if m["metadata"]["mode"].lower() == "competitive"][:5]
    
    if not comp_matches:
        await interaction.followup.send("❌ No recent competitive matches found.")
        return
    
    # Calculate stats from recent competitive matches
    wins = 0
    total_kills = 0
    total_deaths = 0
    total_assists = 0
    match_count = len(comp_matches)
    
    for match in comp_matches:
        for player in match["players"]["all_players"]:
            if player["name"].lower() == user_info["riot_name"].lower():
                total_kills += player["stats"]["kills"]
                total_deaths += player["stats"]["deaths"]
                total_assists += player["stats"]["assists"]
                
                team = player["team"].lower()
                if match["teams"][team]["has_won"]:
                    wins += 1
                break
    
    embed = discord.Embed(
        title=f"📊 Recent Competitive Stats for {user_info['riot_name']}#{user_info['riot_tag']}",
        color=discord.Color.blurple()
    )
    embed.add_field(name=f"Last {match_count} Comp Games", value=f"{wins}W - {match_count-wins}L", inline=True)
    embed.add_field(name="Total K/D/A", value=f"{total_kills}/{total_deaths}/{total_assists}", inline=True)
    embed.add_field(name="Avg K/D", value=f"{total_kills/match_count:.1f}/{total_deaths/match_count:.1f}", inline=True)
    
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="setchannel", description="Set the channel for match announcements (Admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    """Set the announcement channel for this server."""
    announcement_channels[interaction.guild.id] = channel.id
    save_settings()
    await interaction.response.send_message(f"✅ Match announcements will now be posted in {channel.mention}")


@bot.tree.command(name="lastmatch", description="Get details about your last competitive match")
async def last_match(interaction: discord.Interaction):
    """Get your last competitive match details."""
    await interaction.response.defer()
    
    guild_user_key = f"{interaction.guild.id}:{interaction.user.id}"
    
    if guild_user_key not in user_data:
        await interaction.followup.send("❌ You're not registered in this server. Use `/register` first!")
        return
    
    user_info = user_data[guild_user_key]
    matches = await valorant_api.get_recent_matches(
        user_info["riot_name"],
        user_info["riot_tag"],
        user_info.get("region", "na")
    )
    
    if not matches:
        await interaction.followup.send("❌ Could not fetch your match history.")
        return
    
    # Find the most recent competitive match
    match = None
    for m in matches:
        if m["metadata"]["mode"].lower() == "competitive":
            match = m
            break
    
    if not match:
        await interaction.followup.send("❌ No recent competitive matches found.")
        return
    
    # Find player data
    player_data = None
    for player in match["players"]["all_players"]:
        if player["name"].lower() == user_info["riot_name"].lower():
            player_data = player
            break
    
    if not player_data:
        await interaction.followup.send("❌ Could not find your data in the match.")
        return
    
    team = player_data["team"].lower()
    won = match["teams"][team]["has_won"]
    
    embed = discord.Embed(
        title="🎮 Last Competitive Match",
        color=discord.Color.green() if won else discord.Color.red()
    )
    
    result = "🏆 Victory" if won else "💀 Defeat"
    red_score = match["teams"]["red"]["rounds_won"]
    blue_score = match["teams"]["blue"]["rounds_won"]
    score = f"{red_score}-{blue_score}" if team == "red" else f"{blue_score}-{red_score}"
    
    embed.add_field(name="Result", value=result, inline=True)
    embed.add_field(name="Score", value=score, inline=True)
    embed.add_field(name="Map", value=match["metadata"]["map"], inline=True)
    embed.add_field(name="Agent", value=player_data["character"], inline=True)
    embed.add_field(
        name="K/D/A",
        value=f"{player_data['stats']['kills']}/{player_data['stats']['deaths']}/{player_data['stats']['assists']}",
        inline=True
    )
    embed.add_field(name="Mode", value=match["metadata"]["mode"], inline=True)
    
    await interaction.followup.send(embed=embed)


# Run the bot
if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        print("❌ Please set the DISCORD_BOT_TOKEN environment variable")
        exit(1)
    
    bot.run(token)