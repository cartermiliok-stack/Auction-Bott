"""Mock auction bot for Discord (discord.py 2.4+, Python 3.10+).

Direct messages (best effort: people with DMs closed simply don't get them)
  * Outbid: when someone beats your bid you get a DM with the new top bid. It never says who.
  * Raised: when you raise your own top bid you get a DM with the previous and new amounts.
  * Time checks: everyone who has bid gets a DM when 50% and again when 75% of the
    auction's running time has passed, with where their bids stand.

An auction can hold any number of items. When they don't all fit on the post, a
"View all items" button opens a paged list. The post turns orange once the auction
enters its final quarter.

Starting bids and currency
  * /create_auction starting_bids:"50, 100, 25" gives every item its own opening bid, in the
    same order as the items. A single number applies to all of them.
  * currency:"gold" (or $, 🪙 ...) names the money. /set_currency changes it on a running
    auction, and the DEFAULT_CURRENCY setting picks one when none is given.

Memory
  * Auctions are not carried over between runs: a restart starts clean, and auction numbers
    always start again from the lowest free one. Set KEEP_AUCTIONS=1 to save running auctions
    to AUCTION_DATA_FILE and resume them after a restart instead.
"""
import asyncio
import json
import logging
import os
import re
import secrets
import time
from decimal import Decimal
from itertools import islice
from pathlib import Path
from typing import Callable, Optional

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
KEEP_AUCTIONS = os.getenv("KEEP_AUCTIONS", "").strip().lower() in ("1", "true", "yes", "on")
MAX_CURRENCY = 16  # characters in a currency name
MAX_BID = 10**12
MAX_DURATION = 14 * 86400
SNIPE_SECONDS = 30
MAX_EXTENSIONS = 20

GOLD = discord.Color.gold()
GREEN = discord.Color.green()
RED = discord.Color.red()
ORANGE = discord.Color.orange()
GREY = discord.Color.dark_grey()
DARK_RED = discord.Color.dark_red()

# There is no cap on items per auction. These only control how a big auction is laid out.
ITEM_FIELD_LIMIT = 930  # characters per item field (a field holds 1024; the rest is room for a note)
MAX_ITEM_FIELDS = 5  # item fields on the post; even maxed out, the embed stays under Discord's 6,000-character cap
ITEMS_PER_PAGE = 10  # rows per page in the "View all items" list

# Everyone who has bid gets a DM when this % of the running time has passed.
# percent -> (DM title, how the auction is described in the DM, DM colour)
MILESTONES = {
    50: ("Halfway point", "is halfway through", GOLD),
    75: ("Final quarter", "has only a quarter of its time left", ORANGE),
}
FINAL_QUARTER = max(MILESTONES)  # the post turns orange once this much of the time has passed
MIN_NOTICE_SECONDS = 10  # skip a time-check DM if it would arrive with less than this left
DM_DELAY = 0.5  # seconds between DMs, to stay friendly with Discord's rate limits


# ─────────────────────────── helpers ───────────────────────────


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


def amount(value: str, minimum: int = 1) -> Optional[int]:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([km]?)", re.sub(r"[\s,_]", "", value.lower()))
    if not match:
        return None
    number = Decimal(match[1]) * {"": 1, "k": 1000, "m": 1000000}[match[2]]
    return int(number) if number == number.to_integral_value() and minimum <= number <= MAX_BID else None


def fmt(n: int) -> str:
    return f"{n:,}"


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def money(a: dict, n: int) -> str:
    """1234 -> '1,234', '$1,234' or '1,234 gold', depending on the auction's currency name."""
    cur = a.get("currency")
    if not cur:
        return fmt(n)
    if len(cur) <= 2 and not cur.isalnum():  # $, €, 🪙 ... go in front
        return f"{cur}{fmt(n)}"
    return f"{fmt(n)} {cur}"


def clean_currency(value: str) -> Optional[str]:
    """A currency name that is safe to show (it can't ping anyone), or None if it's empty."""
    return discord.utils.escape_mentions(clean(value, MAX_CURRENCY)) or None


DEFAULT_CURRENCY = clean_currency(os.getenv("DEFAULT_CURRENCY", ""))  # used when a host doesn't pick one


def starting_bids_for(text: Optional[str], count: int) -> tuple[list[int], Optional[str]]:
    """'50, 100, 25' -> one opening bid per item. A single number applies to every item, and
    leaving it empty means the lowest allowed bid is 1. Returns (bids, error message)."""
    parts = [p.strip() for p in (text or "").split(",") if p.strip()]
    if not parts:
        return [0] * count, None
    bids = [amount(p, minimum=0) for p in parts]
    if None in bids:
        return [], "I couldn't read the starting bids. Use whole numbers separated by commas, like `50, 100, 25`."
    if len(bids) == 1:
        bids *= count
    if len(bids) != count:
        return [], (
            f"You listed {plural(count, 'item')} but {plural(len(bids), 'starting bid')}. "
            "Give one starting bid per item, or a single number for all of them."
        )
    return bids, None


def tag(a: dict) -> str:
    """Plain text name: '#3 Friday Night Auction', or just '#3'."""
    return f"#{a['id']}" + (f" {a['title']}" if a.get("title") else "")


def label(a: dict) -> str:
    """Bold, markdown-safe name for use inside messages."""
    return f"**{safe(a['title'])}** (#{a['id']})" if a.get("title") else f"**Auction #{a['id']}**"


def jump_url(a: dict) -> str:
    return f"https://discord.com/channels/{a['guild_id']}/{a['channel_id']}/{a['message_id']}"


def running(auction_id: int, nonce: str) -> Optional[dict]:
    """The auction with this number, but only if it is still the one a button or form was made for.
    Numbers are reused once an auction is over, so a leftover button must not reach a newer auction."""
    a = bot.store.auctions.get(auction_id)
    return a if a is not None and a["nonce"] == nonce else None


def minimum_bid(a: dict, it: dict) -> int:
    """The lowest bid currently allowed on an item."""
    floor = it["start"] if it["bidder_id"] is None else it["bid"] + a["min_increment"]
    return max(1, floor)


