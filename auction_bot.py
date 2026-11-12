"""Persistent mock auction bot for Discord (discord.py 2.4+, Python 3.10+)."""
import json
import logging
import os
import re
import time
from decimal import Decimal
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("auctionbot")
DATA_FILE = Path(os.getenv("AUCTION_DATA_FILE", "data/auctions.json"))
MAX_BID = 10**12
MAX_DURATION = 14 * 86400
SNIPE_SECONDS = 30
MAX_EXTENSIONS = 20


def clean(value: str, limit: int = 80) -> str:
    return " ".join(value.split())[:limit]


def safe(value: str) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(value))


def duration(value: str) -> Optional[int]:
    value = "".join(value.lower().split())
    parts = re.findall(r"(\d+)([smhd])", value)
    if not parts or "".join(n + u for n, u in parts) != value:
        return None
    seconds = sum(int(n) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[u] for n, u in parts)
    return seconds if 10 <= seconds <= MAX_DURATION else None


def amount(value: str) -> Optional[int]:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([km]?)", re.sub(r"[\s,_]", "", value.lower()))
    if not match:
        return None
    number = Decimal(match[1]) * {"": 1, "k": 1000, "m": 1000000}[match[2]]
    return int(number) if number == number.to_integral_value() and 1 <= number <= MAX_BID else None


class Store:
    """JSON-backed auction state. Mount the configured data directory persistently."""
    def __init__(self):
        self.path = DATA_FILE
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.next_id = 1
        self.auctions: dict[int, dict] = {}
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                self.next_id = int(saved.get("next_id", 1))
                self.auctions = {int(k): v for k, v in saved.get("auctions", {}).items()}
            except (OSError, ValueError, TypeError) as exc:
                log.exception("Could not load auction data: %s", exc)
                self.path.replace(self.path.with_suffix(f".corrupt-{int(time.time())}.json"))

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"next_id": self.next_id, "auctions": self.auctions}, indent=2), encoding="utf-8")
        tmp.replace(self.path)


class AuctionBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default(), allowed_mentions=discord.AllowedMentions.none())
        self.tree = app_commands.CommandTree(self)
        self.store = Store()

    async def setup_hook(self):
        self.add_dynamic_items(BidButton)
        guild_id = os.getenv("GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.tick.start()

    async def get_channel_for(self, auction):
        return self.get_channel(auction["channel_id"]) or await self.fetch_channel(auction["channel_id"])

    @tasks.loop(seconds=2)
    async def tick(self):
        now = time.time()
        for aid, auction in list(self.store.auctions.items()):
            if auction["ends_at"] <= now:
                await self.close_auction(aid)

    @tick.before_loop
    async def before_tick(self):
        await self.wait_until_ready()

    async def close_auction(self, aid: int, cancelled: bool = False):
        auction = self.store.auctions.pop(aid, None)
        if auction is None:
            return
        self.store.save()
        try:
            channel = await self.get_channel_for(auction)
        except discord.HTTPException:
            log.exception("Could not access channel for auction #%s", aid)
            return
        sold = [it for it in auction["items"] if it["bidder_id"] is not None]
        state = "cancelled" if cancelled else "ended"
        embed = discord.Embed(title=f"Auction #{aid} · {state.title()}", color=discord.Color.dark_red() if cancelled else discord.Color.dark_grey())
        if cancelled:
            embed.description = "Cancelled by the host. Nobody wins."
        else:
            embed.description = "\n".join(
                f"**{i}. {safe(it['name'])}** · {it['bid']:,} · <@{it['bidder_id']}>" if it["bidder_id"] else f"**{i}. {safe(it['name'])}** · unsold"
                for i, it in enumerate(auction["items"], 1)
            )[:4000]
            embed.add_field(name="Sold", value=f"{len(sold)} of {len(auction['items'])}")
        try:
            await channel.get_partial_message(auction["message_id"]).edit(embed=embed, view=None)
        except discord.HTTPException:
            pass
        if not cancelled:
            winners = sorted({it["bidder_id"] for it in sold})
            for i in range(0, len(winners), 50):
                users = winners[i:i + 50]
                allowed = discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(id=u) for u in users])
                await channel.send("Winners: " + " ".join(f"<@{u}>" for u in users), allowed_mentions=allowed)

    async def place_bid(self, interaction, auction, index: int, bid: int, alias: Optional[str] = None):
        if self.store.auctions.get(auction["id"]) is not auction or time.time() >= auction["ends_at"]:
            return await interaction.response.send_message("Bidding is closed.", ephemeral=True)
        item = auction["items"][index]
        minimum = auction["starting_bid"] if item["bidder_id"] is None else item["bid"] + auction["min_increment"]
        if bid < max(1, minimum):
            return await interaction.response.send_message(f"Minimum bid is {max(1, minimum):,}.", ephemeral=True)
        old_bidder = item["bidder_id"]
        display = clean(alias or interaction.user.display_name, 32) or "Anonymous"
        item.update(bid=bid, bidder_id=interaction.user.id, bidder_name=display)
        item.setdefault("bidders", {})[str(interaction.user.id)] = bid
        auction["bid_count"] += 1
        if auction["anti_snipe"] and auction["ends_at"] - time.time() < SNIPE_SECONDS and auction["extensions"] < MAX_EXTENSIONS:
            auction["ends_at"] = time.time() + SNIPE_SECONDS
            auction["extensions"] += 1
        self.store.save()
        await interaction.response.send_message(f"Bid placed on **{safe(item['name'])}** for **{bid:,}** as **{safe(display)}**.", ephemeral=True)
        try:
            channel = await self.get_channel_for(auction)
            await channel.send(embed=discord.Embed(description=f"**{safe(display)}** bid **{bid:,}** on **{safe(item['name'])}**", color=discord.Color.gold()))
            if old_bidder and old_bidder != interaction.user.id:
                user = self.get_user(old_bidder) or await self.fetch_user(old_bidder)
                await user.send(f"You've been outbid on **{safe(item['name'])}** in auction #{auction['id']}. New top bid: **{bid:,}**.")
        except discord.HTTPException:
            log.info("Could not announce bid or send outbid DM")


bot = AuctionBot()


class BidButton(discord.ui.DynamicItem[discord.ui.Button], template=r"auction:bid:(?P<id>[0-9]+)"):
    def __init__(self, auction_id: int):
        super().__init__(discord.ui.Button(label="Place a bid", style=discord.ButtonStyle.success, custom_id=f"auction:bid:{auction_id}"))
        self.auction_id = auction_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        auction = bot.store.auctions.get(self.auction_id)
        if not auction or time.time() >= auction["ends_at"]:
            return await interaction.response.send_message("That auction has ended.", ephemeral=True)
        await interaction.response.send_modal(BidModal(auction, interaction.user))


class BidModal(discord.ui.Modal):
    def __init__(self, auction: dict, user: discord.User):
        super().__init__(title=f"Bid · {auction.get('title') or 'Auction'}"[:45])
        self.auction_id = auction["id"]
        self.item_no = None
        if len(auction["items"]) > 1:
            self.item_no = discord.ui.TextInput(label="Item number", placeholder=f"1 to {len(auction['items'])}", max_length=len(str(len(auction["items"]))))
            self.add_item(self.item_no)
        self.bid = discord.ui.TextInput(label="Your bid", placeholder="e.g. 250 or 1.5k", max_length=20)
        self.alias = discord.ui.TextInput(label="Custom name (optional)", placeholder=clean(user.display_name, 32), required=False, max_length=32)
        self.add_item(self.bid)
        self.add_item(self.alias)

    async def on_submit(self, interaction: discord.Interaction):
        auction = bot.store.auctions.get(self.auction_id)
        if not auction:
            return await interaction.response.send_message("That auction has ended.", ephemeral=True)
        index = 0
        if self.item_no is not None:
            if not self.item_no.value.isdecimal() or not 1 <= int(self.item_no.value) <= len(auction["items"]):
                return await interaction.response.send_message("Invalid item number.", ephemeral=True)
            index = int(self.item_no.value) - 1
        parsed = amount(self.bid.value)
        if parsed is None:
            return await interaction.response.send_message("Enter a whole-number bid, such as 250 or 1.5k.", ephemeral=True)
        await bot.place_bid(interaction, auction, index, parsed, self.alias.value)


