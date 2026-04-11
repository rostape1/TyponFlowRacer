FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY *.py ./
COPY static/ static/

EXPOSE 8080

# Default: auto-detect AIS source (falls back to AISstream if key is set)
# Override with --demo or --aisstream via fly.toml
CMD ["python", "main.py"]