def notice(text: str, kind: str = "info", title: Optional[str] = None) -> discord.Embed:
    """A small coloured card for replies: info (gold), ok (green) or error (red)."""
    color = {"info": GOLD, "ok": GREEN, "error": RED}[kind]
    return discord.Embed(title=title, description=text, color=color)


def chunk_lines(lines: list[str], limit: int = 1900, sep: str = "\n"):
    """Group lines into pieces that each stay under `limit` characters."""
    chunk = ""
    for line in lines:
        if chunk and len(chunk) + len(sep) + len(line) > limit:
            yield chunk
            chunk = ""
        chunk += (sep if chunk else "") + line
    if chunk:
        yield chunk


def fit_lines(lines: list[str], limit: int) -> tuple[str, int]:
    """Join as many lines as fit in `limit` characters. Returns (text, how many were left out)."""
    out, used = [], 0
    for line in lines:
        if used + len(line) + 1 > limit:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out), len(lines) - len(out)


def paginate_groups(groups: list[tuple[str, list[str]]], limit: int = 3600) -> list[str]:
    """Pages of 'heading + rows' blocks. A group that spills over a page break gets its heading repeated."""
    pages, page = [], ""
    for head, rows in groups:
        fresh = True  # this group's heading isn't on the current page yet
        for row in rows:
            block = f"{head}\n{row}" if fresh else row
            joiner = ("\n\n" if fresh else "\n") if page else ""
            if page and len(page) + len(joiner) + len(block) > limit:
                pages.append(page)
                page, joiner, block = "", "", f"{head}\n{row}"
            page += joiner + block
            fresh = False
    if page:
        pages.append(page)
    return pages


def link_view(a: dict, text: str = "View auction") -> discord.ui.View:
    """A one-button view that jumps to the auction post."""
    view = discord.ui.View(timeout=60)
    view.add_item(discord.ui.Button(label=text, url=jump_url(a)))
    return view


def progress_bar(percent: int, width: int = 10) -> str:
    filled = round(width * percent / 100)
    return "▰" * filled + "▱" * (width - filled)


def bidder_count(a: dict) -> int:
    return len({uid for it in a["items"] for uid in it["bidders"]})


def in_final_quarter(a: dict) -> bool:
    start, total = a.get("started_at"), a.get("duration")
    return bool(start and total) and time.time() >= start + total * FINAL_QUARTER / 100


# ─────────────────────────── embeds ───────────────────────────


def item_line(a: dict, n: int, it: dict, state: str, show_start: bool = False) -> str:
    name = safe(it["name"])
    if it["bidder_id"] is None:
        status = {"open": "no bids yet", "ended": "unsold", "cancelled": "no bids"}[state]
        opens = f" · opens at **{money(a, max(1, it['start']))}**" if show_start and state == "open" else ""
        return f"**{n}.** {name} · *{status}*{opens}"
    word = {"open": "", "ended": "sold for ", "cancelled": "last bid "}[state]
    return f"**{n}.** {name} · {word}**{money(a, it['bid'])}** · {safe(it['bidder_name'])}"


def layout(a: dict, state: str = "open") -> tuple[list[tuple[str, str]], list[str], int]:
    """The post's stat fields, its item fields, and how many items didn't fit."""
    ts = int(a["ends_at"])
    sold = [it for it in a["items"] if it["bidder_id"] is not None]
    starts = [max(1, it["start"]) for it in a["items"]]
    varied = min(starts) != max(starts)  # the items have different opening bids
    if state == "open":
        if varied:
            opening = ("Opening bids", f"{money(a, min(starts))} to {money(a, max(starts))}")
        else:
            opening = ("Opening bid", money(a, starts[0]))
        stats = [
            ("Ends", f"<t:{ts}:R>\n<t:{ts}:f>"),
            opening,
            ("Minimum raise", money(a, a["min_increment"])),
        ]
    elif state == "ended":
        stats = [
            ("Ended", f"<t:{ts}:f>"),
            ("Sold", f"{len(sold)} of {len(a['items'])}"),
            ("Total", money(a, sum(it["bid"] for it in sold)) if sold else "None"),
        ]
    else:
        stats = []
    lines = [item_line(a, n, it, state, varied) for n, it in enumerate(a["items"], 1)]
    chunks = list(islice(chunk_lines(lines, ITEM_FIELD_LIMIT), MAX_ITEM_FIELDS))
    hidden = len(lines) - sum(c.count("\n") + 1 for c in chunks)
    if hidden:
        more = {
            "open": "Tap **View all items** to see the rest.",
            "ended": "Every item is listed in the results below.",
            "cancelled": "",
        }[state]
        chunks[-1] += f"\n…and {plural(hidden, 'more item')}. {more}".rstrip()
    return stats, chunks, hidden


def auction_embed(a: dict, state: str = "open") -> discord.Embed:
    """state is 'open', 'ended' or 'cancelled'."""
    stats, chunks, _ = layout(a, state)
    soon = state == "open" and in_final_quarter(a)
    color = ORANGE if soon else {"open": GOLD, "ended": GREY, "cancelled": DARK_RED}[state]
    status = "Final quarter" if soon else {"open": "Open", "ended": "Ended", "cancelled": "Cancelled"}[state]

    notes = []
    if state == "cancelled":
        notes.append("Cancelled by the host. Nobody wins.")
    elif state == "open" and a["anti_snipe"]:
        notes.append(f"Anti-snipe is on: a bid in the last {SNIPE_SECONDS}s pushes the end back.")
    embed = discord.Embed(description="\n".join(notes) or None, color=color)
    if a.get("title"):
        embed.title = a["title"]
        embed.set_author(name=f"Auction #{a['id']} · {status}")
    else:
        embed.title = f"Auction #{a['id']}"
        embed.set_author(name=status)
    for name, value in stats:
        embed.add_field(name=name, value=value, inline=True)
    for i, chunk in enumerate(chunks):
        embed.add_field(name=f"Items ({len(a['items'])})" if i == 0 else "\u200b", value=chunk, inline=False)
    counts = f"{plural(a['bid_count'], 'bid')} · {plural(bidder_count(a), 'bidder')}"
    host = a.get("creator_name")
    embed.set_footer(text=f"Hosted by {host} · {counts}" if host else counts, icon_url=a.get("creator_avatar"))
    return embed


