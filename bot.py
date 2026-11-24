"""
Hypixel SkyBlock change tracker - Discord bot

Setup:
    pip install discord.py aiohttp
    export DISCORD_TOKEN="your-bot-token"
    export CHANNEL_ID="123456789012345678"
    export HYPIXEL_KEY="optional-api-key"      # resource + bazaar endpoints work without one
    python bot.py
"""

import asyncio
import io
import json
import os
import time
import re

from discord import app_commands
import aiohttp
import discord
from discord.ext import tasks

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
API = "https://api.hypixel.net/v2"
WIKI = "https://wiki.hypixel.net/api.php"
SNAP_DIR = os.getenv("SNAP_DIR", "snapshots")
POLL_MINUTES = 5
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
HYPIXEL_KEY = os.getenv("HYPIXEL_KEY", "")

# Bazaar spike alerts: a +1000% rise means the price is now 11x the baseline.
BAZAAR_SPIKE_PCT = 900          # alert when buy price is up at least this much
BAZAAR_WINDOW_HOURS = 48         # ...compared with the lowest price seen in this window
BAZAAR_MIN_BASE_PRICE = 500.0      # ignore tiny/zero baselines (empty order books)
BAZAAR_SAMPLE_MINUTES = 30       # how often a price point is stored in the history


os.makedirs(SNAP_DIR, exist_ok=True)