def auction_embed(auction: dict) -> discord.Embed:
    lines = []
    for i, item in enumerate(auction["items"], 1):
        status = "no bids yet" if item["bidder_id"] is None else f"{item['bid']:,} · {safe(item['bidder_name'])}"
        lines.append(f"**{i}. {safe(item['name'])}** · {status}")
    embed = discord.Embed(title=auction.get("title") or f"Auction #{auction['id']}", description="\n".join(lines)[:4000], color=discord.Color.gold())
    embed.add_field(name="Ends", value=f"<t:{int(auction['ends_at'])}:R>")
    embed.add_field(name="Opening bid", value=f"{auction['starting_bid']:,}")
    embed.add_field(name="Minimum raise", value=f"{auction['min_increment']:,}")
    if auction["anti_snipe"]:
        embed.set_footer(text=f"Anti-snipe enabled · {auction['bid_count']} bids")
    return embed


@bot.tree.command(name="create_auction", description="Start an auction with one or more items")
@app_commands.guild_only()
@app_commands.describe(items="Comma-separated item names", duration_text="For example 30m, 2h, or 1d", title="Optional title", starting_bid="Opening bid", min_increment="Minimum raise", anti_snipe="Extend bidding when a late bid arrives")
async def create_auction(interaction: discord.Interaction, items: str, duration_text: str, title: Optional[str] = None, starting_bid: app_commands.Range[int, 0, MAX_BID] = 0, min_increment: app_commands.Range[int, 1, MAX_BID] = 1, anti_snipe: bool = False):
    names = [clean(part, 50) for part in items.split(",") if clean(part, 50)]
    seconds = duration(duration_text)
    if not names or len({n.lower() for n in names}) != len(names):
        return await interaction.response.send_message("Provide at least one uniquely named item (comma-separated).", ephemeral=True)
    if seconds is None:
        return await interaction.response.send_message("Duration must be between 10 seconds and 14 days (e.g. 30m, 2h, 1d).", ephemeral=True)
    now = time.time()
    aid = bot.store.next_id
    auction = {"id": aid, "guild_id": interaction.guild_id, "channel_id": interaction.channel_id, "message_id": 0, "creator_id": interaction.user.id, "title": clean(title, 80) if title else None, "starting_bid": starting_bid, "min_increment": min_increment, "anti_snipe": anti_snipe, "extensions": 0, "bid_count": 0, "ends_at": now + seconds, "items": [{"name": n, "bid": 0, "bidder_id": None, "bidder_name": None, "bidders": {}} for n in names]}
    bot.store.next_id += 1
    view = discord.ui.View(timeout=None)
    view.add_item(BidButton(aid))
    await interaction.response.send_message(embed=auction_embed(auction), view=view)
    auction["message_id"] = (await interaction.original_response()).id
    bot.store.auctions[aid] = auction
    bot.store.save()


@bot.tree.command(name="bid", description="Bid on an auction item")
@app_commands.guild_only()
async def bid(interaction: discord.Interaction, item: str, amount_value: app_commands.Range[int, 1, MAX_BID], name: Optional[app_commands.Range[str, 1, 32]] = None):
    match = re.fullmatch(r"(\d+):(\d+)", item.strip())
    auction = bot.store.auctions.get(int(match[1])) if match else None
    if match:
        index = int(match[2])
    else:
        found = [(a, i) for a in bot.store.auctions.values() if a["guild_id"] == interaction.guild_id for i, it in enumerate(a["items"]) if it["name"].lower() == item.lower()]
        if len(found) != 1:
            return await interaction.response.send_message("Choose an unambiguous item name or use auction_id:item_index.", ephemeral=True)
        auction, index = found[0]
    if not auction or auction["guild_id"] != interaction.guild_id or not 0 <= index < len(auction["items"]):
        return await interaction.response.send_message("I couldn't find that auction item in this server.", ephemeral=True)
    await bot.place_bid(interaction, auction, index, amount_value, name)