def result_pages(a: dict) -> tuple[list[discord.Embed], list[int]]:
    """The final results as one or more embeds (a long auction spills onto extra pages),
    plus the ids of the winners. Sold items come first; unsold ones are folded into a short list."""
    lines, unsold, winners, total, sold = [], [], set(), 0, 0
    for n, it in enumerate(a["items"], 1):
        if it["bidder_id"] is None:
            unsold.append(safe(it["name"]))
            continue
        sold += 1
        total += it["bid"]
        winners.add(it["bidder_id"])
        lines.append(
            f"**{n}.** {safe(it['name'])} · **{money(a, it['bid'])}** · "
            f"**{safe(it['bidder_name'])}** (<@{it['bidder_id']}>)"
        )
    if unsold:
        if lines:
            lines.append("")
        lines.extend(f"**Unsold** · {chunk}" for chunk in chunk_lines(unsold, 1500, ", "))

    summary = f"**{sold} of {len(a['items'])} items sold**"
    if sold:
        summary += f" · **{money(a, total)}** in total"
    intro = f"{label(a)} has ended. [Original post]({jump_url(a)})\n{summary}"
    pages = list(chunk_lines(lines, 3600))
    embeds = []
    for i, page in enumerate(pages):
        embed = discord.Embed(color=GOLD)
        if i == 0:
            embed.title = "Auction results"
            embed.description = f"{intro}\n\n{page}"
        else:
            embed.title = "Auction results, continued"
            embed.description = page
        footer = f"Auction #{a['id']}"
        if len(pages) > 1:
            footer += f" · Page {i + 1} of {len(pages)}"
        embed.set_footer(text=footer)
        embeds.append(embed)
    return embeds, sorted(winners)


def outbid_embed(a: dict, it: dict) -> discord.Embed:
    """The DM sent to someone who just lost the top bid. It never says who took it."""
    embed = discord.Embed(
        title="You've been outbid",
        description=f"Someone placed a higher bid on **{safe(it['name'])}** in {label(a)}.",
        color=ORANGE,
    )
    embed.add_field(name="New top bid", value=money(a, it["bid"]), inline=True)
    embed.add_field(name="To take the lead", value=f"At least {money(a, minimum_bid(a, it))}", inline=True)
    embed.add_field(name="Ends", value=f"<t:{int(a['ends_at'])}:R>", inline=True)
    return embed


def raised_embed(a: dict, it: dict, previous_bid: int) -> discord.Embed:
    """The DM sent to someone who raised their own top bid. It goes only to that person, so it
    can safely show the custom name they bid under."""
    embed = discord.Embed(
        title="Your bid was raised",
        description=(
            f"You raised your own bid on **{safe(it['name'])}** in {label(a)}. "
            "You're still the top bidder."
        ),
        color=GREEN,
    )
    embed.add_field(name="Previous bid", value=money(a, previous_bid), inline=True)
    embed.add_field(name="New bid", value=money(a, it["bid"]), inline=True)
    embed.add_field(name="Bidding as", value=safe(it["bidder_name"]), inline=True)
    embed.add_field(name="Ends", value=f"<t:{int(a['ends_at'])}:R>", inline=False)
    return embed


# ─────────────────────────── storage ───────────────────────────


def migrate(a: dict):
    """Fill in fields that older saves don't have."""
    a.setdefault("title", None)
    a.setdefault("currency", None)
    a.setdefault("nonce", secrets.token_hex(4))
    legacy_start = a.pop("starting_bid", 0)  # older saves had one opening bid for the whole auction
    a.setdefault("min_increment", 1)
    a.setdefault("anti_snipe", False)
    a.setdefault("extensions", 0)
    a.setdefault("creator_name", None)
    a.setdefault("creator_avatar", None)
    # Auctions saved before time-check DMs existed don't know when they started, so they skip them.
    a.setdefault("started_at", None)
    a.setdefault("duration", None)
    a.setdefault("notified", [])
    for it in a["items"]:
        it.setdefault("start", legacy_start)
        it.setdefault("bidders", {} if it["bidder_id"] is None else {str(it["bidder_id"]): it["bid"]})
    a.setdefault("bid_count", sum(1 for it in a["items"] if it["bidder_id"] is not None))


class Store:
    """Auction state. Nothing is carried over between runs unless KEEP_AUCTIONS is on, in which case
    running auctions are saved as JSON (mount the data directory persistently when hosting)."""

    def __init__(self):
        self.path = DATA_FILE
        self.persist = KEEP_AUCTIONS
        self.auctions: dict[int, dict] = {}
        self.reserved: set[int] = set()  # numbers claimed by auctions that are still being posted
        if not self.persist:
            if self.path.exists():
                log.info("Ignoring %s: auctions are not carried over between runs (set KEEP_AUCTIONS=1 to keep them)", self.path)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                self.auctions = {int(k): v for k, v in saved.get("auctions", {}).items()}
                for a in self.auctions.values():
                    migrate(a)
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                log.exception("Could not load auction data: %s", exc)
                self.path.replace(self.path.with_suffix(f".corrupt-{int(time.time())}.json"))
                self.auctions = {}

    def reserve_id(self) -> int:
        """The lowest auction number nobody is using, so numbering never depends on past auctions."""
        n = 1
        while n in self.auctions or n in self.reserved:
            n += 1
        self.reserved.add(n)
        return n

    def save(self):
        if not self.persist:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"auctions": self.auctions}, indent=2), encoding="utf-8")
        tmp.replace(self.path)


# ─────────────────────────── the bot ───────────────────────────


class AuctionBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default(), allowed_mentions=discord.AllowedMentions.none())
        self.tree = app_commands.CommandTree(self)
        self.store = Store()
        self.dirty: set[int] = set()  # auctions whose post needs refreshing
        self.background: set[asyncio.Task] = set()  # keeps DM tasks from being garbage-collected

    async def setup_hook(self):
        self.add_dynamic_items(BidButton, ItemsButton)  # keeps the buttons working across restarts
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

    def spawn(self, coro):
        """Run a coroutine in the background so slow work (DMs) never holds up a bid."""
        task = asyncio.create_task(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    async def dm(self, user_id: int, embed: discord.Embed, view: Optional[discord.ui.View] = None):
        """Best-effort direct message. People with DMs closed just don't get it."""
        try:
            user = self.get_user(user_id) or await self.fetch_user(user_id)
            await user.send(embed=embed, view=view if view is not None else discord.utils.MISSING)
        except discord.HTTPException:
            log.info("Could not DM user %s (DMs closed, or the account is gone)", user_id)
        except Exception:
            log.exception("Unexpected error while DMing user %s", user_id)

    async def send_dms(self, messages: dict[int, tuple[discord.Embed, discord.ui.View]]):
        for user_id, (embed, view) in messages.items():
            await self.dm(user_id, embed, view)
            await asyncio.sleep(DM_DELAY)

    # Every couple of seconds: close auctions whose time is up (this also catches ones that
    # ended while the bot was offline), send the halfway / 75% DMs, and refresh posts that got
    # new bids. Refreshing here, instead of on every bid, keeps a bidding war within rate limits.
    @tasks.loop(seconds=2)
    async def tick(self):
        now = time.time()
        for aid in [i for i, a in self.store.auctions.items() if a["ends_at"] <= now]:
            try:
                await self.close_auction(aid)
            except Exception:
                log.exception("Failed to close auction #%s", aid)
        for auction in list(self.store.auctions.values()):
            try:
                self.check_milestones(auction, now)
            except Exception:
                log.exception("Failed to send time-check DMs for auction #%s", auction["id"])
        for aid in list(self.dirty):
            self.dirty.discard(aid)
            auction = self.store.auctions.get(aid)
            if auction:
                try:
                    await self.refresh_embed(auction)
                except Exception:
                    log.exception("Failed to refresh auction #%s", aid)

    @tick.before_loop
    async def before_tick(self):
        await self.wait_until_ready()

    def check_milestones(self, a: dict, now: float):
        """DM everyone who has bid once 50% and then 75% of the running time has passed.
        The marks are measured against the original length, so anti-snipe extensions
        don't move them."""
        start, total = a["started_at"], a["duration"]
        if not start or not total:  # auction from before this feature existed
            return
        due = [p for p in MILESTONES if p not in a["notified"] and now >= start + total * p / 100]
        if not due:
            return
        a["notified"].extend(due)  # recorded first, so a restart can never repeat a DM
        self.store.save()
        self.dirty.add(a["id"])  # the post changes colour in the final quarter, so redraw it
        if a["ends_at"] - now < MIN_NOTICE_SECONDS:  # too late for the DM to be useful
            return

        # If the bot was offline and several marks passed at once, only the latest matters.
        percent = due[-1]
        title, phrase, color = MILESTONES[percent]
        ts = int(a["ends_at"])
        intro = f"{label(a)} {phrase}.\n\n{progress_bar(percent)}  {percent}% of the time has passed"

        rows: dict[int, list[tuple[str, bool]]] = {}
        for n, it in enumerate(a["items"], 1):
            for uid, best in it["bidders"].items():
                winning = int(uid) == it["bidder_id"]
                status = "**Winning**" if winning else f"Outbid, now **{money(a, it['bid'])}**"
                rows.setdefault(int(uid), []).append(
                    (f"**{n}.** {safe(it['name'])} · your best {money(a, best)} · {status}", winning)
                )
        if not rows:
            return

        messages = {}
        for uid, entries in rows.items():
            winning = sum(1 for _, w in entries if w)
            shown, hidden = fit_lines([line for line, _ in entries], 900)
            if hidden:
                shown += f"\n…and {plural(hidden, 'more item')}. Use `/mybids` to see them all."
            embed = discord.Embed(title=title, description=intro, color=color)
            embed.add_field(name="Ends", value=f"<t:{ts}:R>", inline=True)
            embed.add_field(name="Winning", value=str(winning), inline=True)
            embed.add_field(name="Outbid", value=str(len(entries) - winning), inline=True)
            embed.add_field(name="Your bids", value=shown, inline=False)
            embed.set_footer(text="You're getting this because you bid in this auction.")
            messages[uid] = (embed, link_view(a, "Go to auction"))
        self.spawn(self.send_dms(messages))

    async def refresh_embed(self, auction: dict):
        try:
            channel = await self.get_channel_for(auction)
        except discord.HTTPException:
            return log.warning("Auction #%s: can't reach its channel to refresh the post", auction["id"])
        try:
            # The view is re-sent too, so "View all items" appears once the list outgrows the post.
            await channel.get_partial_message(auction["message_id"]).edit(
                embed=auction_embed(auction), view=bid_view(auction)
            )
        except discord.NotFound:  # somebody deleted the post, so put a fresh one up
            try:
                msg = await channel.send(embed=auction_embed(auction), view=bid_view(auction))
                auction["message_id"] = msg.id
                self.store.save()
            except discord.HTTPException:
                log.warning("Auction #%s: could not re-post the deleted auction message", auction["id"])
        except discord.HTTPException:
            log.warning("Auction #%s: could not refresh the auction post", auction["id"])

    async def close_auction(self, aid: int, cancelled: bool = False):
        # Pop first: from here on nobody can bid, and it can't be closed twice.
        auction = self.store.auctions.pop(aid, None)
        if auction is None:
            return
        self.dirty.discard(aid)
        self.store.save()
        auction["ends_at"] = min(auction["ends_at"], time.time())  # shows the real end if closed early
        try:
            channel = await self.get_channel_for(auction)
        except discord.HTTPException:
            log.exception("Could not access channel for auction #%s", aid)
            return
        try:
            state = "cancelled" if cancelled else "ended"
            await channel.get_partial_message(auction["message_id"]).edit(
                embed=auction_embed(auction, state), view=None  # view=None removes the buttons
            )
        except discord.HTTPException:
            pass
        try:
            if cancelled:
                await channel.send(
                    embed=discord.Embed(
                        title="Auction cancelled",
                        description=f"{label(auction)} was cancelled by the host. Nobody wins.",
                        color=DARK_RED,
                    )
                )
                return
            embeds, winners = result_pages(auction)
            for embed in embeds:
                await channel.send(embed=embed)
            # Only the actual winners can be pinged, whatever custom names were typed.
            # Discord caps the allowed-mentions list at 100 users, so ping in groups.
            for i in range(0, len(winners), 50):
                group = winners[i : i + 50]
                intro = "Congratulations to the winners:" if i == 0 else "More winners:"
                allowed = discord.AllowedMentions(
                    everyone=False, roles=False, users=[discord.Object(id=u) for u in group]
                )
                await channel.send(f"{intro} " + " ".join(f"<@{u}>" for u in group), allowed_mentions=allowed)
        except discord.HTTPException:
            log.exception("Could not post the results of auction #%s", aid)

    async def place_bid(self, interaction, auction, index: int, bid: int, alias: Optional[str] = None):
        """Shared by /bid and the bid form. Everything before the first await runs without
        interruption, so two simultaneous bids can never both win."""
        item = auction["items"][index]
        now = time.time()
        if self.store.auctions.get(auction["id"]) is not auction or now >= auction["ends_at"]:
            return await reject(interaction, "Bidding on that auction has closed.", "Bidding closed")
        need = minimum_bid(auction, item)
        if bid < need:
            if item["bidder_id"] is None:
                msg = f"The first bid on **{safe(item['name'])}** has to be at least **{money(auction, need)}**."
            else:
                msg = (
                    f"**{safe(item['name'])}** is at **{money(auction, item['bid'])}**, "
                    f"so you need to bid at least **{money(auction, need)}**."
                )
            return await reject(interaction, msg, "Bid too low")

        previous_top, previous_bid = item["bidder_id"], item["bid"]  # who led until this moment, and at what
        display = clean(alias or "", 32) or clean(interaction.user.display_name, 32) or "Anonymous"
        item.update(bid=bid, bidder_id=interaction.user.id, bidder_name=display)
        item.setdefault("bidders", {})[str(interaction.user.id)] = bid
        auction["bid_count"] += 1
        extended = False
        if auction["anti_snipe"] and auction["ends_at"] - now < SNIPE_SECONDS and auction["extensions"] < MAX_EXTENSIONS:
            auction["ends_at"] = now + SNIPE_SECONDS
            auction["extensions"] += 1
            extended = True
        self.store.save()
        self.dirty.add(auction["id"])

        # DMs go out in the background, so they can't slow the bid down or be skipped if the
        # channel announcement fails. The outbid DM says that someone outbid you and what the new
        # bid is. It never names the bidder, or even the custom name they used.
        if previous_top is not None and previous_top != interaction.user.id:
            self.spawn(self.dm(previous_top, outbid_embed(auction, item), link_view(auction, "Go to auction")))
        elif previous_top == interaction.user.id:  # raising your own top bid: you get a DM too
            self.spawn(self.dm(previous_top, raised_embed(auction, item, previous_bid), link_view(auction, "Go to auction")))

        # The reply is private and the announcement comes from the bot, so Discord's
        # "X used /bid" header never gives away who is behind a custom name.
        confirm = notice(
            f"You're the top bidder on {label(auction)}. You'll get a DM if someone outbids you, "
            "provided your DMs are open.",
            "ok",
            "Bid placed",
        )
        confirm.add_field(name="Item", value=safe(item["name"]), inline=True)
        confirm.add_field(name="Your bid", value=money(auction, bid), inline=True)
        confirm.add_field(name="Bidding as", value=safe(display), inline=True)
        await send_card(interaction, confirm, link_view(auction))

        text = f"**{safe(display)}** bid **{money(auction, bid)}** on **{safe(item['name'])}**"
        if previous_top is not None:
            text += f" (up from {money(auction, previous_bid)})"
        if extended:
            text += f"\nAnti-snipe: bidding now ends <t:{int(auction['ends_at'])}:R>."
        card = discord.Embed(description=text, color=ORANGE if extended else GOLD)
        card.set_footer(text=f"{tag(auction)} · {plural(auction['bid_count'], 'bid')}")
        try:
            channel = await self.get_channel_for(auction)
            await channel.send(embed=card)
        except discord.HTTPException:
            log.info("Could not announce a bid in auction #%s", auction["id"])


bot = AuctionBot()


# ─────────────────────── replies and paging ───────────────────────


async def send_card(interaction: discord.Interaction, embed: discord.Embed, view: Optional[discord.ui.View] = None):
    """Send a private embed, optionally with a view (for example a jump-to-auction button)."""
    await interaction.response.send_message(
        embed=embed, view=view if view is not None else discord.utils.MISSING, ephemeral=True
    )


async def reply(interaction: discord.Interaction, text: str, kind: str = "info", title: Optional[str] = None):
    """A private reply shown as a small coloured card."""
    await send_card(interaction, notice(text, kind, title))


async def reject(interaction: discord.Interaction, text: str, title: Optional[str] = None):
    await reply(interaction, text, "error", title)


async def report_error(interaction: discord.Interaction, error: Exception):
    log.error("Unhandled error while handling an interaction", exc_info=error)
    embed = notice("Something went wrong on my end. Please try again.", "error")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException:
        pass


class Pager(discord.ui.View):
    """Previous / Next buttons under a private embed. `render(page)` returns
    (embed, page_count), or None when there is nothing left to show."""

    def __init__(self, render: Callable[[int], Optional[tuple[discord.Embed, int]]], auction_id: Optional[int] = None, nonce: Optional[str] = None):
        super().__init__(timeout=600)
        self.render = render
        self.page = 0
        self.origin: Optional[discord.Interaction] = None
        self.prev = discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary)
        self.counter = discord.ui.Button(label="1 / 1", style=discord.ButtonStyle.secondary, disabled=True)
        self.next = discord.ui.Button(label="Next", style=discord.ButtonStyle.secondary)
        self.prev.callback = self.go_prev
        self.next.callback = self.go_next
        for button in (self.prev, self.counter, self.next):
            self.add_item(button)
        if auction_id is not None:

            async def place_bid_here(interaction: discord.Interaction):
                await open_bid_modal(interaction, auction_id, nonce)

            bid_button = discord.ui.Button(label="Place a bid", style=discord.ButtonStyle.success)
            bid_button.callback = place_bid_here
            self.add_item(bid_button)

    def build(self) -> Optional[discord.Embed]:
        out = self.render(self.page)
        if out is None:
            return None
        embed, count = out
        if self.page >= count:  # the list shrank since this page was drawn
            self.page = max(count - 1, 0)
            embed, count = self.render(self.page)
        self.prev.disabled = self.page <= 0
        self.next.disabled = self.page >= count - 1
        self.counter.label = f"{self.page + 1} / {count}"
        return embed

    async def send(self, interaction: discord.Interaction):
        embed = self.build()
        if embed is None:
            return await reject(interaction, "That auction has ended.")
        self.origin = interaction
        await interaction.response.send_message(embed=embed, view=self, ephemeral=True)

    async def turn(self, interaction: discord.Interaction, step: int):
        self.page += step
        embed = self.build()
        if embed is None:
            return await interaction.response.edit_message(embed=notice("That auction has ended."), view=None)
        await interaction.response.edit_message(embed=embed, view=self)

    async def go_prev(self, interaction: discord.Interaction):
        await self.turn(interaction, -1)

    async def go_next(self, interaction: discord.Interaction):
        await self.turn(interaction, 1)

    async def on_timeout(self):
        if self.origin is None:
            return
        for child in self.children:
            child.disabled = True
        try:
            await self.origin.edit_original_response(view=self)
        except discord.HTTPException:
            pass


