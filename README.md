# Auction Bot

A Discord auction bot using Python 3.10+ and discord.py 2.4+. Auction state is stored as JSON and can be preserved by mounting a volume when deploying.

## Run locally

1. Create a Discord application and bot at <https://discord.com/developers/applications>.
2. Invite it with the `bot` and `applications.commands` scopes. Grant View Channels, Send Messages, Send Messages in Threads, and Embed Links.
3. Copy `.env.example` to `.env`, set `DISCORD_TOKEN` to a **newly generated** bot token, then install and run:

   ```sh
   python -m pip install -r requirements.txt
   python auction_bot.py
   ```

Set `GUILD_ID` while testing to sync slash commands immediately to one test server; omit it for global sync. Never commit `.env` or paste a bot token into GitHub.

## Docker hosting

Build and run with a persistent data volume and pass the token as a runtime secret/environment variable:

```sh
docker build -t auction-bot .
docker run -d --name auction-bot --restart unless-stopped \
  -e DISCORD_TOKEN='YOUR_NEW_TOKEN' \
  -v auction-data:/data auction-bot
```

Configure the same environment variables in your hosting provider's secret settings. Keep the `/data` volume attached so auction data survives restarts.

## Commands

- `/create_auction` — create an auction with comma-separated items and a duration such as `30m`, `2h`, or `1d`.
- `/bid` — bid on an item; autocomplete lists active auction items. You can also use the button on an auction post.
- `/auctions`, `/mybids`, `/auction_help` — view current auctions, your bids, and help.
- `/end_auction` and `/cancel_auction` — host or Manage Server controls.

The bot supports custom bidder names, minimum increments, anti-snipe extensions, JSON persistence, and winner announcements. An auction can hold any number of items; when they don't all fit on the post, a **View all items** button opens a paged list. The post refreshes as bids come in and turns orange once the auction enters its final quarter.

## Direct messages

The bot sends these DMs on a best-effort basis. Anyone who has turned off DMs from server members simply won't receive them, and nothing else is affected.

- **Outbid:** when someone beats your bid you get the item, the new top bid, and the minimum needed to retake the lead. It never says who outbid you.
- **Raised:** when you raise your own top bid you get the previous and new amounts.
- **Time checks:** everyone who has bid gets a DM when 50% and again when 75% of an auction's running time has passed (a 100-minute auction sends them at 50 and 25 minutes left), showing where their bids stand. These marks are measured against the auction's original length, so anti-snipe extensions don't move them. Auctions that were already running before this feature was added don't send them.
