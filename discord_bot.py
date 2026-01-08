import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
import asyncio
from datetime import datetime, timezone
from typing import Optional
import json
import os
import random
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

# User balances for betting
# Format: {discord_user_id: {"balance": int, "vc_minutes_today": int, "last_daily": str, "daily_claimed": bool}}
user_balances = {}
STARTING_BALANCE = 100
DAILY_BONUS = 50
VC_MINUTES_FOR_DAILY = 30

# Active betting pools
# Format: {(guild_id, player_user_id): {"player_name": str, "bets": {"win": {}, "loss": {}}, "closes_at": datetime, "message": Message}}
active_bets = {}
BETTING_WINDOW = 180  # 3 minutes in seconds

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
BALANCES_FILE = os.path.join(DATA_DIR, "balances.json")

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


def load_balances():
    """Load user balances from file."""
    global user_balances
    if os.path.exists(BALANCES_FILE):
        with open(BALANCES_FILE, "r") as f:
            user_balances = json.load(f)
    else:
        user_balances = {}


def save_balances():
    """Save user balances to file."""
    with open(BALANCES_FILE, "w") as f:
        json.dump(user_balances, f, indent=2)


def get_balance(user_id: str) -> int:
    """Get a user's balance, creating account if needed."""
    if user_id not in user_balances:
        user_balances[user_id] = {
            "balance": STARTING_BALANCE,
            "vc_minutes_today": 0,
            "last_vc_check": None,
            "daily_claimed": False,
            "last_daily_date": None
        }
        save_balances()
    return user_balances[user_id]["balance"]


def update_balance(user_id: str, amount: int):
    """Update a user's balance by amount (can be negative)."""
    get_balance(user_id)  # Ensure account exists
    user_balances[user_id]["balance"] += amount
    if user_balances[user_id]["balance"] < 0:
        user_balances[user_id]["balance"] = 0
    save_balances()
    return user_balances[user_id]["balance"]


def set_balance(user_id: str, amount: int):
    """Set a user's balance to a specific amount."""
    get_balance(user_id)  # Ensure account exists
    user_balances[user_id]["balance"] = max(0, amount)
    save_balances()
    return user_balances[user_id]["balance"]


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
    load_balances()
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
    print(f"💰 Users with balances: {len(user_balances)}")
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
    """Track voice channel changes for active players and VC time for daily bonus."""
    user_id = str(member.id)
    
    # Track VC for active sessions
    if user_id in active_sessions:
        new_vc = after.channel.id if after.channel else None
        active_sessions[user_id]["voice_channel_id"] = new_vc
        print(f"🔊 {member.display_name} moved to VC: {new_vc}")
    
    # Track VC time for daily bonus
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    
    # Ensure user has balance data
    get_balance(user_id)
    user_bal = user_balances[user_id]
    
    # Reset daily tracking if it's a new day
    if user_bal.get("last_daily_date") != today:
        user_bal["vc_minutes_today"] = 0
        user_bal["daily_claimed"] = False
        user_bal["last_daily_date"] = today
    
    # User joined VC
    if before.channel is None and after.channel is not None:
        user_bal["vc_join_time"] = now.isoformat()
        save_balances()
    
    # User left VC
    elif before.channel is not None and after.channel is None:
        if user_bal.get("vc_join_time"):
            try:
                join_time = datetime.fromisoformat(user_bal["vc_join_time"])
                minutes = int((now - join_time).total_seconds() / 60)
                user_bal["vc_minutes_today"] = user_bal.get("vc_minutes_today", 0) + minutes
                user_bal["vc_join_time"] = None
                save_balances()
                print(f"⏱️ {member.display_name} spent {minutes} min in VC (total today: {user_bal['vc_minutes_today']})")
                
                # Auto-claim daily bonus if eligible
                if not user_bal.get("daily_claimed") and user_bal.get("vc_minutes_today", 0) >= VC_MINUTES_FOR_DAILY:
                    user_bal["daily_claimed"] = True
                    new_balance = update_balance(user_id, DAILY_BONUS)
                    print(f"🎁 Auto-claimed daily bonus for {member.display_name}")
                    
                    # Announce in channel (no ping)
                    guild = member.guild
                    channel_id = announcement_channels.get(guild.id)
                    if channel_id:
                        channel = guild.get_channel(channel_id)
                        if channel:
                            await channel.send(
                                f"🎁 **{member.display_name}** earned their daily bonus! +**{DAILY_BONUS}** coins (Balance: {new_balance})"
                            )
            except:
                pass


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
    
    # Open betting for this player
    await open_betting(member, user_info)


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
            await channel.send(embed=embed)
            print(f"✅ Announcement sent to #{channel.name}")
        else:
            print(f"❌ Could not find announcement channel {channel_id}")
    else:
        print(f"⚠️ No announcement channel set for guild {guild.name}")
    
    # Resolve bets for each player
    for ps in player_stats:
        bet_key = (guild.id, str(ps["member"].id))
        outcome = "win" if ps["won"] else "loss"
        await resolve_bets(bet_key, outcome)


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


