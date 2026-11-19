import os, re, tempfile, threading, uuid, logging, time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, request, jsonify, send_file, render_template
from google import genai
from gtts import gTTS

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024
app.config["JSON_AS_ASCII"] = False
logging.basicConfig(level=logging.INFO)

JOBS = {}
LOCK = threading.Lock()
OUTPUT_DIR = Path(tempfile.gettempdir()) / "cnkh_jobs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

LANGUAGES = {
    "auto": "the language spoken in the video",
    "zh": "Chinese", "en": "English", "th": "Thai", "ko": "Korean",
    "ja": "Japanese", "vi": "Vietnamese", "lo": "Lao"
}

def update_job(job_id, **values):
    with LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(values)

def generate_with_retry(client, *, model, contents, on_retry=None):
    """Retry temporary Gemini capacity and rate-limit errors."""
    delays = (2, 4, 8, 16)
    for attempt in range(len(delays) + 1):
        try:
            return client.models.generate_content(model=model, contents=contents)
        except Exception as exc:
            message = str(exc).lower()
            temporary = any(token in message for token in (
                "503", "unavailable", "high demand", "429", "resource_exhausted",
                "500", "502", "504", "internal error", "temporarily"
            ))
            if not temporary or attempt >= len(delays):
                raise
            wait_seconds = delays[attempt]
            if on_retry:
                on_retry(attempt + 1, len(delays), wait_seconds)
            time.sleep(wait_seconds)


def srt_to_text(srt):
    lines = []
    for line in srt.splitlines():
        value = line.strip()
        if not value or value.isdigit() or "-->" in value:
            continue
        if re.match(r"^\d{2}:\d{2}:\d{2}", value):
            continue
        lines.append(value)
    return " ".join(lines).strip()

def process_video(job_id, video_path, api_key, source_language):
    uploaded = None
    client = genai.Client(api_key=api_key)
    try:
        update_job(job_id, status="processing", progress=12, message="កំពុងបញ្ជូនវីដេអូទៅ AI...")
        uploaded = client.files.upload(file=video_path)
        update_job(job_id, progress=25, message="កំពុងរង់ចាំ AI រៀបចំវីដេអូ...")
        for _ in range(90):
            uploaded = client.files.get(name=uploaded.name)
            state = str(getattr(uploaded, "state", "")).upper()
            if state.endswith("ACTIVE"):
                break
            if state.endswith("FAILED"):
                raise RuntimeError("AI មិនអាចដំណើរការវីដេអូនេះបានទេ។")
            import time
            time.sleep(2)
        else:
            raise TimeoutError("ការរៀបចំវីដេអូចំណាយពេលយូរពេក។ សូមសាកល្បងវីដេអូខ្លីជាងនេះ។")

        update_job(job_id, progress=42, message="កំពុងស្តាប់ និងបកប្រែភាសាចិនទៅខ្មែរ...")
        source = LANGUAGES.get(source_language, LANGUAGES["auto"])
        prompt = (
            f"Listen to the spoken dialogue in this video. The source language is {source}. "
            "Transcribe and translate every spoken line into natural, faithful Khmer. "
            "Return ONLY valid SRT subtitles, with sequential numbers and accurate "
            "HH:MM:SS,mmm timestamps. Keep each subtitle concise and preserve meaning. "
            "Do not include markdown fences or explanations."
        )
        response = generate_with_retry(
            client, model=GEMINI_MODEL, contents=[uploaded, prompt],
            on_retry=lambda attempt, total, wait: update_job(
                job_id, progress=42,
                message=f"AI កំពុងមមាញឹក។ កម្មវិធីនឹងសាកល្បងម្ដងទៀត ({attempt}/{total})..."
            )
        )
        srt = (response.text or "").strip()
        if not srt:
            raise RuntimeError("AI មិនបានផ្តល់អត្ថបទបកប្រែទេ។")
        transcript = srt_to_text(srt)
        if not transcript:
            raise RuntimeError("មិនអាចរកឃើញអត្ថបទបកប្រែសម្រាប់បង្កើតសំឡេងទេ។")

        update_job(job_id, progress=72, message="បង្កើតសំឡេងនិយាយភាសាខ្មែរ...")
        audio_path = OUTPUT_DIR / f"{job_id}.mp3"
        gTTS(text=transcript, lang="km", slow=False).save(str(audio_path))
        update_job(job_id, progress=100, status="completed",
                   message="បកប្រែ និងបង្កើតសំឡេងខ្មែររួចរាល់។",
                   translation_result=srt, transcript=transcript,
                   audio_url=f"/api/jobs/{job_id}/audio")
    except Exception as exc:
        app.logger.exception("CN-KH job %s failed", job_id)
        update_job(job_id, status="failed", progress=0,
                   message="ដំណើរការមិនបានសម្រេច។",
                   error=str(exc))
    finally:
        try:
            os.remove(video_path)
        except OSError:
            pass
        if uploaded:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                app.logger.warning("Could not delete temporary Gemini upload")