@bid.autocomplete("item")
async def bid_autocomplete(interaction: discord.Interaction, current: str):
    choices = []
    for auction in bot.store.auctions.values():
        if auction["guild_id"] != interaction.guild_id or auction["ends_at"] <= time.time():
            continue
        for i, item in enumerate(auction["items"]):
            label = f"#{auction['id']} · {item['name']} · {item['bid']:,}"[:100]
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label, value=f"{auction['id']}:{i}"))
    return choices[:25]


@bot.tree.command(name="auctions", description="List running auctions in this server")
@app_commands.guild_only()
async def auctions(interaction: discord.Interaction):
    active = sorted((a for a in bot.store.auctions.values() if a["guild_id"] == interaction.guild_id), key=lambda a: a["ends_at"])
    if not active:
        return await interaction.response.send_message("No auctions are running.", ephemeral=True)
    await interaction.response.send_message("\n".join(f"**#{a['id']} {safe(a.get('title') or '')}** · {len(a['items'])} item(s) · ends <t:{int(a['ends_at'])}:R>" for a in active)[:1900], ephemeral=True)


@bot.tree.command(name="mybids", description="See your standing bids in running auctions")
@app_commands.guild_only()
async def mybids(interaction: discord.Interaction):
    rows = []
    for auction in bot.store.auctions.values():
        if auction["guild_id"] != interaction.guild_id:
            continue
        for i, item in enumerate(auction["items"], 1):
            best = item.get("bidders", {}).get(str(interaction.user.id))
            if best is not None:
                state = "Winning" if item["bidder_id"] == interaction.user.id else f"Outbid (top {item['bid']:,})"
                rows.append(f"Auction #{auction['id']} · {i}. {safe(item['name'])} · your best {best:,} · {state}")
    await interaction.response.send_message("\n".join(rows)[:1900] if rows else "You haven't bid in any running auctions.", ephemeral=True)


async def manageable(interaction: discord.Interaction, aid: int):
    auction = bot.store.auctions.get(aid)
    if not auction or auction["guild_id"] != interaction.guild_id:
        await interaction.response.send_message("Auction not found.", ephemeral=True)
        return None
    if interaction.user.id != auction["creator_id"] and not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Only the host or a server manager can do that.", ephemeral=True)
        return None
    return auction


@bot.tree.command(name="end_auction", description="End your auction early and announce winners")
@app_commands.guild_only()
async def end_auction(interaction: discord.Interaction, auction_id: int):
    if await manageable(interaction, auction_id):
        await interaction.response.send_message(f"Ending auction #{auction_id}…", ephemeral=True)
        await bot.close_auction(auction_id)


@bot.tree.command(name="cancel_auction", description="Cancel your auction without winners")
@app_commands.guild_only()
async def cancel_auction(interaction: discord.Interaction, auction_id: int):
    if await manageable(interaction, auction_id):
        await interaction.response.send_message(f"Cancelling auction #{auction_id}…", ephemeral=True)
        await bot.close_auction(auction_id, cancelled=True)


@bot.tree.command(name="auction_help", description="Show auction bot help")
async def auction_help(interaction: discord.Interaction):
    await interaction.response.send_message("Use `/create_auction` to start, `/bid` or the post button to bid, `/auctions` to list, `/mybids` to check your bids, and `/end_auction` or `/cancel_auction` to manage your auction.", ephemeral=True)


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_TOKEN in your hosting environment or .env file.")
    bot.run(token)