# ==================== BETTING SYSTEM ====================

async def open_betting(member: discord.Member, user_info: dict):
    """Open betting for a player's match."""
    guild = member.guild
    channel_id = announcement_channels.get(guild.id)
    
    if not channel_id:
        return
    
    channel = guild.get_channel(channel_id)
    if not channel:
        return
    
    bet_key = (guild.id, str(member.id))
    closes_at = datetime.now(timezone.utc).timestamp() + BETTING_WINDOW
    
    active_bets[bet_key] = {
        "player_name": member.display_name,
        "player_riot_id": f"{user_info['riot_name']}#{user_info['riot_tag']}",
        "bets": {"win": {}, "loss": {}},
        "closes_at": closes_at,
        "message": None,
        "guild_id": guild.id
    }
    
    embed = discord.Embed(
        title=f"🎰 Betting Open: {member.display_name}",
        description=f"**{user_info['riot_name']}#{user_info['riot_tag']}** just started a match!\n\n"
                    f"Place your bets with `/bet`\n"
                    f"Betting closes <t:{int(closes_at)}:R>",
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="💰 Win Pool", value="0 coins (0 bets)", inline=True)
    embed.add_field(name="💀 Loss Pool", value="0 coins (0 bets)", inline=True)
    embed.add_field(name="📊 Win Odds", value="--", inline=True)
    embed.set_footer(text="Win bets get 1.05-1.2x bonus • House takes 5% on pools 100+")
    
    msg = await channel.send(embed=embed)
    active_bets[bet_key]["message"] = msg
    
    print(f"🎰 Betting opened for {member.display_name}")
    
    # Schedule betting close
    asyncio.create_task(close_betting_after_delay(bet_key, BETTING_WINDOW))


async def close_betting_after_delay(bet_key: tuple, delay: int):
    """Close betting after the delay."""
    await asyncio.sleep(delay)
    await close_betting(bet_key)


async def close_betting(bet_key: tuple):
    """Close betting for a match."""
    if bet_key not in active_bets:
        return
    
    bet_data = active_bets[bet_key]
    bet_data["closed"] = True
    
    # Update the message
    if bet_data.get("message"):
        try:
            embed = bet_data["message"].embeds[0]
            embed.title = f"🔒 Betting Closed: {bet_data['player_name']}"
            embed.description = f"**{bet_data['player_riot_id']}** is in a match!\n\nBetting is now closed. Results when match ends."
            embed.color = discord.Color.dark_gray()
            await bet_data["message"].edit(embed=embed)
        except:
            pass
    
    print(f"🔒 Betting closed for {bet_data['player_name']}")


