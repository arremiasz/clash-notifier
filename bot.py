import discord
from discord.ext import commands, tasks
from discord.ui import Button, View, Select
import requests
import datetime
import asyncio
import os
import json

# --- CONFIGURATION ---
RIOT_API_KEY = os.getenv('RIOT_API_KEY')
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
# ANNOUNCEMENT_CHANNEL_ID is no longer hardcoded. Use /setclashchannel command.
RIOT_REGION = os.getenv('RIOT_REGION', 'na1')
PING_ROLE = "@everyone"
DATA_FILE = "clash_state.json"

# --- GLOBAL STATE ---
# Stores configuration and RSVPs for all guilds
# Structure: { 'guilds': { 'GUILD_ID': { 'channel_id': 123, 'message_id': 456, 'tournament_id': '...', 'saturday': {...}, 'sunday': {...} } } }
CLASH_STATE = {'guilds': {}}

# --- SETUP ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)


# --- PERSISTENCE HELPERS ---
def load_state():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
                # Ensure root structure exists
                if 'guilds' not in data:
                    return {'guilds': {}}
                return data
        except json.JSONDecodeError:
            return {'guilds': {}}
    return {'guilds': {}}


def save_state(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)


# --- RIOT API FUNCTIONS ---
def get_upcoming_clash_tournaments():
    url = f"https://{RIOT_REGION}.api.riotgames.com/lol/clash/v1/tournaments"
    headers = {"X-Riot-Token": RIOT_API_KEY}
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            tournaments = response.json()
            upcoming = []
            current_time = datetime.datetime.now().timestamp() * 1000

            for tournament in tournaments:
                for day in tournament.get('schedule', []):
                    if day['startTime'] > current_time:
                        day['tournament_id'] = tournament['id']  # Store ID for uniqueness check
                        day['name'] = tournament['nameKey'].replace('_', ' ').title()
                        day['secondary_name'] = tournament['nameKeySecondary'].replace('_', ' ').title()
                        upcoming.append(day)

            upcoming.sort(key=lambda x: x['startTime'])
            return upcoming
        else:
            print(f"Error fetching data: {response.status_code} - {response.text}")
            return []
    except Exception as e:
        print(f"Exception during API call: {e}")
        return []


# --- DISCORD UI ---

