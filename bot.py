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
ADMIN_USER_ID = 271789786883293195

# --- GLOBAL STATE ---
# Structure: { 
#   'guilds': { 'GUILD_ID': { ... } }, 
#   'days': [ ... ],
#   'approved_ids': [ ... ],
#   'pending_ids': [ ... ]
# }
CLASH_STATE = {'guilds': {}, 'days': [], 'approved_ids': [], 'pending_ids': []}

# --- SETUP ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)


# --- PERSISTENCE HELPERS ---
def load_state():
    """Loads state from JSON, ensuring all keys exist."""
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
                
                # Ensure root structures exist (Migration/Safety)
                if 'guilds' not in data: data['guilds'] = {}
                if 'days' not in data: data['days'] = []
                if 'approved_ids' not in data: data['approved_ids'] = []
                if 'pending_ids' not in data: data['pending_ids'] = []
                    
                return data
        except json.JSONDecodeError:
            return {'guilds': {}, 'days': [], 'approved_ids': [], 'pending_ids': []}
    return {'guilds': {}, 'days': [], 'approved_ids': [], 'pending_ids': []}

def save_state(data):
    """Saves state to JSON file."""
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
                        day['tournament_id'] = tournament['id']
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

        if self.day == "Saturday":
            self.parent_view.state['saturday'][user_id] = roles_display
        else:
            self.parent_view.state['sunday'][user_id] = roles_display

        self.parent_view.save_current_state()
        new_embed = self.parent_view.update_embed(self.main_message.embeds[0])
        await self.main_message.edit(embed=new_embed)
        await interaction.response.edit_message(content=f"✅ Registered for {self.day} as: {roles_display}", view=self.view)

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
            if not user_dict: return "No one yet."
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