async def update_betting_embed(bet_key: tuple):
    """Update the betting embed with current pools."""
    if bet_key not in active_bets:
        return
    
    bet_data = active_bets[bet_key]
    if not bet_data.get("message"):
        return
    
    win_pool = sum(bet_data["bets"]["win"].values())
    loss_pool = sum(bet_data["bets"]["loss"].values())
    total_pool = win_pool + loss_pool
    
    win_bettors = len(bet_data["bets"]["win"])
    loss_bettors = len(bet_data["bets"]["loss"])
    
    # Calculate odds (potential payout multiplier for a 1 coin bet)
    # House takes 5% only on pools >= 100
    house_mult = 0.95 if total_pool >= 100 else 1.0
    
    if total_pool > 0 and win_pool > 0:
        win_odds = f"{((total_pool * house_mult) / win_pool):.2f}x"
    else:
        win_odds = "--"
    
    if total_pool > 0 and loss_pool > 0:
        loss_odds = f"{((total_pool * house_mult) / loss_pool):.2f}x"
    else:
        loss_odds = "--"
    
    try:
        embed = bet_data["message"].embeds[0]
        embed.set_field_at(0, name="💰 Win Pool", value=f"{win_pool} coins ({win_bettors} bets)", inline=True)
        embed.set_field_at(1, name="💀 Loss Pool", value=f"{loss_pool} coins ({loss_bettors} bets)", inline=True)
        embed.set_field_at(2, name="📊 Odds (Win/Loss)", value=f"{win_odds} / {loss_odds}", inline=True)
        await bet_data["message"].edit(embed=embed)
    except:
        pass


def calculate_payouts(bet_data: dict, outcome: str) -> dict:
    """
    Calculate payouts for all bettors.
    House takes 5% only on pools of 100+ coins.
    Win bets get a random 1.05-1.2x multiplier to encourage winning.
    Returns: {user_id: {"payout": int, "profit": int, "bet": int, "side": str, "multiplier": float}}
    """
    bets = bet_data["bets"]
    win_pool = sum(bets["win"].values())
    loss_pool = sum(bets["loss"].values())
    total_pool = win_pool + loss_pool
    
    # House cut: 5% only on pools >= 100 coins
    if total_pool >= 100:
        house_cut = 0.05
    else:
        house_cut = 0.0
    
    winning_side = "win" if outcome == "win" else "loss"
    losing_side = "loss" if outcome == "win" else "win"
    
    winning_bets = bets[winning_side]
    losing_bets = bets[losing_side]
    winning_pool_total = sum(winning_bets.values())
    losing_pool_total = sum(losing_bets.values())
    
    results = {}
    
    # Initialize losers
    for uid, amount in losing_bets.items():
        results[uid] = {"payout": 0, "profit": -amount, "bet": amount, "side": losing_side, "multiplier": None}
    
    # Edge case: No bets at all
    if total_pool == 0:
        return results
    
    # Edge case: No one bet on winning side - losers lose to house
    if winning_pool_total == 0:
        return results
    
    # Edge case: Only one person bet total - return their bet + bonus from house
    if len(winning_bets) + len(losing_bets) == 1:
        for uid, amount in winning_bets.items():
            bonus = max(1, int(amount * 0.25))  # 25% bonus for being brave, min 1
            # Apply win multiplier if they bet on win
            if winning_side == "win":
                multiplier = round(random.uniform(1.05, 1.20), 2)
                bonus = int(bonus * multiplier)
            else:
                multiplier = None
            results[uid] = {"payout": amount + bonus, "profit": bonus, "bet": amount, "side": winning_side, "multiplier": multiplier}
        return results
    
    # Edge case: No one bet on losing side - winners get their bet back + bonus from house
    if losing_pool_total == 0:
        for uid, amount in winning_bets.items():
            bonus = max(1, int(amount * 0.20))  # 20% bonus for correct prediction, min 1
            # Apply win multiplier if they bet on win
            if winning_side == "win":
                multiplier = round(random.uniform(1.05, 1.20), 2)
                bonus = int(bonus * multiplier)
            else:
                multiplier = None
            results[uid] = {"payout": amount + bonus, "profit": bonus, "bet": amount, "side": winning_side, "multiplier": multiplier}
        return results
    
    # Normal case: Pari-mutuel payout
    house_take = int(total_pool * house_cut)
    payout_pool = total_pool - house_take
    
    for uid, amount in winning_bets.items():
        share = amount / winning_pool_total
        payout = int(payout_pool * share)
        
        # Apply win multiplier if they bet on win (not loss)
        if winning_side == "win":
            multiplier = round(random.uniform(1.05, 1.20), 2)
            payout = int(payout * multiplier)
        else:
            multiplier = None
        
        profit = payout - amount
        results[uid] = {"payout": payout, "profit": profit, "bet": amount, "side": winning_side, "multiplier": multiplier}
    
    return results


