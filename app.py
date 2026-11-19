import os, re, tempfile, threading, uuid, logging, time, json, subprocess, shutil, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, request, jsonify, send_file, render_template
from google import genai
from PIL import ImageFont

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024
app.config["JSON_AS_ASCII"] = False
logging.basicConfig(level=logging.INFO)

JOBS = {}
LOCK = threading.Lock()
OUTPUT_DIR = Path(tempfile.gettempdir()) / "cnkh_jobs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
KHMER_FONT = os.environ.get("KHMER_FONT", "/usr/share/fonts/truetype/noto/NotoSansKhmer-Regular.ttf")
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

def seconds_to_srt_time(value):
    millis = max(0, int(round(float(value) * 1000)))
    hours, millis = divmod(millis, 3600000)
    minutes, millis = divmod(millis, 60000)
    seconds, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"

def dialogue_to_srt(segments):
    blocks = []
    for i, item in enumerate(segments, 1):
        blocks.append(
            f"{i}\n{seconds_to_srt_time(item['start'])} --> {seconds_to_srt_time(item['end'])}\n{item['khmer']}"
        )
    return "\n\n".join(blocks)

def normalize_dialogue(raw):
    data = parse_json_response(raw)
    source = data.get("segments", []) if isinstance(data, dict) else []
    segments = []
    for item in source[:300]:
        try:
            start = max(0.0, float(item["start"]))
            end = max(start + 0.25, float(item["end"]))
            text = str(item.get("khmer", item.get("translation", ""))).strip()
            speaker = str(item.get("speaker_id", item.get("speaker", "speaker_1"))).strip() or "speaker_1"
            gender = str(item.get("voice_gender", item.get("gender", "male"))).lower()
            gender = "female" if gender in ("female", "woman", "ស្រី") else "male"
            if text:
                segments.append({"start": start, "end": end, "khmer": text[:500],
                                 "speaker_id": speaker[:40], "voice_gender": gender})
        except (KeyError, TypeError, ValueError):
            continue
    segments.sort(key=lambda x: x["start"])
    if not segments:
        raise RuntimeError("AI មិនបានសម្គាល់សន្ទនាតួអង្គសម្រាប់បង្កើតសំឡេងខ្មែរទេ។")
    return segments


def kiri_request(path, payload=None, timeout=180):
    api_key = os.environ.get("KIRI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("សូមកំណត់ KIRI_API_KEY ក្នុង Render Environment ជាមុនសិន។")
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"https://api.kiritts.com/v1{path}", data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1200]
        if exc.code == 401:
            raise RuntimeError("Kiri API Key មិនត្រឹមត្រូវ។ សូមពិនិត្យ KIRI_API_KEY ក្នុង Render។") from exc
        if exc.code == 403:
            raise RuntimeError("គណនី Kiri មិនទាន់មានសិទ្ធិ API ឬសំឡេងក្លូននេះមិនមែនជារបស់គណនីនេះទេ។") from exc
        if exc.code == 429:
            raise RuntimeError("Kiri បានដល់កម្រិតប្រើប្រាស់ ឬកំពុងកំណត់ល្បឿន។ សូមពិនិត្យ Credits/Plan របស់ Kiri។") from exc
        raise RuntimeError(f"Kiri TTS API បរាជ័យ ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("មិនអាចភ្ជាប់ទៅ Kiri TTS បានទេ។ សូមពិនិត្យអ៊ីនធឺណិតរបស់ Render។") from exc

def available_kiri_voices():
    combined = []
    for endpoint in ("/voices", "/audio/voice-clones"):
        try:
            raw, _ = kiri_request(endpoint, timeout=45)
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("voices") or data.get("data") or data.get("items") or []
                if isinstance(items, dict):
                    items = items.get("data") or items.get("voices") or []
            else:
                items = []
            if isinstance(items, list):
                combined.extend(items)
        except Exception as exc:
            app.logger.warning("Kiri voice list %s unavailable: %s", endpoint, exc)
    cleaned, seen = [], set()
    for item in combined:
        if isinstance(item, str):
            name, gender, cloned = item, "", False
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("voice") or item.get("voice_id") or item.get("id") or "").strip()
            gender = str(item.get("gender") or item.get("voice_gender") or "").lower()
            cloned = bool(item.get("cloned") or item.get("is_cloned"))
        else:
            continue
        if name and name not in seen:
            seen.add(name)
            cleaned.append({"name": name, "gender": gender, "cloned": cloned})
    return cleaned

