# CN-KH AI
Chinese-to-Khmer video translation web app and Android WebView application.

## Features
- Khmer mobile-friendly interface
- Gemini video transcription and Khmer SRT translation
- Character diarization with Khmer neural male/female voices
- MP3 and MP4 export
- OCR of embedded Chinese text, black cover and Khmer replacement
- Android APK build through GitHub Actions

## Render configuration
Build command:
`apt-get update && apt-get install -y ffmpeg fonts-noto-core && pip install -r requirements.txt`

Start command:
`gunicorn app:app --workers 1 --threads 4 --timeout 1200`

Health check: `/health`

Users enter their own Gemini API key. Never commit API keys.

## Android APK
Push to `main` or run the **Build CN-KH AI Android APK** workflow manually in GitHub Actions. The workflow publishes an installable APK artifact and GitHub Release. The Android app loads `https://cn-kh-ai-api.onrender.com/`.