async def resolve_bets(bet_key: tuple, outcome: str):
    """Resolve all bets for a completed match."""
    if bet_key not in active_bets:
        return
    
    bet_data = active_bets.pop(bet_key)
    
    win_pool = sum(bet_data["bets"]["win"].values())
    loss_pool = sum(bet_data["bets"]["loss"].values())
    total_pool = win_pool + loss_pool
    
    if total_pool == 0:
        print(f"🎰 No bets placed for {bet_data['player_name']}")
        return
    
    # Calculate payouts
    payouts = calculate_payouts(bet_data, outcome)
    
    # Apply payouts
    for uid, result in payouts.items():
        if result["payout"] > 0:
            update_balance(uid, result["payout"])
    
    # Create results embed
    guild = bot.get_guild(bet_data["guild_id"])
    channel_id = announcement_channels.get(bet_data["guild_id"])
    
    if not guild or not channel_id:
        return
    
    channel = guild.get_channel(channel_id)
    if not channel:
        return
    
    outcome_emoji = "🏆" if outcome == "win" else "💀"
    outcome_text = "WON" if outcome == "win" else "LOST"
    
    embed = discord.Embed(
        title=f"🎰 Betting Results: {bet_data['player_name']} {outcome_emoji}",
        description=f"**{bet_data['player_riot_id']}** {outcome_text} their match!",
        color=discord.Color.green() if outcome == "win" else discord.Color.red(),
        timestamp=datetime.now(timezone.utc)
    )
    
    # Sort by profit
    sorted_results = sorted(payouts.items(), key=lambda x: x[1]["profit"], reverse=True)
    
    winners_text = []
    losers_text = []
    mentions = []
    
    for uid, result in sorted_results:
        try:
            member = guild.get_member(int(uid))
            if member:
                mentions.append(member.mention)
                name = member.display_name
            else:
                name = f"User {uid[:8]}"
        except:
            name = f"User {uid[:8]}"
        
        multiplier_text = f" (🎲 {result['multiplier']}x)" if result.get('multiplier') else ""
        
        if result["profit"] > 0:
            winners_text.append(f"🤑 **{name}**: +{result['profit']} coins{multiplier_text} (bet {result['bet']} on {result['side']})")
        elif result["profit"] == 0:
            winners_text.append(f"😐 **{name}**: ±0 coins (bet {result['bet']} on {result['side']})")
        else:
            losers_text.append(f"😭 **{name}**: {result['profit']} coins (bet {result['bet']} on {result['side']})")
    
    if winners_text:
        embed.add_field(name="Winners", value="\n".join(winners_text[:10]) or "None", inline=False)
    if losers_text:
        embed.add_field(name="Losers", value="\n".join(losers_text[:10]) or "None", inline=False)
    
    # Check if house gave bonus or took cut
    win_bettors = len(bet_data["bets"]["win"])
    loss_bettors = len(bet_data["bets"]["loss"])
    winning_side_count = win_bettors if outcome == "win" else loss_bettors
    losing_side_count = loss_bettors if outcome == "win" else win_bettors
    
    if losing_side_count == 0 or (win_bettors + loss_bettors) == 1:
        # House gave bonus
        embed.set_footer(text=f"Total pool: {total_pool} coins • House bonus paid out 🎁")
    elif total_pool >= 100:
        house_take = int(total_pool * 0.05)
        embed.set_footer(text=f"Total pool: {total_pool} coins • House took: {house_take} coins")
    else:
        embed.set_footer(text=f"Total pool: {total_pool} coins")
    
    # Ping all bettors
    ping_text = " ".join(mentions) if mentions else ""
    await channel.send(content=ping_text, embed=embed)
    print(f"🎰 Bets resolved for {bet_data['player_name']}: {outcome}")


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