def synthesize_character_audio(job_id, segments, video_duration, male_voice='Chanda', female_voice='Maly'):
    """Create natural Khmer neural speech, assigning one consistent voice per speaker."""
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("Render មិនទាន់មាន FFmpeg ទេ។")
    voices = {"male": male_voice or "Chanda", "female": female_voice or "Maly"}
    speaker_voices = {}
    clips = []
    for index, segment in enumerate(segments):
        gender = segment["voice_gender"]
        speaker = segment["speaker_id"]
        speaker_voices.setdefault(speaker, voices[gender])
        voice = speaker_voices[speaker]
        clip = OUTPUT_DIR / f"{job_id}-voice-{index}.mp3"
        try:
            audio_bytes, content_type = kiri_request("/audio/speech", {
                "model": "kiritts", "input": segment["khmer"], "voice": voice,
                "response_format": "mp3", "speed": 1.0,
                "instructions": "Speak natural Cambodian Khmer with clear pronunciation and conversational emotion."
            }, timeout=240)
            if not audio_bytes or ("json" in content_type and audio_bytes[:1] in (b"{", b"[")):
                raise RuntimeError(audio_bytes[:1000].decode("utf-8", errors="replace"))
            clip.write_bytes(audio_bytes)
        except Exception as exc:
            raise RuntimeError(f"Kiri មិនអាចបង្កើតសំឡេង {voice} បានទេ៖ {exc}") from exc
        if clip.exists() and clip.stat().st_size > 0:
            clips.append((clip, segment["start"]))
    if not clips:
        raise RuntimeError("មិនអាចបង្កើតសំឡេងតួអង្គបានទេ។")
    command = [ffmpeg, "-y"]
    for clip, _ in clips:
        command += ["-i", str(clip)]
    filters = []
    labels = []
    for index, (_, start) in enumerate(clips):
        delay = max(0, int(start * 1000))
        filters.append(f"[{index}:a]adelay={delay}|{delay}[v{index}]")
        labels.append(f"[v{index}]")
    filters.append("".join(labels) + f"amix=inputs={len(clips)}:duration=longest:normalize=0,alimiter=limit=0.95[aout]")
    output = OUTPUT_DIR / f"{job_id}.mp3"
    command += ["-filter_complex", ";".join(filters), "-map", "[aout]",
                "-t", f"{video_duration:.3f}", "-c:a", "libmp3lame", "-b:a", "160k", str(output)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=max(300, int(video_duration * 3)))
        if result.returncode != 0:
            app.logger.error("Voice mix failed: %s", result.stderr[-4000:])
            raise RuntimeError("មិនអាចបញ្ចូលសំឡេងតួអង្គបានទេ។")
    finally:
        for clip, _ in clips:
            try:
                clip.unlink()
            except OSError:
                pass
    return output

