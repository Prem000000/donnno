FROM python:3.11-slim

WORKDIR /app

# Copy the bot file
COPY dcg.py .

# Install dependencies directly
RUN pip install python-telegram-bot discord.py-self anthropic httpx

# Run the bot
CMD ["python", "dcg.py"]