# ==================== BETTING COMMANDS ====================

@bot.tree.command(name="bet", description="Place a bet on a player's match")
@app_commands.describe(
    player="The player to bet on",
    outcome="Bet on win or loss",
    amount="Amount of coins to bet"
)
@app_commands.choices(outcome=[
    app_commands.Choice(name="Win", value="win"),
    app_commands.Choice(name="Loss", value="loss"),
])
async def bet(interaction: discord.Interaction, player: discord.Member, outcome: str, amount: int):
    """Place a bet on a player's match."""
    user_id = str(interaction.user.id)
    bet_key = (interaction.guild.id, str(player.id))
    
    # Check if betting is open for this player
    if bet_key not in active_bets:
        await interaction.response.send_message(
            f"❌ No active betting for **{player.display_name}**. They need to be in a match!",
            ephemeral=True
        )
        return
    
    bet_data = active_bets[bet_key]
    
    # Check if betting is closed
    if bet_data.get("closed") or datetime.now(timezone.utc).timestamp() > bet_data["closes_at"]:
        await interaction.response.send_message(
            f"❌ Betting is closed for **{player.display_name}**'s match!",
            ephemeral=True
        )
        return
    
    # Validate amount
    if amount <= 0:
        await interaction.response.send_message("❌ Bet amount must be positive!", ephemeral=True)
        return
    
    balance = get_balance(user_id)
    if amount > balance:
        await interaction.response.send_message(
            f"❌ You only have **{balance}** coins! Use `/balance` to check.",
            ephemeral=True
        )
        return
    
    # Check if user already bet
    existing_bet = None
    for side in ["win", "loss"]:
        if user_id in bet_data["bets"][side]:
            existing_bet = (side, bet_data["bets"][side][user_id])
            break
    
    if existing_bet:
        await interaction.response.send_message(
            f"❌ You already bet **{existing_bet[1]}** coins on **{existing_bet[0]}**! "
            f"One bet per match.",
            ephemeral=True
        )
        return
    
    # Place the bet
    update_balance(user_id, -amount)
    bet_data["bets"][outcome][user_id] = amount
    
    # Update the betting embed
    await update_betting_embed(bet_key)
    
    # Calculate potential payout
    win_pool = sum(bet_data["bets"]["win"].values())
    loss_pool = sum(bet_data["bets"]["loss"].values())
    total_pool = win_pool + loss_pool
    my_pool = win_pool if outcome == "win" else loss_pool
    
    # House takes 5% only on pools >= 100
    house_mult = 0.95 if total_pool >= 100 else 1.0
    
    if my_pool > 0:
        potential_multiplier = (total_pool * house_mult) / my_pool
        potential_payout = int(amount * potential_multiplier)
    else:
        potential_payout = amount
    
    new_balance = get_balance(user_id)
    
    multiplier_note = "\n🎲 **Win bets get 1.05-1.2x random bonus!**" if outcome == "win" else ""
    
    await interaction.response.send_message(
        f"✅ Bet placed!\n\n"
        f"**{amount}** coins on **{player.display_name}** to **{outcome.upper()}**\n"
        f"Potential payout: ~**{potential_payout}** coins (odds may change){multiplier_note}\n"
        f"Your balance: **{new_balance}** coins",
        ephemeral=True
    )
    
    print(f"🎰 {interaction.user.display_name} bet {amount} on {player.display_name} to {outcome}")


