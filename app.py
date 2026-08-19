import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from flask import Flask, render_template, request
from google import genai
from google.genai import errors as genai_errors
from google.genai import types


BASE_DIR = Path(__file__).resolve().parent
PROMPT_PATH = BASE_DIR / "Prompt.en.md"


class ProcessingError(RuntimeError):
    """An error that can be safely shown in the web interface."""


def load_local_environment() -> None:
    """Load simple KEY=VALUE entries from .env without adding a dependency."""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        if key:
            os.environ[key] = value


load_local_environment()
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
SCORE_WEIGHTS = {
    "densidad": 0.30,
    "especificidad": 0.25,
    "demanda_busqueda": 0.20,
    "autonomia": 0.15,
    "apertura": 0.10,
}


def extract_youtube_video_id(video_url: str) -> str:
    """Validate a YouTube URL and return its eleven-character video ID."""
    candidate = video_url.strip()
    if not candidate:
        raise ValueError("Enter a YouTube URL.")

    parsed = urlparse(candidate)
    hostname = (parsed.hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]

    video_id = ""
    if hostname == "youtu.be":
        video_id = parsed.path.strip("/").split("/", 1)[0]
    elif hostname in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        else:
            path_parts = [part for part in parsed.path.split("/") if part]
            if len(path_parts) >= 2 and path_parts[0] in {"shorts", "live", "embed"}:
                video_id = path_parts[1]

    if not VIDEO_ID_PATTERN.fullmatch(video_id):
        raise ValueError("Enter a valid YouTube video URL.")
    return video_id


def load_selector_prompt(video_url: str) -> str:
    """Read the prompt and unwrap its current JavaScript template literal."""
    if not PROMPT_PATH.exists():
        raise ProcessingError("Prompt.en.md could not be found.")

    prompt_source = PROMPT_PATH.read_text(encoding="utf-8-sig")
    first_backtick = prompt_source.find("`")
    last_backtick = prompt_source.rfind("`")
    if first_backtick != -1 and last_backtick > first_backtick:
        prompt_source = prompt_source[first_backtick + 1 : last_backtick]

    # The file is currently stored as an escaped JavaScript/Markdown prompt.
    prompt_source = re.sub(r"\\([_\[\]=\-])", r"\1", prompt_source)
    return (
        f"{prompt_source.strip()}\n\n"
        "Use the video's native audio directly. Do not translate the spoken "
        "content when analyzing it.\n"
        f"YouTube URL: {video_url}"
    )


def parse_json_response(response_text: str | None, context: str) -> dict[str, Any]:
    """Parse a model JSON response, tolerating an accidental Markdown fence."""
    if not response_text or not response_text.strip():
        raise ProcessingError(f"Gemini returned an empty {context} response.")

    text = response_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProcessingError(f"Gemini returned invalid JSON for {context}.") from exc

    if not isinstance(parsed, dict):
        raise ProcessingError(f"Gemini returned an invalid {context} object.")
    return parsed


