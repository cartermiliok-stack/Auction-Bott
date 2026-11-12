FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY auction_bot.py .
ENV AUCTION_DATA_FILE=/data/auctions.json
VOLUME ["/data"]
CMD ["python", "auction_bot.py"]
