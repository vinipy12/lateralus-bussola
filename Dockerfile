FROM python:3.13-slim
WORKDIR /app
COPY swipe.py swipe.html store.py profiles.py ./
ENV PYTHONUNBUFFERED=1
EXPOSE 8765
CMD ["python3", "swipe.py"]
