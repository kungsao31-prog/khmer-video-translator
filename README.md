# CN-KH AI

Chinese-to-Khmer video translation web app.

## Features
- Mobile-friendly Khmer UI
- Video upload with upload progress
- Gemini video transcription and Khmer SRT translation
- Khmer MP3 narration using gTTS
- Chinese text-to-Khmer translation
- Health endpoint at /health

## Deploy on Render
Create a Python web service from this repository:
- Build: pip install -r requirements.txt
- Start: gunicorn app:app --workers 1 --threads 4 --timeout 600
- Health check: /health

Users enter their own Gemini API key in the app. Do not commit API keys.