# ----------------------------------------------------------------------------
# Snapshot storage + diffing
# ----------------------------------------------------------------------------
def load(name):
    try:
        with open(f"{SNAP_DIR}/{name}.json", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save(name, data):
    tmp = f"{SNAP_DIR}/{name}.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, f"{SNAP_DIR}/{name}.json")  # atomic: no half-written snapshots


def flatten(obj, prefix=""):
    """Turn nested JSON into {'a.b.c': value} so diffs are trivial."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}{k}."))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            key = (v.get("id") or v.get("name") or i) if isinstance(v, dict) else i
            out.update(flatten(v, f"{prefix}{key}."))
    else:
        out[prefix.rstrip(".")] = obj
    return out


def diff(old, new):
    o, n = flatten(old), flatten(new)
    added = [k for k in n if k not in o]
    removed = [k for k in o if k not in n]
    changed = [(k, o[k], n[k]) for k in n if k in o and o[k] != n[k]]
    return added, removed, changed


async def get_json(session, path, **params):
    headers = {"API-Key": HYPIXEL_KEY} if HYPIXEL_KEY else {}
    async with session.get(f"{API}{path}", params=params, headers=headers) as r:
        r.raise_for_status()
        data = await r.json()
    if not data.get("success", False):
        raise RuntimeError(f"{path} returned success=false")
    return data


# ----------------------------------------------------------------------------
# Checkers - each returns a list of messages (strings) or (text, file) tuples
# ----------------------------------------------------------------------------
async def check_resource(session, name, path, strip=("lastUpdated", "success")):
    """Collections / skills (and anything else shaped like a nested resource)."""
    data = await get_json(session, path)
    for k in strip:
        data.pop(k, None)
    old = load(name)
    save(name, data)
    if old is None:  # first run: baseline only, don't spam
        return []

    msgs = []
    if old.get("version") != data.get("version"):
        msgs.append(
            f"🆕 **SkyBlock version** (via `{name}`): "
            f"`{old.get('version')}` → `{data.get('version')}`"
        )

    added, removed, changed = diff(old, data)
    changed = [c for c in changed if c[0] != "version"]
    if added:
        msgs.append(f"➕ **{name}** added: " + ", ".join(f"`{k}`" for k in added[:25]))
    if removed:
        msgs.append(f"➖ **{name}** removed: " + ", ".join(f"`{k}`" for k in removed[:25]))
    if changed:
        body = "\n".join(f"{k}: {a} -> {b}" for k, a, b in changed)
        if len(changed) <= 10:
            msgs.append(f"✏️ **{name}** changed:\n```\n{body}\n```")
        else:
            msgs.append((f"✏️ **{name}**: {len(changed)} values changed (full diff attached)",
                         (f"{name}_diff.txt", body)))
    return msgs


async def check_items(session):
    data = await get_json(session, "/resources/skyblock/items")
    items = {i["id"]: i for i in data["items"]}
    version = data.get("version")
    old = load("items")
    old_version = load("items_version")
    save("items", items)
    save("items_version", version)
    if old is None:
        return []

    msgs = []
    if old_version != version:
        msgs.append(f"🆕 **SkyBlock version** (via `items`): `{old_version}` → `{version}`")

    new_ids = [i for i in items if i not in old]
    removed = [i for i in old if i not in items]
    edited = [
        i for i in items
        if i in old and json.dumps(items[i], sort_keys=True) != json.dumps(old[i], sort_keys=True)
    ]
    if new_ids:
        text = "🗡️ **New items:** " + ", ".join(f"`{i}`" for i in new_ids)
        msgs.append(text if len(text) < 1900 else
                    (f"🗡️ **{len(new_ids)} new items** (full list attached)",
                     ("new_items.txt", "\n".join(new_ids))))
    if removed:
        msgs.append(f"🗑️ **{len(removed)} items removed**, e.g. "
                    + ", ".join(f"`{i}`" for i in removed[:10]))
    if edited:
        msgs.append(f"🔧 **{len(edited)} items modified**, e.g. "
                    + ", ".join(f"`{i}`" for i in edited[:10]))
    return msgs


async def check_bazaar(session):
    """New Bazaar products + products whose buy price spiked >= BAZAAR_SPIKE_PCT."""
    data = await get_json(session, "/skyblock/bazaar")
    products = data["products"]
    ids = sorted(products.keys())
    if not ids:  # never overwrite a snapshot with empty data
        return []

    old_ids = load("bazaar")
    history = load("bazaar_history") or {}
    alerted = set(load("bazaar_alerted") or [])
    now = time.time()
    cutoff = now - BAZAAR_WINDOW_HOURS * 3600
    multiplier = 1 + BAZAAR_SPIKE_PCT / 100
    spikes = []
    new_history = {}

    for pid, prod in products.items():
        price = (prod.get("quick_status") or {}).get("buyPrice") or 0
        points = [p for p in history.get(pid, []) if p[0] >= cutoff]

        if price > 0 and old_ids is not None:  # no alerts on the baseline run
            bases = [p[1] for p in points if p[1] >= BAZAAR_MIN_BASE_PRICE]
            if bases:
                base = min(bases)
                ratio = price / base
                if ratio >= multiplier:
                    if pid not in alerted:
                        alerted.add(pid)
                        spikes.append((pid, base, price, (ratio - 1) * 100))
                else:
                    alerted.discard(pid)  # price came back down: re-arm the alert

        #   store a downsampled history so the snapshot file stays small
        if price > 0 and (not points or now - points[-1][0] >= BAZAAR_SAMPLE_MINUTES * 60):
            points.append([now, price])
        if points:
            new_history[pid] = points

    alerted &= set(products)
    save("bazaar", ids)
    save("bazaar_history", new_history)
    save("bazaar_alerted", sorted(alerted))
    if old_ids is None:
        return []

    msgs = []
    new = sorted(set(ids) - set(old_ids))
    if new:
        msgs.append("💰 **New Bazaar products:** " + ", ".join(f"`{p}`" for p in new))

    spikes.sort(key=lambda x: x[3], reverse=True)
    for pid, base, price, pct in spikes[:15]:
        msgs.append(
            f"🚀 **Bazaar spike:** `{pid}` buy price is up **{pct:,.0f}%** "
            f"in the last {BAZAAR_WINDOW_HOURS}h ({base:,.1f} → {price:,.1f} coins)"
        )
    if len(spikes) > 15:
        msgs.append(f"🚀 …and {len(spikes) - 15} more Bazaar spikes")
    return msgs


async def check_news(session):
    """New SkyBlock news / patch-note posts."""
    data = await get_json(session, "/skyblock/news")
    entries = data.get("items") or []
    if not entries:
        return []

    def key(e):
        return e.get("link") or e.get("title") or e.get("text")

    seen = load("news_seen")
    save("news_seen", sorted(set(seen or []) | {key(e) for e in entries if key(e)}))
    if seen is None:  # baseline run
        return []

    msgs = []
    for e in reversed(entries):  # API lists newest first; post oldest first
        if key(e) and key(e) not in seen:
            title = e.get("title") or "SkyBlock news"
            text = e.get("text") or ""
            link = e.get("link") or ""
            msgs.append(f"📰 **{title}**" + (f" — {text}" if text else "") + (f"\n{link}" if link else ""))
    return msgs


def _clean(text):
    """Strip Minecraft colour codes (§a etc.) and collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"§.", "", text or "")).strip()


