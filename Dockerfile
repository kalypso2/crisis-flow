FROM python:3.11-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

# Which ADK app to serve (overridden per Cloud Run service)
ENV ADK_APP=crisisflow
ENV PORT=8080

# ADK api_server serves the REST/A2A interface (no browser UI needed for Cloud Run)
CMD python server.py
