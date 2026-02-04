# Use Playwright's official Python image with browsers preinstalled
FROM mcr.microsoft.com/playwright/python:latest

WORKDIR /app

# Copy application files
COPY . /app

# Upgrade pip and install Python deps
RUN python -m pip install --upgrade pip
RUN pip install -r requirements.txt

# Expose port used by uvicorn
ENV PORT=8080
EXPOSE 8080

# Run the FastAPI app
CMD ["uvicorn", "render_service:app", "--host", "0.0.0.0", "--port", "8080", "--loop", "asyncio"]