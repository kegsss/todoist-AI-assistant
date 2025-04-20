# Use the official lightweight Python image
FROM python:3.11-slim

# Ensure stdout/stderr is unbuffered (helps with logging)
ENV PYTHONUNBUFFERED=1

# Set working directory inside the container
WORKDIR /app

# Copy only requirements first to leverage Docker cache
COPY requirements.txt ./

# Upgrade pip and install dependencies
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# Copy the rest of the codebase
COPY . .

# Expose the port FastAPI will run on. expose is optional on Render, but harmless
EXPOSE 8000

# shell form so $PORT is expanded
CMD uvicorn app:app --host 0.0.0.0 --port $PORT