async def check_bingo(session):
    """New Bingo events, plus goal changes within the current event."""
    data = await get_json(session, "/resources/skyblock/bingo")
    for k in ("lastUpdated", "success"):
        data.pop(k, None)
    old = load("bingo")
    save("bingo", data)
    if old is None:
        return []

    goals = data.get("goals") or []
    if old.get("id") != data.get("id"):
        header = f"🎲 **New SkyBlock Bingo: {data.get('name', data.get('id'))}**"
        if data.get("start") and data.get("end"):
            header += (f"\nStarts <t:{int(data['start']) // 1000}:F> · "
                       f"ends <t:{int(data['end']) // 1000}:R>")
        if data.get("modifier"):
            header += f"\nModifier: `{data['modifier']}`"
        lines = [f"• **{_clean(g.get('name'))}** — {_clean(g.get('lore'))}" for g in goals]
        body = "\n".join(lines)
        if len(header) + len(body) < 1900:
            return [f"{header}\n{body}"]
        return [(f"{header}\n{len(goals)} goals (full list attached)", ("bingo_goals.txt", body))]

    # same event: look for goals added, removed, or edited
    def goal_map(gs):
        return {g.get("id") or g.get("name"): {k: v for k, v in g.items() if k != "progress"}
                for g in gs}

    og, ng = goal_map(old.get("goals") or []), goal_map(goals)
    added = [ng[k].get("name") or k for k in ng if k not in og]
    removed = [og[k].get("name") or k for k in og if k not in ng]
    changed = [ng[k].get("name") or k for k in ng if k in og and ng[k] != og[k]]
    msgs = []
    if added:
        msgs.append("🎲 **Bingo goals added:** " + ", ".join(_clean(x) for x in added))
    if removed:
        msgs.append("🎲 **Bingo goals removed:** " + ", ".join(_clean(x) for x in removed))
    if changed:
        msgs.append("🎲 **Bingo goals changed:** " + ", ".join(_clean(x) for x in changed))
    return msgs


async def check_achievements(session):
    data = await get_json(session, "/resources/achievements")
    sb = data["achievements"]["skyblock"]
    old = load("achievements")
    save("achievements", sb)
    if old is None:
        return []

    added, removed, changed = diff(old, sb)
    msgs = []
    if added:
        msgs.append("🏆 **Achievements** added: " + ", ".join(f"`{k}`" for k in added[:25]))
    if removed:
        msgs.append("🏆 **Achievements** removed: " + ", ".join(f"`{k}`" for k in removed[:25]))
    if changed:
        msgs.append("🏆 **Achievements** changed: " + ", ".join(f"`{k}`" for k, *_ in changed[:25]))
    return msgs


CHECKS = [
    ("collections", lambda s: check_resource(s, "collections", "/resources/skyblock/collections")),
    ("skills", lambda s: check_resource(s, "skills", "/resources/skyblock/skills")),
    ("items", check_items),
    ("bazaar", check_bazaar),
    ("news", check_news),
    ("bingo", check_bingo),
    ("achievements", check_achievements),
]


# ----------------------------------------------------------------------------
# Discord bot
# ----------------------------------------------------------------------------
intents = discord.Intents.default()
bot = discord.Client(intents=intents)


async def post(channel, msg):
    """Send a string, or a (text, (filename, content)) tuple with an attachment."""
    no_pings = discord.AllowedMentions.none()
    if isinstance(msg, tuple):
        text, (fname, content) = msg
        file = discord.File(io.BytesIO(content.encode("utf-8")), filename=fname)
        await channel.send(text[:2000], file=file, allowed_mentions=no_pings)
    else:
        await channel.send(msg[:2000], allowed_mentions=no_pings)


@tasks.loop(minutes=POLL_MINUTES)
async def poll():
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        print("Can't see the channel: check CHANNEL_ID and the bot's permissions")
        return

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        results = await asyncio.gather(*(fn(session) for _, fn in CHECKS),
                                       return_exceptions=True)

    for (name, _), result in zip(CHECKS, results):
        if isinstance(result, Exception):
            print(f"[{name}] check failed: {result!r}")
            continue
        for msg in result:
            try:
                await post(channel, msg)
            except discord.HTTPException as e:
                print(f"[{name}] failed to send: {e!r}")
            await asyncio.sleep(0.5)  # be gentle with Discord's rate limits


@poll.before_loop
async def before_poll():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        print("Can't see the channel: check CHANNEL_ID and the bot's permissions")
        return
    await channel.send("✅ SkyBlock tracker online")
    if not poll.is_running():
        poll.start()


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
