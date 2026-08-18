FROM python:3.11-slim
WORKDIR /app
COPY dcg.py .
RUN pip install python-telegram-bot discord.py-self anthropic httpx
CMD ["python", "dcg.py"]
