# Discord Mock Auction Bot

A Discord bot for mock auctions. Start an auction with several items, give each its own opening bid,
let people bid from any channel under custom names, and the bot pings the winners when time is up.

## Commands
`/create_auction`, `/bid`, `/mybids`, `/auctions`, `/set_currency`, `/end_auction`, `/cancel_auction`, `/auction_help`

Example: `/create_auction items:Golden Duck, Cursed Spoon, Mystery Box duration_text:2h starting_bids:50, 100, 25 currency:gold`

## Run it locally
1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and put your bot token in it.
3. `python auction_bot.py`

## Settings (environment variables)
| Variable | Purpose |
| --- | --- |
| `DISCORD_TOKEN` | Your bot token (required, never commit it) |
| `GUILD_ID` | Optional: your server ID, for instant slash-command updates |
| `DEFAULT_CURRENCY` | Optional: currency name used when an auction doesn't set one |
| `KEEP_AUCTIONS` | Optional: `1` saves running auctions and resumes them after a restart. Off by default |
| `AUCTION_DATA_FILE` | Only used when `KEEP_AUCTIONS=1`. Defaults to `/data/auctions.json` in Docker |
| `LOG_LEVEL` | Optional: `INFO` (default) or `DEBUG` |

## Hosting notes
The repo includes a `Dockerfile`, so any host that runs Docker or deploys from GitHub can run it.

By default nothing is carried over between runs: if the bot restarts or is redeployed, any auction that
was running just disappears without results, so avoid redeploying mid-auction. If you would rather resume
auctions after a restart, set `KEEP_AUCTIONS=1` and mount a persistent volume at `/data`.

Run only one copy of the bot per token. Two copies will both answer every command.
