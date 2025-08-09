import os
import asyncio
from dataclasses import dataclass
from typing import Dict, Optional
import yt_dlp
import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask
from threading import Thread
keep_alive()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN env var missing")

COOKIES_PATH = os.getenv("COOKIES_PATH")  # e.g. /opt/render/project/src/cookies.txt or None

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0",
    "nocheckcertificate": True,
}
if COOKIES_PATH and os.path.isfile(COOKIES_PATH):
    YTDL_OPTIONS["cookiefile"] = COOKIES_PATH

FFMPEG_BEFORE_OPTS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTS = "-vn"

intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)

@dataclass
class Song:
    title: str
    stream_url: str
    requester: str

class GuildMusic:
    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.play_next: asyncio.Event = asyncio.Event()
        self.current: Optional[Song] = None
        self.voice_client: Optional[discord.VoiceClient] = None
        self.volume: float = 0.5
        self.player_task: Optional[asyncio.Task] = None
        self._stop = False

    async def player_loop(self):
        while not self._stop:
            self.play_next.clear()
            try:
                song: Song = await asyncio.wait_for(self.queue.get(), timeout=300)
            except asyncio.TimeoutError:
                if self.voice_client and self.voice_client.is_connected():
                    await self.voice_client.disconnect()
                break

            self.current = song
            if not self.voice_client or not self.voice_client.is_connected():
                break

            source = discord.FFmpegPCMAudio(
                song.stream_url,
                before_options=FFMPEG_BEFORE_OPTS,
                options=FFMPEG_OPTS,
            )
            player = discord.PCMVolumeTransformer(source, volume=self.volume)

            loop = asyncio.get_running_loop()
            def after_playing(err):
                if err:
                    print(f"Playback error: {err}")
                loop.call_soon_threadsafe(self.play_next.set)

            self.voice_client.play(player, after=after_playing)
            await self.play_next.wait()
            self.current = None
            await asyncio.sleep(0.2)

    async def stop(self):
        self._stop = True
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.disconnect()
        if self.player_task:
            self.player_task.cancel()

guild_players: Dict[int, GuildMusic] = {}

async def ensure_player(guild_id: int) -> GuildMusic:
    if guild_id not in guild_players:
        guild_players[guild_id] = GuildMusic()
    return guild_players[guild_id]

async def fetch_info(query: str):
    loop = asyncio.get_running_loop()
    def extract():
        return ytdl.extract_info(query, download=False)
    data = await loop.run_in_executor(None, extract)
    if "entries" in data:
        data = data["entries"][0]
    return data

def select_audio_url(data: dict) -> str:
    formats = data.get("formats", [])
    for f in reversed(formats):
        if f.get("acodec") and f.get("acodec") != "none" and (not f.get("vcodec") or f.get("vcodec") == "none"):
            if f.get("url"):
                return f["url"]
    return data.get("url")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        await bot.tree.sync()
        print("Commands synced globally.")
    except Exception as e:
        print(f"Sync error: {e}")

@bot.event
async def on_guild_join(guild):
    try:
        await bot.tree.sync(guild=guild)
        print(f"Synced commands to guild: {guild.name} ({guild.id})")
    except Exception as e:
        print(f"Guild join sync error: {e}")

@bot.tree.command(name="play")
@app_commands.describe(query="YouTube URL or search keywords")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    voice_state = interaction.user.voice
    if not voice_state or not voice_state.channel:
        await interaction.followup.send("❌ You need to be in a voice channel.", ephemeral=True)
        return

    gp = await ensure_player(interaction.guild_id)
    if not interaction.guild.voice_client:
        try:
            gp.voice_client = await voice_state.channel.connect()
            gp.player_task = asyncio.create_task(gp.player_loop())
        except Exception as e:
            await interaction.followup.send(f"❌ Could not connect to voice channel: {e}")
            return
    else:
        gp.voice_client = interaction.guild.voice_client

    try:
        data = await fetch_info(query)
    except Exception as e:
        await interaction.followup.send(f"❌ Error fetching video info: {e}")
        return

    try:
        stream_url = select_audio_url(data)
        title = data.get("title", "Unknown")
    except Exception as e:
        await interaction.followup.send(f"❌ Error preparing stream: {e}")
        return

    song = Song(title=title, stream_url=stream_url, requester=str(interaction.user))
    await gp.queue.put(song)
    await interaction.followup.send(f"✅ Queued **{song.title}** — requested by {song.requester}")

@bot.tree.command(name="skip")
async def skip(interaction: discord.Interaction):
    gp = guild_players.get(interaction.guild_id)
    if not gp or not gp.voice_client:
        await interaction.response.send_message("❌ I'm not connected to voice.", ephemeral=True)
        return
    if gp.voice_client.is_playing():
        gp.voice_client.stop()
        await interaction.response.send_message("⏭ Skipped current song.")
    else:
        await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)

@bot.tree.command(name="stop")
async def stop(interaction: discord.Interaction):
    gp = guild_players.get(interaction.guild_id)
    if not gp or not gp.voice_client:
        await interaction.response.send_message("❌ I'm not connected to voice.", ephemeral=True)
        return
    gp.queue = asyncio.Queue()
    if gp.voice_client.is_playing():
        gp.voice_client.stop()
    await gp.voice_client.disconnect()
    await interaction.response.send_message("⏹ Stopped playback and left the voice channel.")

@bot.tree.command(name="pause")
async def pause(interaction: discord.Interaction):
    gp = guild_players.get(interaction.guild_id)
    if not gp or not gp.voice_client or not gp.voice_client.is_playing():
        await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
        return
    gp.voice_client.pause()
    await interaction.response.send_message("⏸ Paused playback.")

@bot.tree.command(name="resume")
async def resume(interaction: discord.Interaction):
    gp = guild_players.get(interaction.guild_id)
    if not gp or not gp.voice_client or not gp.voice_client.is_paused():
        await interaction.response.send_message("❌ Nothing is paused.", ephemeral=True)
        return
    gp.voice_client.resume()
    await interaction.response.send_message("▶ Resumed playback.")

app = Flask("")

@app.route("/")
def home():
    return "Bot is alive!"

def run():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    thread = Thread(target=run)
    thread.start()

if __name__ == "__main__":
    bot.run(TOKEN)
