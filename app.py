import os
import tempfile
import time
from flask import Flask, request, jsonify
from google import genai

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB

@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Gemini-API-Key"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

@app.route("/", methods=["GET"])
def health_check():
    return jsonify({
        "status": "online",
        "message": "Khmer AI Translator Backend is running."
    }), 200

@app.route("/translate-video", methods=["POST", "OPTIONS"])
def translate_video():
    if request.method == "OPTIONS":
        return ("", 204)

    api_key = request.headers.get("X-Gemini-API-Key") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return jsonify({
            "success": False,
            "error": "សូមបញ្ចូល Gemini API Key ក្នុងកម្មវិធីជាមុន។"
        }), 401

    temp_vid_path = None
    uploaded_file = None
    client = None

    try:
        video_file = request.files.get("video")
        if not video_file:
            return jsonify({
                "success": False,
                "error": "សូមជ្រើសរើសវីដេអូជាមុន។"
            }), 400

        source_language = request.form.get("source_language", "auto")
        client = genai.Client(api_key=api_key)

        suffix = os.path.splitext(video_file.filename or "")[1] or ".mp4"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_vid:
            video_file.save(temp_vid.name)
            temp_vid_path = temp_vid.name

        uploaded_file = client.files.upload(file=temp_vid_path)
        # Gemini may need time to process uploaded video before it can analyze it.
        for _ in range(60):
            uploaded_file = client.files.get(name=uploaded_file.name)
            state = str(getattr(uploaded_file, "state", "")).upper()
            if state.endswith("ACTIVE"):
                break
            if state.endswith("FAILED"):
                raise RuntimeError("Gemini could not process this video file.")
            time.sleep(2)
        else:
            raise TimeoutError("Video processing took too long. Please try a shorter video.")
        language_names = {
            "en": "English", "th": "Thai", "zh": "Chinese",
            "ko": "Korean", "ja": "Japanese", "vi": "Vietnamese",
            "fr": "French", "lo": "Lao", "id": "Indonesian",
            "auto": "the language spoken in the video"
        }
        source_name = language_names.get(source_language, "the language spoken in the video")
        prompt = (
            f"Listen carefully to the speech in this video. The spoken language is {source_name}. "
            "Transcribe the speech and translate it into natural, accurate Khmer. "
            "Return the result as valid SubRip SRT only: sequential subtitle numbers, "
            "timestamps in HH:MM:SS,mmm --> HH:MM:SS,mmm format, and Khmer subtitle text. "
            "Keep subtitle segments short and synchronize timestamps to the speech. "
            "Do not add explanations or markdown fences. If there is no speech, say so in Khmer."
        )

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[uploaded_file, prompt]
        )
        result_text = response.text if response and response.text else "មិនមានលទ្ធផលត្រឡប់មកវិញទេ។"

        return jsonify({
            "success": True,
            "translation_result": result_text
        }), 200

    except Exception:
        app.logger.exception("Video translation failed")
        return jsonify({
            "success": False,
            "error": "ដំណើរការមិនបានសម្រេចទេ។ សូមពិនិត្យ API Key អ៊ីនធឺណិត និងទំហំវីដេអូ រួចសាកល្បងម្ដងទៀត។"
        }), 500

    finally:
        if temp_vid_path and os.path.exists(temp_vid_path):
            try:
                os.unlink(temp_vid_path)
            except OSError:
                pass
        if uploaded_file and client:
            try:
                client.files.delete(name=uploaded_file.name)
            except Exception:
                app.logger.warning("Could not delete temporary Gemini upload")

@app.errorhandler(413)
def file_too_large(_error):
    return jsonify({
        "success": False,
        "error": "វីដេអូធំពេក។ សូមជ្រើសរើសវីដេអូតូចជាង 100 MB។"
    }), 413

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