async def send_pages(interaction: discord.Interaction, title: str, pages: list[str], footer: Optional[str] = None):
    """Show private pages of text, with Previous / Next buttons when there is more than one."""

    def render(page: int):
        embed = discord.Embed(title=title, description=pages[page], color=GOLD)
        if footer:
            embed.set_footer(text=footer)
        return embed, len(pages)

    if len(pages) == 1:
        await send_card(interaction, render(0)[0])
    else:
        await Pager(render).send(interaction)


def items_page(auction_id: int, nonce: str, page: int):
    """One page of the "View all items" list (always live), or None if the auction is over."""
    a = running(auction_id, nonce)
    if a is None:
        return None
    total = len(a["items"])
    start = page * ITEMS_PER_PAGE
    entries = []
    for n, it in enumerate(a["items"][start : start + ITEMS_PER_PAGE], start + 1):
        need = money(a, minimum_bid(a, it))
        if it["bidder_id"] is None:
            detail = f"No bids yet · first bid at least **{need}**"
        else:
            detail = f"Top bid **{money(a, it['bid'])}** · {safe(it['bidder_name'])} · next bid at least **{need}**"
        entries.append(f"**{n}. {safe(it['name'])}**\n{detail}")
    embed = discord.Embed(title=a.get("title") or f"Auction #{a['id']}", description="\n\n".join(entries), color=GOLD)
    embed.set_author(name=f"All items · {plural(total, 'item')}")
    embed.set_footer(text=f"Items {start + 1} to {min(start + ITEMS_PER_PAGE, total)} of {total}")
    return embed, max(1, -(-total // ITEMS_PER_PAGE))


# ─────────────────────── buttons and the bid form ───────────────────────


def bid_view(a: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(BidButton(a["id"], a["nonce"]))
    if layout(a, "open")[2]:  # not every item fits on the post
        view.add_item(ItemsButton(a["id"], a["nonce"]))
    return view


async def open_bid_modal(interaction: discord.Interaction, auction_id: int, nonce: str):
    auction = running(auction_id, nonce)
    if not auction or time.time() >= auction["ends_at"]:
        return await reject(interaction, "That auction has ended.")
    await interaction.response.send_modal(BidModal(auction, interaction.user))


class BidButton(discord.ui.DynamicItem[discord.ui.Button], template=r"auction:bid:(?P<id>[0-9]+):(?P<nonce>[0-9a-f]+)"):
    def __init__(self, auction_id: int, nonce: str):
        super().__init__(discord.ui.Button(label="Place a bid", style=discord.ButtonStyle.success, custom_id=f"auction:bid:{auction_id}:{nonce}"))
        self.auction_id, self.nonce = auction_id, nonce

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["id"]), match["nonce"])

    async def callback(self, interaction: discord.Interaction):
        await open_bid_modal(interaction, self.auction_id, self.nonce)


class ItemsButton(discord.ui.DynamicItem[discord.ui.Button], template=r"auction:items:(?P<id>[0-9]+):(?P<nonce>[0-9a-f]+)"):
    """'View all items': a private, paged list. Only shown when the items don't all fit on the post."""

    def __init__(self, auction_id: int, nonce: str):
        super().__init__(discord.ui.Button(label="View all items", style=discord.ButtonStyle.secondary, custom_id=f"auction:items:{auction_id}:{nonce}"))
        self.auction_id, self.nonce = auction_id, nonce

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["id"]), match["nonce"])

    async def callback(self, interaction: discord.Interaction):
        auction_id, nonce = self.auction_id, self.nonce
        await Pager(lambda page: items_page(auction_id, nonce, page), auction_id, nonce).send(interaction)


