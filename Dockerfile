# Use a lightweight Python image
FROM python:3.10-slim

# Set the working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Ensure the uploads directory exists with correct permissions
RUN mkdir -p static/uploads && chmod 777 static/uploads

# Expose the port
EXPOSE 5000

# Run Gunicorn: 1 worker, 8 threads to preserve in-memory state
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--bind", "0.0.0.0:5000", "app:app"]