class RoleSelect(Select):
    def __init__(self, day, parent_view, main_message):
        self.day = day
        self.parent_view = parent_view
        self.main_message = main_message

        options = [
            discord.SelectOption(label="Top", emoji="🛡️"),
            discord.SelectOption(label="Jungle", emoji="🌲"),
            discord.SelectOption(label="Mid", emoji="🔮"),
            discord.SelectOption(label="Bot", emoji="🏹"),
            discord.SelectOption(label="Support", emoji="🩹"),
            discord.SelectOption(label="Fill", emoji="🔄"),
        ]

        super().__init__(placeholder=f"Select roles for {day}...", min_values=1, max_values=6, options=options)

    async def callback(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        selected_roles = self.values

        role_order = ["Top", "Jungle", "Mid", "Bot", "Support", "Fill"]
        selected_roles.sort(key=lambda r: role_order.index(r) if r in role_order else 99)
        roles_display = ", ".join(selected_roles)

        # Update persistent state
        if self.day == "Saturday":
            self.parent_view.state['saturday'][user_id] = roles_display
        else:
            self.parent_view.state['sunday'][user_id] = roles_display

        self.parent_view.save_current_state()

        new_embed = self.parent_view.update_embed(self.main_message.embeds[0])
        await self.main_message.edit(embed=new_embed)
        await interaction.response.edit_message(content=f"✅ Registered for {self.day} as: {roles_display}",
                                                view=self.view)


class EphemeralRSVPView(View):
    def __init__(self, day, parent_view, main_message):
        super().__init__(timeout=60)
        self.day = day
        self.parent_view = parent_view
        self.main_message = main_message
        self.add_item(RoleSelect(day, parent_view, main_message))

    @discord.ui.button(label="Remove Me ❌", style=discord.ButtonStyle.red)
    async def remove_button(self, interaction: discord.Interaction, button: Button):
        user_id = str(interaction.user.id)

        removed = False
        if self.day == "Saturday" and user_id in self.parent_view.state['saturday']:
            del self.parent_view.state['saturday'][user_id]
            removed = True
        elif self.day == "Sunday" and user_id in self.parent_view.state['sunday']:
            del self.parent_view.state['sunday'][user_id]
            removed = True

        if removed:
            self.parent_view.save_current_state()
            new_embed = self.parent_view.update_embed(self.main_message.embeds[0])
            await self.main_message.edit(embed=new_embed)
            await interaction.response.edit_message(content=f"🗑️ Removed from {self.day}.", view=self)
        else:
            await interaction.response.edit_message(content=f"You weren't signed up for {self.day}.", view=self)


class RSVPView(View):
    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = str(guild_id)

    @property
    def state(self):
        # Retrieve the specific state for this guild from the global object
        # Initialize if missing (safety check)
        if self.guild_id not in CLASH_STATE['guilds']:
            CLASH_STATE['guilds'][self.guild_id] = {
                'channel_id': None,
                'message_id': None,
                'tournament_id': None,
                'saturday': {},
                'sunday': {}
            }
        return CLASH_STATE['guilds'][self.guild_id]

    def save_current_state(self):
        save_state(CLASH_STATE)

    def update_embed(self, original_embed):
        def format_list(user_dict):
            if not user_dict:
                return "No one yet."
            lines = []
            for uid, roles in user_dict.items():
                lines.append(f"<@{uid}> *({roles})*")
            return "\n".join(lines)

        sat_str = format_list(self.state['saturday'])
        sun_str = format_list(self.state['sunday'])

        original_embed.set_field_at(1, name=f"🛰️ Saturday ({len(self.state['saturday'])})", value=sat_str, inline=True)
        original_embed.set_field_at(2, name=f"🌞 Sunday ({len(self.state['sunday'])})", value=sun_str, inline=True)
        return original_embed

    @discord.ui.button(label="🛰️ Saturday", style=discord.ButtonStyle.blurple, custom_id="rsvp_saturday")
    async def saturday_button(self, interaction: discord.Interaction, button: Button):
        view = EphemeralRSVPView("Saturday", self, interaction.message)
        await interaction.response.send_message("Select your roles for **Saturday**:", view=view, ephemeral=True)

    @discord.ui.button(label="🌞 Sunday", style=discord.ButtonStyle.blurple, custom_id="rsvp_sunday")
    async def sunday_button(self, interaction: discord.Interaction, button: Button):
        view = EphemeralRSVPView("Sunday", self, interaction.message)
        await interaction.response.send_message("Select your roles for **Sunday**:", view=view, ephemeral=True)


# --- BOT EVENTS ---
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')

    global CLASH_STATE
    CLASH_STATE = load_state()

    # Restore views for all guilds
    print("Restoring views...")
    for guild_id, data in CLASH_STATE['guilds'].items():
        if data.get('message_id'):
            # Check if this guild still exists in the bot's cache
            view = RSVPView(guild_id)
            bot.add_view(view, message_id=data['message_id'])

    # Sync Slash Commands
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

    # Start loop if not already running
    if not check_clash_schedule.is_running():
        check_clash_schedule.start()


@bot.tree.command(name="setclashchannel", description="Set the current channel for Clash announcements")
async def set_clash_channel(interaction: discord.Interaction):
    # Check for admin permissions
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need Administrator permissions to use this.", ephemeral=True)
        return

    guild_id = str(interaction.guild_id)

    if guild_id not in CLASH_STATE['guilds']:
        CLASH_STATE['guilds'][guild_id] = {
            'saturday': {},
            'sunday': {},
            'tournament_id': None,
            'message_id': None
        }

    CLASH_STATE['guilds'][guild_id]['channel_id'] = interaction.channel_id
    save_state(CLASH_STATE)

    await interaction.response.send_message(f"✅ Clash announcements will now be posted in <#{interaction.channel_id}>.")


@bot.tree.command(name="checkclash", description="Manually check for upcoming Clash tournaments")
async def checkclash(interaction: discord.Interaction):
    # Check for admin permissions
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need Administrator permissions to use this.", ephemeral=True)
        return

    await interaction.response.send_message(f"Manually checking...")
    # Call the logic ONLY for this guild
    await core_clash_check(target_guild_id=interaction.guild_id)

@bot.tree.command(name="listtournaments", description="List tournaments from the Riot API")
async def list_tournaments(interaction: discord.Interaction):
    if interaction.user.id == "271789786883293195":
        await interaction.response.send_message(get_upcoming_clash_tournaments(), ephemeral=True)

@tasks.loop(hours=24)
async def check_clash_schedule():
    # Regular loop calls logic for ALL guilds
    await core_clash_check()


async def core_clash_check(target_guild_id=None):
    """
    Core logic to check API and update Discord.
    If target_guild_id is provided, only updates that specific guild.
    """
    print("Checking for Clash tournaments...")
    tournaments = get_upcoming_clash_tournaments()

    if not tournaments:
        return

    # 1. Determine Window
    next_tournament = tournaments[0]
    first_start_time = next_tournament['startTime']
    cutoff_time = first_start_time + (7 * 24 * 60 * 60 * 1000)

    # 2. Find Related Days
    related_days = [t for t in tournaments if t['startTime'] <= cutoff_time]

    # 3. Generate Composite ID
    related_ids = sorted([str(t['tournament_id']) for t in related_days])
    composite_id = "_".join(related_ids)

    print(f"Current Event ID: {composite_id}")

    # --- Generate Content (Same for all guilds) ---
    display_dates = []
    seen_dates = set()
    for day in related_days:
        ts = int(day['registrationTime'] / 1000)
        date_tag = f"<t:{ts}:D>"
        if date_tag not in seen_dates:
            seen_dates.add(date_tag)
            display_dates.append(date_tag)
    dates_str = " & ".join(display_dates)

    reg_timestamp = int(next_tournament['registrationTime'] / 1000)
    start_timestamp = int(next_tournament['startTime'] / 1000)
    t4_ts = reg_timestamp
    t3_ts = reg_timestamp + (45 * 60)
    t2_ts = reg_timestamp + (90 * 60)
    t1_ts = reg_timestamp + (120 * 60)

    time_schedule = (
        f"**Tier IV:** <t:{t4_ts}:t>\n"
        f"**Tier III:** <t:{t3_ts}:t>\n"
        f"**Tier II:** <t:{t2_ts}:t>\n"
        f"**Tier I:** <t:{t1_ts}:t>\n"
        f"**Lock-in Closes:** <t:{start_timestamp}:t>"
    )

    base_embed = discord.Embed(
        title=f"🏆 Clash Alert: {next_tournament['name']} Cup",
        description=f"The next Clash is coming up!\n📅 **Dates:** {dates_str}\n\nRegister your availability below.",
        color=discord.Color.gold()
    )
    base_embed.add_field(name="⏰ Lock-In Schedule", value=time_schedule, inline=False)
    # Placeholder fields (will be overwritten by View updates)
    base_embed.add_field(name="🛰️ Saturday (0)", value="No one yet.", inline=True)
    base_embed.add_field(name="🌞 Sunday (0)", value="No one yet.", inline=True)
    base_embed.set_thumbnail(
        url="https://raw.communitydragon.org/latest/plugins/rcp-fe-lol-clash/global/default/assets/images/trophy.png")

    # --- Process Guilds ---
    # We iterate over a copy of items to avoid modification issues

    # Identify which guilds to process
    guilds_to_process = []
    if target_guild_id:
        g = bot.get_guild(int(target_guild_id))
        if g: guilds_to_process.append(g)
    else:
        guilds_to_process = bot.guilds

    for guild in guilds_to_process:
        guild_id = str(guild.id)

        # Ensure guild entry exists in state
        if guild_id not in CLASH_STATE['guilds']:
            CLASH_STATE['guilds'][guild_id] = {
                'channel_id': None,
                'message_id': None,
                'tournament_id': None,
                'saturday': {},
                'sunday': {}
            }

        guild_data = CLASH_STATE['guilds'][guild_id]
        channel_id = guild_data.get('channel_id')

        channel = None
        if channel_id:
            channel = bot.get_channel(channel_id)

        # Fallback if no channel set or channel deleted
        if not channel:
            # Try system channel
            if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
                channel = guild.system_channel
            else:
                # Find first text channel with permissions (preferring 'general', 'clash' etc)
                # 1. Look for specific names
                for c in guild.text_channels:
                    if c.name in ['general', 'clash', 'league', 'announcements'] and c.permissions_for(
                            guild.me).send_messages:
                        channel = c
                        break
                # 2. Look for ANY valid channel
                if not channel:
                    for c in guild.text_channels:
                        if c.permissions_for(guild.me).send_messages:
                            channel = c
                            break

        if not channel:
            print(f"No suitable channel found for guild {guild.name} ({guild_id}). Skipping.")
            continue

        # Check if this guild already has this tournament announced
        current_event_id = guild_data.get('tournament_id')

        if current_event_id == composite_id:
            # Up to date
            if target_guild_id:
                print(f"Guild {guild_id} is already up to date.")
            continue

        print(f"Posting/Updating for Guild {guild_id}")

        # Check for update vs new
        old_id_str = current_event_id or ''
        old_ids = set(old_id_str.split('_')) if old_id_str else set()
        new_ids = set(related_ids)
        is_update = not old_ids.isdisjoint(new_ids) and guild_data.get('message_id')

        view = RSVPView(guild_id)

        if is_update:
            try:
                # Update existing message
                guild_data['tournament_id'] = composite_id  # Update ID
                msg = await channel.fetch_message(guild_data['message_id'])

                # Update embed content
                updated_embed = view.update_embed(base_embed)
                await msg.edit(embed=updated_embed, view=view)
                continue
            except discord.NotFound:
                print(f"Message not found in guild {guild_id}, posting new.")
                # Fall through to new post

        # New Post
        # Reset RSVPs for this guild for the new event
        guild_data['saturday'] = {}
        guild_data['sunday'] = {}
        guild_data['tournament_id'] = composite_id

        # Prepare embed with empty RSVPs
        updated_embed = view.update_embed(base_embed)

        try:
            message = await channel.send(content=f"{PING_ROLE} New Clash Tournament detected!", embed=updated_embed,
                                         view=view)
            guild_data['message_id'] = message.id
        except discord.Forbidden:
            print(f"Missing permissions in guild {guild_id}")

    # Save all changes
    save_state(CLASH_STATE)


@check_clash_schedule.before_loop
async def before_check():
    await bot.wait_until_ready()

    # Calculate delay to run at a specific time (e.g., 14:00 UTC)
    now = datetime.datetime.now(datetime.timezone.utc)
    # Target time: 14:00 UTC (Adjust as needed)
    target_time = now.replace(hour=18, minute=0, second=0, microsecond=0)

    if now > target_time:
        # If we passed today's target time, schedule for tomorrow
        target_time += datetime.timedelta(days=1)

    delay_seconds = (target_time - now).total_seconds()
    print(
        f"Scheduling first automatic check in {delay_seconds / 3600:.2f} hours (at {target_time.strftime('%H:%M UTC')})...")
    await asyncio.sleep(delay_seconds)


bot.run(DISCORD_TOKEN)