class BidModal(discord.ui.Modal):
    def __init__(self, auction: dict, user: discord.User):
        heading = auction.get("title") or f"Auction #{auction['id']}"
        super().__init__(title=f"Bid · {heading}"[:45], timeout=600)
        self.auction_id, self.nonce = auction["id"], auction["nonce"]
        self.item_no = None
        default = None
        if len(auction["items"]) > 1:
            self.item_no = discord.ui.TextInput(label="Item number", placeholder=f"1 to {len(auction['items'])}", max_length=len(str(len(auction["items"]))))
            self.add_item(self.item_no)
            hint = "e.g. 250 or 1.5k"
        else:
            minimum = minimum_bid(auction, auction["items"][0])
            hint = f"At least {money(auction, minimum)}"
            default = str(minimum)  # pre-filled with the lowest allowed bid; just edit it
        self.bid = discord.ui.TextInput(label="Your bid", placeholder=hint, default=default, max_length=20)
        self.alias = discord.ui.TextInput(label="Custom name (optional)", placeholder=clean(user.display_name, 32), required=False, max_length=32)
        self.add_item(self.bid)
        self.add_item(self.alias)

    async def on_submit(self, interaction: discord.Interaction):
        auction = running(self.auction_id, self.nonce)
        if not auction:
            return await reject(interaction, "That auction has ended.")
        index = 0
        if self.item_no is not None:
            raw = self.item_no.value.strip()
            if not raw.isdecimal() or not 1 <= int(raw) <= len(auction["items"]):
                return await reject(interaction, f"The item number has to be 1 to {len(auction['items'])}.")
            index = int(raw) - 1
        parsed = amount(self.bid.value)
        if parsed is None:
            return await reject(interaction, "Enter your bid as a whole number, like `250` or `1.5k`.")
        await bot.place_bid(interaction, auction, index, parsed, self.alias.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await report_error(interaction, error)


# ─────────────────────────── commands ───────────────────────────


@bot.tree.command(name="create_auction", description="Start an auction with one or more items")
@app_commands.guild_only()
@app_commands.describe(
    items="Comma-separated item names, e.g. Golden Duck, Cursed Spoon, Mystery Box",
    duration_text="For example 30m, 2h, or 1d",
    title="Optional title",
    starting_bids="Opening bid per item, in item order, e.g. 50, 100, 25 (one number applies to all)",
    min_increment="Minimum raise",
    currency="Currency name, e.g. $, 🪙 or gold (optional)",
    anti_snipe="Extend bidding when a late bid arrives",
)
async def create_auction(interaction: discord.Interaction, items: str, duration_text: str, title: Optional[str] = None, starting_bids: Optional[str] = None, min_increment: app_commands.Range[int, 1, MAX_BID] = 1, currency: Optional[app_commands.Range[str, 1, MAX_CURRENCY]] = None, anti_snipe: bool = False):
    names = [clean(part, 50) for part in items.split(",") if clean(part, 50)]
    seconds = duration(duration_text)
    if not names or len({n.lower() for n in names}) != len(names):
        return await reject(interaction, "Provide at least one uniquely named item (comma-separated).")
    if seconds is None:
        return await reject(interaction, "Duration must be between 10 seconds and 14 days (e.g. 30m, 2h, 1d).")
    starts, problem = starting_bids_for(starting_bids, len(names))
    if problem:
        return await reject(interaction, problem)
    now = time.time()
    aid = bot.store.reserve_id()  # the lowest free number, held until the post is up
    try:
        auction = {
            "id": aid,
            "nonce": secrets.token_hex(4),  # tells this auction's buttons apart from an older one with the same number
            "guild_id": interaction.guild_id,
            "channel_id": interaction.channel_id,
            "message_id": 0,
            "creator_id": interaction.user.id,
            "creator_name": clean(interaction.user.display_name, 32),
            "creator_avatar": interaction.user.display_avatar.url,
            "title": clean(title, 80) if title else None,
            "currency": clean_currency(currency) if currency else DEFAULT_CURRENCY,
            "min_increment": min_increment,
            "anti_snipe": anti_snipe,
            "extensions": 0,
            "bid_count": 0,
            "started_at": now,
            "duration": seconds,  # the original length: the 50% / 75% DMs are measured against it
            "notified": [],  # which of those DMs have already gone out
            "ends_at": now + seconds,
            "items": [
                {"name": n, "start": s, "bid": 0, "bidder_id": None, "bidder_name": None, "bidders": {}}
                for n, s in zip(names, starts)
            ],
        }
        await interaction.response.send_message(embed=auction_embed(auction), view=bid_view(auction))
        auction["message_id"] = (await interaction.original_response()).id
        bot.store.auctions[aid] = auction
        bot.store.save()
    finally:
        bot.store.reserved.discard(aid)


@bot.tree.command(name="bid", description="Bid on an auction item")
@app_commands.guild_only()
@app_commands.describe(item="The item to bid on (pick it from the list)", amount_value="Your bid. It has to beat the current bid by the minimum raise", name="Custom name to bid under (defaults to your display name)")
async def bid(interaction: discord.Interaction, item: str, amount_value: app_commands.Range[int, 1, MAX_BID], name: Optional[app_commands.Range[str, 1, 32]] = None):
    match = re.fullmatch(r"(\d+):(\d+)", item.strip())
    auction = bot.store.auctions.get(int(match[1])) if match else None
    if match:
        index = int(match[2])
    else:
        found = [(a, i) for a in bot.store.auctions.values() if a["guild_id"] == interaction.guild_id for i, it in enumerate(a["items"]) if it["name"].lower() == item.lower()]
        if len(found) != 1:
            return await reject(interaction, "Choose an unambiguous item name, or pick one from the list as you type.")
        auction, index = found[0]
    if not auction or auction["guild_id"] != interaction.guild_id or not 0 <= index < len(auction["items"]):
        return await reject(interaction, "I couldn't find that auction item in this server.")
    await bot.place_bid(interaction, auction, index, amount_value, name)


@bid.autocomplete("item")
async def bid_autocomplete(interaction: discord.Interaction, current: str):
    # Discord shows at most 25 suggestions, so with a big auction keep typing to narrow it down.
    now, rows = time.time(), []
    for auction in bot.store.auctions.values():
        if auction["guild_id"] != interaction.guild_id or auction["ends_at"] <= now:
            continue
        for i, item in enumerate(auction["items"]):
            price = "no bids" if item["bidder_id"] is None else money(auction, item["bid"])
            text = f"{tag(auction)[:28]} · {item['name'][:40]} ({price})"[:100]
            if current.lower() in text.lower():
                here = auction["channel_id"] != interaction.channel_id  # this channel's items first
                rows.append((here, auction["ends_at"], app_commands.Choice(name=text, value=f"{auction['id']}:{i}")))
    rows.sort(key=lambda r: r[:2])
    return [r[2] for r in rows[:25]]


@bot.tree.command(name="auctions", description="List running auctions in this server")
@app_commands.guild_only()
async def auctions(interaction: discord.Interaction):
    active = sorted((a for a in bot.store.auctions.values() if a["guild_id"] == interaction.guild_id), key=lambda a: a["ends_at"])
    if not active:
        return await reply(interaction, "No auctions are running right now. Start one with `/create_auction`.")
    lines = [
        f"{label(a)}\n{plural(len(a['items']), 'item')} · {plural(a['bid_count'], 'bid')} · "
        f"ends <t:{int(a['ends_at'])}:R> · [View]({jump_url(a)})"
        for a in active
    ]
    await send_pages(interaction, "Running auctions", list(chunk_lines(lines, 3600, "\n\n")))


@bot.tree.command(name="mybids", description="See your standing bids in running auctions")
@app_commands.guild_only()
async def mybids(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    groups, winning, outbid = [], 0, 0
    for auction in bot.store.auctions.values():
        if auction["guild_id"] != interaction.guild_id:
            continue
        rows = []
        for n, item in enumerate(auction["items"], 1):
            best = item["bidders"].get(uid)
            if best is None:
                continue
            if item["bidder_id"] == interaction.user.id:
                winning += 1
                status = "**Winning**"
            else:
                outbid += 1
                status = f"Outbid, now **{money(auction, item['bid'])}**"
            rows.append(f"**{n}.** {safe(item['name'])} · your best **{money(auction, best)}** · {status}")
        if rows:
            groups.append((f"{label(auction)} · ends <t:{int(auction['ends_at'])}:R> · [View]({jump_url(auction)})", rows))
    if not groups:
        return await reply(interaction, "You haven't bid in any running auctions.")
    await send_pages(interaction, "Your bids", paginate_groups(groups), footer=f"{winning} winning · {outbid} outbid")


async def manageable(interaction: discord.Interaction, aid: int):
    auction = bot.store.auctions.get(aid)
    if not auction or auction["guild_id"] != interaction.guild_id:
        await reject(interaction, "I can't find a running auction with that number here.")
        return None
    if interaction.user.id != auction["creator_id"] and not interaction.user.guild_permissions.manage_guild:
        await reject(interaction, "Only the host (or someone with Manage Server) can do that.")
        return None
    return auction


@bot.tree.command(name="end_auction", description="End your auction early and announce winners")
@app_commands.guild_only()
@app_commands.describe(auction_id="The auction number (see /auctions)")
async def end_auction(interaction: discord.Interaction, auction_id: int):
    if await manageable(interaction, auction_id):
        await reply(interaction, f"Ending auction #{auction_id}…")
        await bot.close_auction(auction_id)


@bot.tree.command(name="cancel_auction", description="Cancel your auction without winners")
@app_commands.guild_only()
@app_commands.describe(auction_id="The auction number (see /auctions)")
async def cancel_auction(interaction: discord.Interaction, auction_id: int):
    if await manageable(interaction, auction_id):
        await reply(interaction, f"Cancelling auction #{auction_id}…")
        await bot.close_auction(auction_id, cancelled=True)


@bot.tree.command(name="set_currency", description="Rename the currency on your running auction")
@app_commands.guild_only()
@app_commands.describe(auction_id="The auction number (see /auctions)", currency="New currency name, e.g. $, 🪙 or gold. Type none to remove it")
async def set_currency(interaction: discord.Interaction, auction_id: int, currency: app_commands.Range[str, 1, MAX_CURRENCY]):
    auction = await manageable(interaction, auction_id)
    if not auction:
        return
    auction["currency"] = None if currency.strip().lower() == "none" else clean_currency(currency)
    bot.store.save()
    bot.dirty.add(auction_id)  # the post redraws with the new name within a couple of seconds
    if auction["currency"]:
        await reply(interaction, f"Prices in {label(auction)} now use **{auction['currency']}**.", "ok", "Currency updated")
    else:
        await reply(interaction, f"Prices in {label(auction)} now show as plain numbers.", "ok", "Currency removed")


@bot.tree.command(name="auction_help", description="Show auction bot help")
async def auction_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Mock auctions",
        description="Bid on items, stay in the lead, and win them when the clock runs out.",
        color=GOLD,
    )
    embed.add_field(
        name="Start one",
        value=(
            "`/create_auction items:Golden Duck, Cursed Spoon duration_text:2h`\n"
            "List as many items as you like, and give each its own opening bid with "
            "`starting_bids:50, 100, 25`. Optional extras: a title, a minimum raise, a currency "
            "name like `gold` or `$`, and anti-snipe (late bids push the end back)."
        ),
        inline=False,
    )
    embed.add_field(
        name="Bid",
        value=(
            "Tap **Place a bid** on the auction post, or use `/bid` from any channel. Pick an item, "
            "enter an amount that beats the current bid, and optionally bid under a custom name. "
            "The bot announces your bid under that name, and your confirmation is private."
        ),
        inline=False,
    )
    embed.add_field(
        name="Direct messages",
        value=(
            "You get a DM when someone outbids you (it never says who) and when you raise your own "
            "bid, and again when an auction you bid in is halfway through and when 75% of its time is gone. "
            "Keep DMs from server members switched on to receive them."
        ),
        inline=False,
    )
    embed.add_field(
        name="Check in",
        value=(
            "`/mybids` shows whether you're winning. `/auctions` lists what's running. "
            "A post turns orange once its auction enters the final quarter."
        ),
        inline=False,
    )
    embed.add_field(
        name="Winners",
        value="When time is up the bot posts the results and pings whoever won each item.",
        inline=False,
    )
    embed.add_field(
        name="Hosts",
        value="`/end_auction` closes one early. `/cancel_auction` calls it off. `/set_currency` renames the currency.",
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    await report_error(interaction, getattr(error, "original", error))


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_TOKEN in your hosting environment or .env file.")
    bot.run(token)
