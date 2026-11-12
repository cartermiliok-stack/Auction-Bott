FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AUCTION_DATA_FILE=/data/auctions.json

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY auction_bot.py .

# Only needed if you set KEEP_AUCTIONS=1: mount a persistent volume at /data so running
# auctions survive restarts. Without it, no volume is required.
RUN mkdir -p /data

CMD ["python", "auction_bot.py"]