def parse_json_response(raw):
    raw = (raw or "").strip()
    raw = raw.replace("\x60\x60\x60json", "").replace("\x60\x60\x60", "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.S)
        if not match:
            raise RuntimeError("AI មិនបានផ្តល់ទិន្នន័យ JSON ត្រឹមត្រូវទេ។")
        return json.loads(match.group(0))

def detect_chinese_screen_text(client, uploaded, on_retry):
    prompt = """
Inspect the entire video for visible, embedded Chinese text/subtitles that should be replaced.
Return ONLY JSON: {"items":[{"start":0.0,"end":2.5,"box":[x,y,width,height],"khmer":"Khmer translation"}]}.
Coordinates are normalized integers from 0 to 1000 relative to the full video frame (top-left origin).
Each item must cover one visible Chinese text line or compact text block. Use the tightest practical
rectangle around the Chinese characters, including a small margin. Give the time interval during which
that exact text remains visible. Merge consecutive intervals when the same text stays in the same place.
Translate visible Chinese faithfully into natural concise Khmer. Ignore logos, scenery, non-Chinese text,
and do not invent text. If there is no readable Chinese text, return {"items":[]}. No markdown.
"""
    response = generate_with_retry(
        client, model=GEMINI_MODEL, contents=[uploaded, prompt],
        on_retry=lambda a, n, w: on_retry(a, n, w, "កំពុងពិនិត្យអក្សរចិនលើវីដេអូ...")
    )
    data = parse_json_response(response.text)
    items = data.get("items", []) if isinstance(data, dict) else []
    clean = []
    for item in items[:100]:
        try:
            start = max(0.0, float(item["start"]))
            end = max(start + 0.25, float(item["end"]))
            box = item["box"]
            x, y, w, h = [int(max(0, min(1000, float(v)))) for v in box]
            khmer = str(item.get("khmer", "")).strip()
            if not khmer or w < 8 or h < 8:
                continue
            clean.append({"start": start, "end": end, "box": [x, y, w, h], "khmer": khmer[:240]})
        except (KeyError, TypeError, ValueError):
            continue
    return clean

def wrap_khmer(text, font_size, max_width):
    font = ImageFont.truetype(KHMER_FONT, font_size)
    lines, current = [], ""
    for char in text:
        candidate = current + char
        if current and font.getlength(candidate) > max_width:
            lines.append(current)
            current = char
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\n".join(lines)

def ffmpeg_escape_path(path):
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")

def render_translated_video(job_id, video_path, audio_path, ocr_items):
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("Render មិនទាន់មាន FFmpeg ទេ។ សូមពិនិត្យ Build Environment។")
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,duration", "-of", "json", str(video_path)],
        capture_output=True, text=True, check=True
    )
    streams = json.loads(probe.stdout).get("streams", [])
    if not streams:
        raise RuntimeError("មិនអាចអានទំហំវីដេអូបានទេ។")
    width, height = int(streams[0]["width"]), int(streams[0]["height"])
    duration = float(streams[0].get("duration") or 0)
    if duration <= 0:
        p = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(video_path)], capture_output=True, text=True, check=True)
        duration = float(p.stdout.strip())
    filters = []
    text_files = []
    for index, item in enumerate(ocr_items):
        x, y, bw, bh = item["box"]
        px = max(0, min(width - 2, round(x * width / 1000)))
        py = max(0, min(height - 2, round(y * height / 1000)))
        pw = max(2, min(width - px, round(bw * width / 1000)))
        ph = max(2, min(height - py, round(bh * height / 1000)))
        fontsize = max(16, min(52, int(ph * 0.55)))
        try:
            wrapped = wrap_khmer(item["khmer"], fontsize, max(40, pw - 16))
        except Exception:
            wrapped = item["khmer"]
        text_path = OUTPUT_DIR / f"{job_id}-sub-{index}.txt"
        text_path.write_text(wrapped, encoding="utf-8")
        text_files.append(text_path)
        start = max(0.0, min(duration, item["start"]))
        end = max(start + 0.1, min(duration, item["end"]))
        enable = f"between(t,{start:.3f},{end:.3f})"
        filters.append(
            f"drawbox=x={px}:y={py}:w={pw}:h={ph}:color=black@1:t=fill:enable='{enable}'"
        )
        font_arg = f":fontfile='{ffmpeg_escape_path(KHMER_FONT)}'" if Path(KHMER_FONT).exists() else ""
        filters.append(
            f"drawtext=textfile='{ffmpeg_escape_path(text_path)}'{font_arg}:"
            f"fontcolor=white:fontsize={fontsize}:line_spacing=2:"
            f"x={px + 6}:y={py + 4}:fix_bounds=1:enable='{enable}'"
        )
    output_path = OUTPUT_DIR / f"{job_id}.mp4"
    video_chain = ",".join(filters) if filters else "null"
    filter_complex = f"[0:v:0]{video_chain}[v];[1:a:0]apad[a]"
    command = [
        ffmpeg, "-y", "-i", str(video_path), "-i", str(audio_path),
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "[a]", "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(output_path)
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=max(900, int(duration * 5)))
        if result.returncode != 0:
            app.logger.error("FFmpeg failed: %s", result.stderr[-5000:])
            raise RuntimeError("មិនអាចបង្កើត MP4 បានទេ។ សូមពិនិត្យអក្សរ ឬទម្រង់វីដេអូ។")
    finally:
        for path in text_files:
            try:
                path.unlink()
            except OSError:
                pass
    return output_path, len(ocr_items)

