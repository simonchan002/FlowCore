### `api/Dockerfile`

#dockerfile
FROM python:3.12-slim

WORKDIR /app

# Copy and install dependencies first — Docker layer caching means this
# layer is only rebuilt when requirements.txt changes, not on every code change.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