@bot.tree.command(name="balance", description="Check your coin balance")
async def balance(interaction: discord.Interaction):
    """Check your coin balance."""
    user_id = str(interaction.user.id)
    bal = get_balance(user_id)
    
    # Check if daily is available
    user_bal = user_balances.get(user_id, {})
    vc_minutes = user_bal.get("vc_minutes_today", 0)
    daily_claimed = user_bal.get("daily_claimed", False)
    
    embed = discord.Embed(
        title=f"💰 {interaction.user.display_name}'s Balance",
        color=discord.Color.gold()
    )
    embed.add_field(name="Coins", value=f"**{bal}** 🪙", inline=True)
    embed.add_field(name="VC Time Today", value=f"{vc_minutes} min", inline=True)
    
    if daily_claimed:
        embed.add_field(name="Daily Bonus", value="✅ Claimed", inline=True)
    else:
        remaining = max(0, VC_MINUTES_FOR_DAILY - vc_minutes)
        embed.add_field(name="Daily Bonus", value=f"⏳ {remaining} more min in VC", inline=True)
    
    embed.set_footer(text="Daily bonus auto-claims when you leave VC with 30+ min")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="leaderboard", description="View the coin leaderboard")
async def leaderboard(interaction: discord.Interaction):
    """Show top coin holders."""
    if not user_balances:
        await interaction.response.send_message("No one has any coins yet!", ephemeral=True)
        return
    
    # Sort by balance
    sorted_users = sorted(
        user_balances.items(),
        key=lambda x: x[1].get("balance", 0),
        reverse=True
    )[:10]
    
    embed = discord.Embed(
        title="🏆 Coin Leaderboard",
        color=discord.Color.gold()
    )
    
    lines = []
    for i, (uid, data) in enumerate(sorted_users, 1):
        try:
            member = interaction.guild.get_member(int(uid))
            name = member.display_name if member else f"User {uid[:8]}"
        except:
            name = f"User {uid[:8]}"
        
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        lines.append(f"{medal} **{name}**: {data.get('balance', 0)} coins")
    
    embed.description = "\n".join(lines) or "No users found"
    
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="set", description="Set a user's coin balance (Admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def set_coins(interaction: discord.Interaction, user: discord.Member, amount: int):
    """Set a user's coin balance (admin only)."""
    user_id = str(user.id)
    new_balance = set_balance(user_id, amount)
    
    await interaction.response.send_message(
        f"✅ Set **{user.display_name}**'s balance to **{new_balance}** coins"
    )
    
    print(f"💰 Admin {interaction.user.display_name} set {user.display_name}'s balance to {new_balance}")


@bot.tree.command(name="rules", description="Show bot rules and commands")
async def rules(interaction: discord.Interaction):
    """Display bot rules and commands."""
    embed = discord.Embed(
        title="📜 Post Plant Bot Rules & Commands",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="🎮 Match Tracking",
        value=(
            "• `/register <RiotName> <TAG> <region>` - Register your Riot ID\n"
            "• `/unregister` - Stop tracking your matches\n"
            "• `/stats` - Check your recent stats\n"
            "• `/status` - See who's being tracked"
        ),
        inline=False
    )
    
    embed.add_field(
        name="🎰 Betting System",
        value=(
            "• Everyone starts with **100 coins**\n"
            "• When someone starts a match, betting opens for 3 min\n"
            "• `/bet <player> <win|loss> <amount>` - Place a bet\n"
            "• Win bets get a **1.05-1.2x bonus** multiplier!\n"
            "• `/balance` - Check your coins\n"
            "• `/leaderboard` - See top coin holders"
        ),
        inline=False
    )
    
    embed.add_field(
        name="💰 Earning Coins",
        value=(
            "• Spend **30 min in voice chat** → Auto-claim **50 coins** daily\n"
            "• Win bets on your friends!"
        ),
        inline=False
    )
    
    embed.add_field(
        name="🏠 House Rules",
        value=(
            "• House takes **5%** only on pools of **100+ coins**\n"
            "• Solo bets & unanimous bets get **bonus payouts**\n"
            "• No house cut when there's no loser"
        ),
        inline=False
    )
    
    embed.set_footer(text="Good luck, have fun! 🎯")
    
    await interaction.response.send_message(embed=embed)


# Run the bot
if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        print("❌ Please set the DISCORD_BOT_TOKEN environment variable")
        exit(1)
    
    bot.run(token)