def validate_selection(selection: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate highlight identity, ranges, and scoring data in model output."""
    highlights = selection.get("highlights")
    discarded = selection.get("descartados")
    if not isinstance(highlights, list) or not isinstance(discarded, list):
        raise ProcessingError(
            "Gemini's highlight response is missing highlights or descartados."
        )

    seen_ids: set[str] = set()
    for highlight in highlights:
        if not isinstance(highlight, dict):
            raise ProcessingError("Gemini returned an invalid highlight entry.")

        highlight_id = highlight.get("id")
        if not isinstance(highlight_id, str) or not highlight_id.strip():
            raise ProcessingError("A highlight is missing its ID.")
        if highlight_id in seen_ids:
            raise ProcessingError(f"Gemini returned duplicate highlight ID {highlight_id}.")
        seen_ids.add(highlight_id)

        start = highlight.get("inicio")
        end = highlight.get("fin")
        if (
            not isinstance(start, (int, float))
            or isinstance(start, bool)
            or not isinstance(end, (int, float))
            or isinstance(end, bool)
            or start < 0
            or end <= start
        ):
            raise ProcessingError(f"Highlight {highlight_id} has invalid timestamps.")

        scores = highlight.get("puntajes")
        if not isinstance(scores, dict):
            raise ProcessingError(f"Highlight {highlight_id} is missing its scores.")
        for score_name in SCORE_WEIGHTS:
            score = scores.get(score_name)
            if (
                not isinstance(score, (int, float))
                or isinstance(score, bool)
                or score < 0
                or score > 10
            ):
                raise ProcessingError(
                    f"Highlight {highlight_id} has an invalid {score_name} score."
                )

        highlight["duracion_seg"] = round(float(end) - float(start), 3)
        highlight["ponderado"] = round(
            sum(float(scores[name]) * weight for name, weight in SCORE_WEIGHTS.items()),
            2,
        )

    return highlights


def create_gemini_client() -> genai.Client:
    load_local_environment()
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ProcessingError("GEMINI_API_KEY is not configured.")
    return genai.Client(api_key=api_key)


def select_highlights(
    client: genai.Client, video_url: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=[
            types.Part.from_uri(file_uri=video_url, mime_type="video/*"),
            load_selector_prompt(video_url),
        ],
        config=types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type="application/json",
        ),
    )
    selection = parse_json_response(response.text, "highlight selection")
    highlights = validate_selection(selection)
    return selection, highlights


def transcribe_highlight(
    client: genai.Client, video_url: str, highlight: dict[str, Any]
) -> str:
    highlight_id = highlight["id"]
    start = highlight["inicio"]
    end = highlight["fin"]
    transcription_prompt = f"""
Listen directly to the native audio of the supplied YouTube video from exactly
{start} seconds through {end} seconds for highlight {highlight_id}.

Return a verbatim transcription of every intelligible spoken word in that range.
Preserve the original spoken language. Do not translate, summarize, paraphrase,
polish grammar, or invent inaudible words. Do not add commentary, timestamps, or
speaker labels. Return only the verbatim transcription as plain text, with no
JSON wrapper, Markdown fence, heading, preamble, or explanation.
""".strip()

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=[
            types.Part.from_uri(file_uri=video_url, mime_type="video/*"),
            transcription_prompt,
        ],
        config=types.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="text/plain",
        ),
    )
    text = response.text
    if not isinstance(text, str) or not text.strip():
        raise ProcessingError(f"Gemini returned no transcription for {highlight_id}.")
    return text.strip()


def process_video(video_url: str) -> dict[str, Any]:
    """Select highlights and add a verified native-audio transcription to each."""
    extract_youtube_video_id(video_url)
    client = create_gemini_client()
    try:
        selection, highlights = select_highlights(client, video_url)
    except genai_errors.ClientError as exc:
        message = str(exc).lower()
        if "api key not valid" in message or "api_key_invalid" in message:
            raise ProcessingError(
                "GEMINI_API_KEY was rejected by Google. Configure a valid key and try again."
            ) from exc
        raise ProcessingError("Gemini rejected the video analysis request.") from exc
    except genai_errors.ServerError as exc:
        raise ProcessingError("Gemini is temporarily unavailable. Try again shortly.") from exc

    for highlight in highlights:
        try:
            highlight["texto"] = transcribe_highlight(client, video_url, highlight)
            highlight["transcripcion_estado"] = "verificada"
        except ProcessingError as exc:
            highlight["texto"] = None
            highlight["transcripcion_estado"] = "no_verificada"
            highlight["error_transcripcion"] = str(exc)
        except Exception:
            logging.exception("Unexpected transcription error for %s", highlight["id"])
            highlight["texto"] = None
            highlight["transcripcion_estado"] = "no_verificada"
            highlight["error_transcripcion"] = (
                "Gemini could not transcribe this highlight."
            )

    return selection


app = Flask(__name__)


@app.route("/", methods=["GET", "POST"])
def index() -> tuple[str, int] | str:
    video_url = ""
    output = ""
    error = ""
    status = 200

    if request.method == "POST":
        video_url = request.form.get("youtube_url", "").strip()
        try:
            extract_youtube_video_id(video_url)
            result = process_video(video_url)
            output = json.dumps(result, ensure_ascii=False, indent=2)
        except ValueError as exc:
            error = str(exc)
            status = 400
        except ProcessingError as exc:
            error = str(exc)
            status = 502
        except Exception:
            app.logger.exception("Unexpected video processing error")
            error = "Gemini could not process this video. Check the URL and try again."
            status = 502

    rendered = render_template(
        "index.html",
        video_url=video_url,
        output=output,
        error=error,
        model_name=MODEL_NAME,
    )
    return (rendered, status) if status != 200 else rendered


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG") == "1")