@app.get("/")
def home():
    return render_template("index.html")

@app.get("/health")
def health():
    return jsonify({"status": "online", "app": "CN-KH AI", "model": GEMINI_MODEL, "time": datetime.now(timezone.utc).isoformat()})

@app.post("/api/jobs")
def create_job():
    video = request.files.get("video")
    api_key = (request.form.get("api_key") or "").strip()
    source_language = request.form.get("source_language", "auto")
    if not api_key:
        return jsonify({"error": "សូមបញ្ចូល Gemini API Key ជាមុន។"}), 400
    if not video or not video.filename:
        return jsonify({"error": "សូមជ្រើសរើសវីដេអូជាមុន។"}), 400
    ext = Path(video.filename).suffix.lower()
    if ext not in {".mp4", ".mov", ".m4v", ".webm", ".mpeg", ".mpg"}:
        return jsonify({"error": "សូមប្រើឯកសារវីដេអូ MP4, MOV ឬ WEBM។"}), 415
    job_id = str(uuid.uuid4())
    fd, path = tempfile.mkstemp(suffix=ext)
    os.close(fd)
    video.save(path)
    with LOCK:
        JOBS[job_id] = {"status": "queued", "progress": 5, "message": "ទទួលបានវីដេអូហើយ...", "created_at": datetime.now(timezone.utc).isoformat()}
    thread = threading.Thread(target=process_video, args=(job_id, path, api_key, source_language), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id}), 202

@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "រកមិនឃើញការងារនេះទេ។"}), 404
        return jsonify(job)

@app.get("/api/jobs/<job_id>/audio")
def job_audio(job_id):
    path = OUTPUT_DIR / f"{job_id}.mp3"
    if not path.exists():
        return jsonify({"error": "សំឡេងមិនទាន់រួចរាល់ ឬផុតកំណត់។"}), 404
    return send_file(path, mimetype="audio/mpeg", as_attachment=True, download_name="CN-KH-AI-Khmer.mp3")

@app.post("/api/translate-text")
def translate_text():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    api_key = (data.get("api_key") or "").strip()
    if not api_key or not text:
        return jsonify({"error": "សូមបញ្ចូល Gemini API Key និងអត្ថបទចិន។"}), 400
    try:
        client = genai.Client(api_key=api_key)
        result = generate_with_retry(
            client, model=GEMINI_MODEL,
            contents=f"Translate the following text into natural Khmer. Return only the Khmer translation:\n\n{text}"
        )
        return jsonify({"translation": result.text or ""})
    except Exception as exc:
        app.logger.exception("Text translation failed")
        return jsonify({"error": str(exc)}), 500

@app.errorhandler(413)
def too_large(_):
    return jsonify({"error": "វីដេអូធំពេក។ អតិបរមា 100 MB។"}), 413

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
