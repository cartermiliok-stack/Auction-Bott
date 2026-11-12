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

The bot supports custom bidder names, minimum increments, anti-snipe extensions, outbid DMs, JSON persistence, and winner announcements.