# --- ADMIN APPROVAL VIEW ---
class AdminApprovalView(View):
    def __init__(self, composite_id, embed, related_ids):
        super().__init__(timeout=None)
        self.composite_id = composite_id
        self.embed = embed
        self.related_ids = related_ids

    @discord.ui.button(label="✅ Approve Broadcast", style=discord.ButtonStyle.green)
    async def approve(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != ADMIN_USER_ID: return

        # Update State
        if self.composite_id not in CLASH_STATE['approved_ids']:
            CLASH_STATE['approved_ids'].append(self.composite_id)
        
        if self.composite_id in CLASH_STATE['pending_ids']:
            CLASH_STATE['pending_ids'].remove(self.composite_id)
            
        save_state(CLASH_STATE)

        await interaction.response.edit_message(content=f"✅ **Approved!** Broadcasting to all servers...", view=None)
        print(f"Admin approved event {self.composite_id}. Starting broadcast...")
        
        # Trigger Broadcast
        await broadcast_to_guilds(self.composite_id, self.embed, self.related_ids)

    @discord.ui.button(label="❌ Reject / Ignore", style=discord.ButtonStyle.red)
    async def reject(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != ADMIN_USER_ID: return
        
        # We don't verify rejection, just leave it pending or ignore
        await interaction.response.edit_message(content=f"❌ **Rejected.** I will not broadcast this event.", view=None)
        print(f"Admin rejected event {self.composite_id}.")

# --- BOT EVENTS ---
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    global CLASH_STATE
    CLASH_STATE = load_state()

    print("Restoring views...")
    for guild_id, data in CLASH_STATE['guilds'].items():
        if data.get('message_id'):
            view = RSVPView(guild_id)
            bot.add_view(view, message_id=data['message_id'])

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

    if not check_clash_schedule.is_running():
        check_clash_schedule.start()

@bot.tree.command(name="setclashchannel", description="Set the current channel for Clash announcements")
async def set_clash_channel(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need Administrator permissions to use this.", ephemeral=True)
        return

    guild_id = str(interaction.guild_id)
    if guild_id not in CLASH_STATE['guilds']:
        CLASH_STATE['guilds'][guild_id] = {
            'saturday': {}, 'sunday': {}, 
            'tournament_id': None, 'message_id': None
        }
    
    CLASH_STATE['guilds'][guild_id]['channel_id'] = interaction.channel_id
    save_state(CLASH_STATE)
    await interaction.response.send_message(f"✅ Clash announcements will now be posted in <#{interaction.channel_id}>.")

@bot.tree.command(name="checkclash", description="Manually check for upcoming Clash tournaments")
async def checkclash(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need Administrator permissions to use this.", ephemeral=True)
        return

    await interaction.response.send_message(f"Checking API and pending approvals...")
    await core_clash_check()

@bot.tree.command(name="listtournaments", description="List tournaments from the Riot API")
async def list_tournaments(interaction: discord.Interaction):
    if interaction.user.id == ADMIN_USER_ID:
        await interaction.response.send_message(str(get_upcoming_clash_tournaments())[:2000], ephemeral=True)
    else:
        await interaction.response.send_message("Restricted command.", ephemeral=True)

@tasks.loop(hours=24)
async def check_clash_schedule():
    await core_clash_check()

@check_clash_schedule.before_loop
async def before_check():
    await bot.wait_until_ready()
    now = datetime.datetime.now(datetime.timezone.utc)
    target_time = now.replace(hour=22, minute=0, second=0, microsecond=0)
    if now > target_time:
        target_time += datetime.timedelta(days=1)
    delay_seconds = (target_time - now).total_seconds()
    print(f"Scheduling first automatic check in {delay_seconds/3600:.2f} hours (at {target_time.strftime('%H:%M UTC')})...")
    await asyncio.sleep(delay_seconds)

async def core_clash_check(target_guild_id=None):
    print("Checking for Clash tournaments...")
    tournaments = get_upcoming_clash_tournaments()

    if not tournaments:
        CLASH_STATE['days'] = []
        save_state(CLASH_STATE)
        return

    # Update 'days' list so we track what we've seen, but don't stop execution
    for t in tournaments:
        if t['id'] not in CLASH_STATE['days']:
            CLASH_STATE['days'].append(t['id'])
    
    # 1. Determine Window
    next_tournament = tournaments[0]
    first_start_time = next_tournament['startTime']
    cutoff_time = first_start_time + (7 * 24 * 60 * 60 * 1000)

    # 2. Find Related Days
    related_days = [t for t in tournaments if t['startTime'] <= cutoff_time]
    related_ids = sorted([str(t['tournament_id']) for t in related_days])
    composite_id = "_".join(related_ids)

    print(f"Current Event ID: {composite_id}")

    # --- Generate Content ---
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
    base_embed.add_field(name="🛰️ Saturday (0)", value="No one yet.", inline=True)
    base_embed.add_field(name="🌞 Sunday (0)", value="No one yet.", inline=True)
    base_embed.set_thumbnail(url="https://raw.communitydragon.org/latest/plugins/rcp-fe-lol-clash/global/default/assets/images/trophy.png")

    # --- APPROVAL LOGIC ---
    if composite_id in CLASH_STATE['approved_ids']:
        # Already approved? Just verify broadcast to guilds (update logic)
        await broadcast_to_guilds(composite_id, base_embed, related_ids, target_guild_id)
    elif composite_id in CLASH_STATE['pending_ids']:
        print(f"Event {composite_id} is pending admin approval.")
    else:
        # New event detected! Ask Admin.
        print(f"New Event {composite_id} detected. Sending DM to Admin...")
        try:
            admin_user = await bot.fetch_user(ADMIN_USER_ID)
            view = AdminApprovalView(composite_id, base_embed, related_ids)
            await admin_user.send(
                content="🚨 **New Clash Tournament Detected!**\nPlease review the data below. If it looks correct, click Approve to broadcast to all servers.",
                embed=base_embed,
                view=view
            )
            CLASH_STATE['pending_ids'].append(composite_id)
            save_state(CLASH_STATE)
        except Exception as e:
            print(f"Failed to DM Admin: {e}")

async def broadcast_to_guilds(composite_id, base_embed, related_ids, target_guild_id=None):
    """
    Broadcasts the approved tournament to all guilds or a specific target.
    """
    print(f"Broadcasting event {composite_id}...")
    
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
                'channel_id': None, 'message_id': None, 
                'tournament_id': None, 'saturday': {}, 'sunday': {}
            }

        guild_data = CLASH_STATE['guilds'][guild_id]
        channel_id = guild_data.get('channel_id')
        channel = None

        if channel_id:
            channel = bot.get_channel(channel_id)

        # Fallback detection
        if not channel:
            if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
                channel = guild.system_channel
            else:
                for c in guild.text_channels:
                    if c.name in ['general', 'clash', 'league', 'announcements'] and c.permissions_for(guild.me).send_messages:
                        channel = c
                        break
                if not channel:
                    for c in guild.text_channels:
                        if c.permissions_for(guild.me).send_messages:
                            channel = c
                            break

        if not channel:
            print(f"No suitable channel found for guild {guild.name} ({guild_id}). Skipping.")
            continue

        current_event_id = guild_data.get('tournament_id')

        if current_event_id == composite_id and not target_guild_id:
            # Already up to date
            continue

        print(f"Posting/Updating for Guild {guild_id}")

        old_id_str = current_event_id or ''
        old_ids = set(old_id_str.split('_')) if old_id_str else set()
        new_ids = set(related_ids)
        is_update = not old_ids.isdisjoint(new_ids) and guild_data.get('message_id')

        view = RSVPView(guild_id)

        if is_update:
            try:
                guild_data['tournament_id'] = composite_id 
                msg = await channel.fetch_message(guild_data['message_id'])
                updated_embed = view.update_embed(base_embed)
                await msg.edit(embed=updated_embed, view=view)
                continue
            except discord.NotFound:
                print(f"Message not found in guild {guild_id}, posting new.")

        # New Post
        guild_data['saturday'] = {}
        guild_data['sunday'] = {}
        guild_data['tournament_id'] = composite_id
        
        updated_embed = view.update_embed(base_embed)

        try:
            message = await channel.send(content=f"{PING_ROLE} New Clash Tournament detected!", embed=updated_embed, view=view)
            guild_data['message_id'] = message.id
        except discord.Forbidden:
            print(f"Missing permissions in guild {guild_id}")

    save_state(CLASH_STATE)

bot.run(DISCORD_TOKEN)