def process_video(job_id, video_path, api_key, source_language, male_voice, female_voice):
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
            time.sleep(2)
        else:
            raise TimeoutError("ការរៀបចំវីដេអូចំណាយពេលយូរពេក។ សូមសាកល្បងវីដេអូខ្លីជាងនេះ។")

        def retry_message(a, n, wait, prefix):
            update_job(job_id, message=f"{prefix} AI កំពុងមមាញឹក សាកល្បងម្ដងទៀត ({a}/{n})...")
        source = LANGUAGES.get(source_language, LANGUAGES["auto"])
        update_job(job_id, progress=36, message="កំពុងស្តាប់ និងបកប្រែសំឡេងទៅជាអក្សរខ្មែរ...")
        prompt = (
            f"Listen carefully to all spoken dialogue in this video. The source language is {source}. "
            "Perform speaker diarization: assign a stable speaker_id to each distinct audible person "
            "and keep that ID consistent throughout the video. For each spoken turn, identify the "
            "speaker's perceived voice gender from the AUDIO only (female or male); do not guess from "
            "appearance. Translate every turn into natural, conversational Cambodian Khmer, preserving "
            "meaning, emotion, and character intent. Keep the original timing accurate. "
            "Return ONLY JSON in this exact shape: {\"segments\":[{\"start\":0.5,\"end\":3.8,"
            "\"speaker_id\":\"speaker_1\",\"voice_gender\":\"female\","
            "\"khmer\":\"អត្ថបទខ្មែរ\"}]}. "
            "Use seconds as numeric timestamps. One item per spoken turn. No markdown or commentary."
        )
        response = generate_with_retry(
            client, model=GEMINI_MODEL, contents=[uploaded, prompt],
            on_retry=lambda a, n, w: retry_message(a, n, w, "កំពុងបកប្រែ និងសម្គាល់តួអង្គ៖")
        )
        dialogue = normalize_dialogue(response.text)
        srt = dialogue_to_srt(dialogue)
        transcript = " ".join(item["khmer"] for item in dialogue)

        update_job(job_id, progress=55, message="កំពុងស្វែងរកអក្សរចិនដែលបង្ហាញលើផ្ទៃវីដេអូ...")
        ocr_items = detect_chinese_screen_text(client, uploaded, retry_message)
        update_job(job_id, progress=70, message=f"រកឃើញអក្សរចិន {len(ocr_items)} កន្លែង។ កំពុងបង្កើតសំឡេងស្រី/ប្រុសខ្មែរ...")
        probe = subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error",
                                "-show_entries", "format=duration", "-of", "default=nw=1:nk=1",
                                video_path], capture_output=True, text=True, check=True)
        video_duration = float(probe.stdout.strip())
        audio_path = synthesize_character_audio(job_id, dialogue, video_duration, male_voice, female_voice)

        update_job(job_id, progress=82, message="កំពុងបិទសំឡេងចិនដើម បិទអក្សរចិន ដាក់អក្សរខ្មែរ និងបញ្ចូលសំឡេងតួអង្គ...")
        video_out, replaced_count = render_translated_video(job_id, video_path, audio_path, ocr_items)
        update_job(job_id, progress=100, status="completed",
                   message=f"បកប្រែរួចរាល់! បានបង្កើត MP4 ថ្មី និងជំនួសអក្សរចិន {replaced_count} កន្លែង។",
                   translation_result=srt, transcript=transcript,
                   audio_url=f"/api/jobs/{job_id}/audio",
                   video_url=f"/api/jobs/{job_id}/video",
                   replaced_text_count=replaced_count,
                   speaker_count=len(set(item["speaker_id"] for item in dialogue)),
                   voice_style="Khmer neural female/male voices", original_audio_removed=True)
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

@app.get("/api/kiri/status")
def kiri_status():
    try:
        voices = available_kiri_voices()
        if not voices:
            return jsonify({"connected": False, "error": "Kiri API មិនបានផ្តល់បញ្ជីសំឡេងទេ។ សូមពិនិត្យ API Key និងសិទ្ធិ API។"}), 502
        return jsonify({"connected": True, "voice_count": len(voices),
                        "cloned_voice_count": sum(1 for voice in voices if voice.get("cloned")),
                        "voices": voices})
    except Exception as exc:
        return jsonify({"connected": False, "error": str(exc)}), 502

@app.get("/api/voices")
def api_voices():
    try:
        voices = available_kiri_voices()
        if not voices:
            return jsonify({"error": "មិនអាចទាញយកបញ្ជីសំឡេងពី Kiri បានទេ។ សូមពិនិត្យ KIRI_API_KEY និងសិទ្ធិ API។"}), 502
        return jsonify({"voices": voices})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

@app.get("/health")
def health():
    return jsonify({"status": "online", "app": "CN-KH AI", "model": GEMINI_MODEL, "video_export": "enabled", "time": datetime.now(timezone.utc).isoformat()})

@app.post("/api/jobs")
def create_job():
    video = request.files.get("video")
    api_key = (request.form.get("api_key") or "").strip()
    source_language = request.form.get("source_language", "auto")
    male_voice = request.form.get("male_voice", "Chanda").strip() or "Chanda"
    female_voice = request.form.get("female_voice", "Maly").strip() or "Maly"
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
    thread = threading.Thread(target=process_video, args=(job_id, path, api_key, source_language, male_voice, female_voice), daemon=True)
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

@app.get("/api/jobs/<job_id>/video")
def job_video(job_id):
    path = OUTPUT_DIR / f"{job_id}.mp4"
    if not path.exists():
        return jsonify({"error": "វីដេអូ MP4 មិនទាន់រួចរាល់ ឬផុតកំណត់។"}), 404
    return send_file(path, mimetype="video/mp4", as_attachment=True, download_name="CN-KH-AI-Khmer.mp4")

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
    return jsonify({"error": "វីដេអូធំពេក។ អតិបរមា 500 MB។"}), 413

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
