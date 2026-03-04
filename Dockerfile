FROM python:3.14-slim

WORKDIR /app

# Create a non-root user
RUN groupadd -r loko && useradd --no-log-init -r -g loko loko

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Use non-root user
USER loko

ENTRYPOINT ["kopf", "run", "main.py"]
