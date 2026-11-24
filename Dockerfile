
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Prevent Python from buffering logs
ENV PYTHONUNBUFFERED=1

# Copy and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire repository
COPY . .

# Create snapshots directory
RUN mkdir -p snapshots

# Start the Discord bot
CMD ["python", "-u", "bot.py"]
