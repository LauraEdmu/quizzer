"""Multi-quiz Flask application with per-browser, SQLite-backed progress."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import secrets
import sqlite3
import unicodedata
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator

from flask import (
    Flask,
    abort,
    make_response,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from waitress import serve

BASE_DIR = Path(__file__).resolve().parent
QUIZ_DIR = BASE_DIR / "quizzes"
AUDIO_DIR = QUIZ_DIR / "audio"
VIDEO_DIR = QUIZ_DIR / "video"
IMAGE_DIR = QUIZ_DIR / "image"
DATA_DIR = BASE_DIR / "data"
DB_FILE = DATA_DIR / "progress.sqlite3"
COOKIE_NAME = "quiz_id"
COOKIE_AGE = 60 * 60 * 24 * 365  # One year
QUIZ_SLUG_PATTERN = re.compile(r"[a-z0-9_-]{1,64}\Z")
NUMBER_PATTERN = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\Z")
AUDIO_FORMATS = {
    "audio",  # Backwards-compatible alias for "audio regex".
    "audio simple",
    "audio regex",
    "audio number",
    "audio multiple choice",
}
VIDEO_FORMATS = {
    "video simple",
    "video regex",
    "video number",
    "video multiple choice",
}
IMAGE_FORMATS = {
    "image simple",
    "image regex",
    "image number",
    "image multiple choice",
}
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".jfif", ".png", ".apng", ".gif",
    ".webp", ".avif", ".bmp", ".svg", ".ico",
}
MEDIA_FORMATS = AUDIO_FORMATS | VIDEO_FORMATS | IMAGE_FORMATS
DISPLAY_MEDIA_FIELDS = {
    "display audio": "audio",
    "display video": "video",
    "display image": "image",
}

app = Flask(__name__)
app.config.update(
    MAX_CONTENT_LENGTH=4096,
    # This app is commonly updated in-place while Waitress is running. Flask
    # normally caches Jinja templates in production, which can leave an older
    # quiz.html rendering against newer Python question logic. Recheck template
    # mtimes so frontend and backend answer types stay in sync.
    TEMPLATES_AUTO_RELOAD=True,
)


class QuizLoadError(ValueError):
    """Invalid or missing admin-supplied quiz file."""


def normalise(value: str) -> str:
    """Unicode-normalise, collapse whitespace, and compare without case."""
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def quiz_slug(value: str) -> str | None:
    """Allow only safe quiz filenames; spaces in names become hyphens."""
    name = "-".join(unicodedata.normalize("NFKC", value).strip().casefold().split())
    return name if QUIZ_SLUG_PATTERN.fullmatch(name) else None


def parse_number(value: str) -> Decimal | None:
    """Keep only ASCII digits and decimal points before parsing a number.

    Commas, currency signs, spaces, letters, signs, and other characters are
    discarded. For example, £1,290.50 becomes 1290.50.
    """
    value = "".join(
        char
        for char in unicodedata.normalize("NFKC", value)
        if char.isascii() and (char.isdigit() or char == ".")
    )
    if not NUMBER_PATTERN.fullmatch(value):
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def number_range(value: str) -> tuple[Decimal, Decimal] | None:
    """Read inclusive bounds from 'minimum|maximum'."""
    parts = value.split("|")
    if len(parts) != 2:
        return None
    low, high = (parse_number(part) for part in parts)
    if low is None or high is None or low > high:
        return None
    return low, high


def is_audio_format(value: str) -> bool:
    """Whether a normalised format includes an audio clip."""
    return value in AUDIO_FORMATS


def is_video_format(value: str) -> bool:
    """Whether a normalised format includes an MP4 video clip."""
    return value in VIDEO_FORMATS


def is_image_format(value: str) -> bool:
    """Whether a normalised format includes an image."""
    return value in IMAGE_FORMATS


def is_media_format(value: str) -> bool:
    """Whether a normalised format includes audio, video, or image media."""
    return value in MEDIA_FORMATS


def answer_format(value: str) -> str:
    """Return the underlying answer type for a normalised question format."""
    if value == "audio":
        return "regex"
    if value.startswith("audio "):
        return value.removeprefix("audio ")
    if value.startswith("video "):
        return value.removeprefix("video ")
    if value.startswith("image "):
        return value.removeprefix("image ")
    return value


def media_question_parts(value: str) -> tuple[str, str] | None:
    """Read a media question written as 'question text|filename'.

    The final pipe is used as the separator so the visible prompt may itself
    contain a pipe. Media filenames must refer directly to their media folder
    and cannot contain path separators.
    """
    prompt, separator, filename = value.rpartition("|")
    prompt = prompt.strip()
    filename = filename.strip()
    if not separator or not prompt or not filename:
        return None
    if filename in {".", ".."} or "/" in filename or "\\" in filename or "\x00" in filename:
        return None
    if Path(filename).name != filename:
        return None
    return prompt, filename


def audio_question_parts(value: str) -> tuple[str, str] | None:
    """Backwards-compatible wrapper for parsing audio question text."""
    return media_question_parts(value)


def media_directory(kind: str) -> Path:
    """Return the on-disk directory for a supported media kind."""
    return {
        "audio": AUDIO_DIR,
        "video": VIDEO_DIR,
        "image": IMAGE_DIR,
    }[kind]


def validate_media_filename(kind: str, filename: str) -> str | None:
    """Validate and normalise a direct filename for one media directory."""
    filename = filename.strip()
    if media_question_parts(f"x|{filename}") is None:
        return None
    if kind == "video" and Path(filename).suffix.casefold() != ".mp4":
        return None
    if kind == "image" and Path(filename).suffix.casefold() not in IMAGE_EXTENSIONS:
        return None
    return filename


def media_version_entry(kind: str, filename: str, role: str) -> dict[str, object]:
    """Describe a referenced media file for quiz-version invalidation."""
    path = media_directory(kind) / filename
    entry: dict[str, object] = {"role": role, "kind": kind, "filename": filename}
    try:
        stat = path.stat()
    except (FileNotFoundError, OSError):
        entry["exists"] = False
    else:
        entry.update(
            {
                "exists": path.is_file(),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    return entry


def media_kind(question: dict[str, str]) -> str | None:
    """Return 'audio', 'video', or 'image' for a media question."""
    fmt = question.get("format", "")
    if is_audio_format(fmt):
        return "audio"
    if is_video_format(fmt):
        return "video"
    if is_image_format(fmt):
        return "image"
    return None


def media_file_path(question: dict[str, str]) -> Path | None:
    """Return the configured media path for an audio/video question."""
    kind = media_kind(question)
    if kind is None:
        return None
    parts = media_question_parts(question.get("question", ""))
    if parts is None:
        return None
    return media_directory(kind) / parts[1]


def media_is_available(question: dict[str, str]) -> bool:
    """Whether a media question has its referenced file on disk."""
    path = media_file_path(question)
    return path is not None and path.is_file()


def audio_file_path(question: dict[str, str]) -> Path | None:
    """Backwards-compatible audio-only path helper."""
    return media_file_path(question) if is_audio_format(question.get("format", "")) else None


def audio_is_available(question: dict[str, str]) -> bool:
    """Backwards-compatible audio availability helper."""
    return is_audio_format(question.get("format", "")) and media_is_available(question)


def question_counts(question: dict[str, str]) -> bool:
    """Missing-media questions are shown as skipped but do not count."""
    return not is_media_format(question.get("format", "")) or media_is_available(question)

def load_quiz(name: str) -> tuple[list[dict[str, str]], dict[str, str], str]:
    """Load a quiz on demand; edits reset progress for that quiz only."""
    if not QUIZ_SLUG_PATTERN.fullmatch(name):
        raise QuizLoadError("Invalid quiz name.")

    file = QUIZ_DIR / f"{name}.json"
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise QuizLoadError(f"Quiz '{name}' was not found. Expected quizzes/{name}.json.") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QuizLoadError(f"Could not read quizzes/{name}.json: {exc}") from exc

    if not isinstance(data, list) or not data:
        raise QuizLoadError("The quiz must contain a non-empty JSON array.")

    settings: dict[str, str] = {}
    questions = data
    first = data[0]
    if isinstance(first, dict) and "question" not in first:
        settings = dict(first)
        questions = data[1:]
        if not questions:
            raise QuizLoadError("A quiz settings entry must be followed by at least one question.")

        colour = settings.get("colour")
        if colour is not None:
            if not isinstance(colour, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", colour.strip()):
                raise QuizLoadError(
                    "Quiz setting 'colour' must be a six-digit hex colour such as #F5A9B8."
                )
            settings["colour"] = colour.strip().upper()

    media_version_data: list[dict[str, object]] = []

    for number, item in enumerate(questions, start=1):
        if not isinstance(item, dict):
            raise QuizLoadError(f"Question {number} must be a JSON object.")
        for field in ("question", "format", "answer", "display_answer"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise QuizLoadError(f"Question {number} requires a non-empty '{field}' string.")

        if "example" in item:
            if not isinstance(item["example"], str):
                raise QuizLoadError(f"Question {number}: 'example' must be a string when provided.")
            item["example"] = item["example"].strip()

        item["format"] = " ".join(
            item["format"].strip().lower().replace("_", " ").replace("-", " ").split()
        )
        supported_formats = {"simple", "multiple choice", "regex", "number"} | MEDIA_FORMATS
        if item["format"] not in supported_formats:
            raise QuizLoadError(f"Question {number}: unsupported format {item['format']!r}.")

        kind = answer_format(item["format"])

        if kind == "multiple choice":
            choices = [part.strip() for part in item["answer"].split("|")]
            if len(choices) < 2 or any(not part for part in choices):
                raise QuizLoadError(f"Question {number}: separate two or more choices with |.")
            if len({normalise(choice) for choice in choices}) != len(choices):
                raise QuizLoadError(f"Question {number}: duplicate multiple-choice options.")

        elif kind == "regex":
            try:
                re.compile(item["answer"], re.IGNORECASE)
            except re.error as exc:
                raise QuizLoadError(f"Question {number}: invalid regex: {exc}.") from exc

        elif kind == "number":
            if number_range(item["answer"]) is None:
                raise QuizLoadError(
                    f"Question {number}: number answer must be minimum|maximum "
                    "with valid numbers and minimum <= maximum."
                )

        if is_media_format(item["format"]):
            parts = media_question_parts(item["question"])
            kind_name = media_kind(item)
            if parts is None or kind_name is None:
                if is_audio_format(item["format"]):
                    folder, example = "audio", "audiofile.mp3"
                elif is_video_format(item["format"]):
                    folder, example = "video", "videofile.mp4"
                else:
                    folder, example = "image", "imagefile.png"
                raise QuizLoadError(
                    f"Question {number}: {folder} question must be written as "
                    f"'question text|{example}', using a filename directly inside quizzes/{folder}."
                )
            _, filename = parts
            if kind_name == "video" and Path(filename).suffix.casefold() != ".mp4":
                raise QuizLoadError(
                    f"Question {number}: video files must use the .mp4 extension."
                )
            if kind_name == "image" and Path(filename).suffix.casefold() not in IMAGE_EXTENSIONS:
                raise QuizLoadError(
                    f"Question {number}: unsupported image extension. "
                    "Use JPG/JPEG/JFIF, PNG/APNG, GIF, WebP, AVIF, BMP, SVG, or ICO."
                )
            media_version_data.append(
                media_version_entry(kind_name, filename, role="question")
            )

        for field, display_kind in DISPLAY_MEDIA_FIELDS.items():
            value = item.get(field)
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                raise QuizLoadError(
                    f"Question {number}: '{field}' must be a non-empty filename string when provided."
                )

            filename = validate_media_filename(display_kind, value)
            if filename is None:
                if display_kind == "video":
                    requirement = "an .mp4 filename directly inside quizzes/video"
                elif display_kind == "image":
                    requirement = (
                        "a supported image filename directly inside quizzes/image "
                        "(JPG/JPEG/JFIF, PNG/APNG, GIF, WebP, AVIF, BMP, SVG, or ICO)"
                    )
                else:
                    requirement = "a filename directly inside quizzes/audio"
                raise QuizLoadError(
                    f"Question {number}: '{field}' must be {requirement}."
                )

            item[field] = filename
            media_version_data.append(
                media_version_entry(display_kind, filename, role=field)
            )

    # Include media file presence/metadata in the version. This prevents a score
    # obtained with one question count from being reused if an audio/video/image
    # file is later added, removed, or replaced.
    version_payload = {"settings": settings, "questions": questions, "media": media_version_data}
    version = hashlib.sha256(
        json.dumps(version_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return questions, settings, version


@contextmanager
def db_connection() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DB_FILE, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


CREATE_ATTEMPTS = """
CREATE TABLE IF NOT EXISTS attempts (
    id TEXT NOT NULL,
    quiz_name TEXT NOT NULL,
    version TEXT NOT NULL,
    csrf TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    score INTEGER NOT NULL DEFAULT 0,
    answered INTEGER NOT NULL DEFAULT 0,
    last_correct INTEGER NOT NULL DEFAULT 0,
    last_response TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (id, quiz_name)
)
"""


def initialise_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    with db_connection() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(attempts)").fetchall()
        }
        if columns and "quiz_name" not in columns:
            # Preserve existing single-quiz installations as the "general" quiz.
            connection.execute("ALTER TABLE attempts RENAME TO old_attempts")
            connection.execute(CREATE_ATTEMPTS)
            connection.execute(
                """INSERT INTO attempts
                   (id, quiz_name, version, csrf, position, score, answered,
                    last_correct, last_response)
                   SELECT id, 'general', version, csrf, position, score, answered,
                          last_correct, last_response
                   FROM old_attempts"""
            )
            connection.execute("DROP TABLE old_attempts")
        else:
            connection.execute(CREATE_ATTEMPTS)
        connection.execute("""CREATE TABLE IF NOT EXISTS visitor_tokens (
            id TEXT PRIMARY KEY,
            csrf TEXT NOT NULL
        )""")


def check_answer(question: dict[str, str], submitted: str) -> bool:
    kind = answer_format(question["format"])
    if kind == "number":
        value = parse_number(submitted)
        bounds = number_range(question["answer"])
        return value is not None and bounds is not None and bounds[0] <= value <= bounds[1]
    if kind == "regex":
        # Only trusted quiz authors should provide regex patterns.
        value = " ".join(unicodedata.normalize("NFKC", submitted).split())
        return re.fullmatch(question["answer"], value, re.IGNORECASE) is not None
    correct = (
        question["answer"].split("|", maxsplit=1)[0]
        if kind == "multiple choice"
        else question["answer"]
    )
    return normalise(submitted) == normalise(correct)


def get_visitor() -> tuple[str, bool]:
    visitor_id = request.cookies.get(COOKIE_NAME, "")
    if re.fullmatch(r"[A-Za-z0-9_-]{43}", visitor_id):
        return visitor_id, False
    return secrets.token_urlsafe(32), True


def with_cookie(response, visitor_id: str, is_new: bool):
    if is_new:
        response.set_cookie(
            COOKIE_NAME,
            visitor_id,
            max_age=COOKIE_AGE,
            httponly=True,
            secure=os.getenv("COOKIE_SECURE", "0") == "1",
            samesite="Lax",
        )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/quiz-audio/<filename>")
def quiz_audio(filename: str):
    """Serve audio files from quizzes/audio without exposing other paths."""
    if media_question_parts(f"x|{filename}") is None:
        abort(404)
    return send_from_directory(AUDIO_DIR, filename, conditional=True, max_age=3600)


@app.route("/quiz-video/<filename>")
def quiz_video(filename: str):
    """Serve MP4 video files from quizzes/video without exposing other paths."""
    if media_question_parts(f"x|{filename}") is None or Path(filename).suffix.casefold() != ".mp4":
        abort(404)
    return send_from_directory(VIDEO_DIR, filename, conditional=True, max_age=3600)


@app.route("/quiz-image/<filename>")
def quiz_image(filename: str):
    """Serve supported image files from quizzes/image without exposing other paths."""
    if (
        media_question_parts(f"x|{filename}") is None
        or Path(filename).suffix.casefold() not in IMAGE_EXTENSIONS
    ):
        abort(404)
    return send_from_directory(IMAGE_DIR, filename, conditional=True, max_age=3600)


@app.route("/", methods=["GET", "POST"])
def home():
    visitor_id, is_new = get_visitor()
    error = None
    typed_name = ""

    with db_connection() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO visitor_tokens (id, csrf) VALUES (?, ?)",
            (visitor_id, secrets.token_urlsafe(32)),
        )
        csrf = connection.execute(
            "SELECT csrf FROM visitor_tokens WHERE id = ?", (visitor_id,)
        ).fetchone()["csrf"]

        if request.method == "POST":
            action = request.form.get("action", "start")
            if action in ("reset_one", "reset_all"):
                if not secrets.compare_digest(request.form.get("csrf", ""), csrf):
                    error = "This form has expired. Please refresh and try again."
                elif action == "reset_all":
                    connection.execute("DELETE FROM attempts WHERE id = ?", (visitor_id,))
                    return with_cookie(redirect(url_for("home"), code=303), visitor_id, is_new)
                else:
                    name = request.form.get("quiz_name", "")
                    if QUIZ_SLUG_PATTERN.fullmatch(name):
                        connection.execute(
                            "DELETE FROM attempts WHERE id = ? AND quiz_name = ?",
                            (visitor_id, name),
                        )
                        return with_cookie(redirect(url_for("home"), code=303), visitor_id, is_new)
                    error = "Invalid quiz name."
            else:
                typed_name = request.form.get("quiz_name", "")
                name = quiz_slug(typed_name)
                if not name:
                    error = "Use 1–64 letters, numbers, spaces, hyphens, or underscores."
                else:
                    try:
                        load_quiz(name)
                    except QuizLoadError as exc:
                        error = str(exc)
                    else:
                        return with_cookie(
                            redirect(url_for("quiz_page", name=name), code=303),
                            visitor_id, is_new,
                        )

        previous = connection.execute(
            "SELECT quiz_name, position, score FROM attempts WHERE id = ? ORDER BY quiz_name",
            (visitor_id,),
        ).fetchall()

    response = make_response(render_template(
        "home.html", error=error, typed_name=typed_name, previous=previous,
        csrf=csrf,
    ))
    return with_cookie(response, visitor_id, is_new)


@app.route("/quiz/<name>", methods=["GET", "POST"])
def quiz_page(name: str):
    slug = quiz_slug(name)
    if slug is None:
        abort(404)
    if slug != name:
        return redirect(url_for("quiz_page", name=slug), code=303)

    try:
        questions, settings, version = load_quiz(name)
    except QuizLoadError as exc:
        response = make_response(render_template(
            "home.html", error=str(exc), typed_name=name, previous=[], csrf=""
        ), 404)
        response.headers["Cache-Control"] = "no-store"
        return response

    visitor_id, is_new = get_visitor()
    error = None
    with db_connection() as connection:
        connection.execute(
            """INSERT OR IGNORE INTO attempts (id, quiz_name, version, csrf)
               VALUES (?, ?, ?, ?)""",
            (visitor_id, name, version, secrets.token_urlsafe(32)),
        )
        state = connection.execute(
            "SELECT * FROM attempts WHERE id = ? AND quiz_name = ?", (visitor_id, name)
        ).fetchone()

        if state["version"] != version:
            connection.execute(
                """UPDATE attempts SET version = ?, position = 0, score = 0,
                   answered = 0, last_correct = 0, last_response = ''
                   WHERE id = ? AND quiz_name = ?""",
                (version, visitor_id, name),
            )
            state = connection.execute(
                "SELECT * FROM attempts WHERE id = ? AND quiz_name = ?", (visitor_id, name)
            ).fetchone()

        position = state["position"]
        current_question = questions[position] if position < len(questions) else None
        current_media_missing = bool(
            current_question
            and is_media_format(current_question["format"])
            and not media_is_available(current_question)
        )

        handled = False
        if request.method == "POST":
            csrf = request.form.get("csrf", "")
            if not secrets.compare_digest(csrf, state["csrf"]):
                error = "This form has expired. Please refresh and try again."
            else:
                action = request.form.get("action")
                if action == "reset":
                    connection.execute(
                        """UPDATE attempts SET position = 0, score = 0, answered = 0,
                           last_correct = 0, last_response = ''
                           WHERE id = ? AND quiz_name = ?""",
                        (visitor_id, name),
                    )
                    handled = True

                elif action == "next" and state["answered"]:
                    connection.execute(
                        """UPDATE attempts SET position = position + 1, answered = 0,
                           last_response = '' WHERE id = ? AND quiz_name = ?
                           AND position = ? AND answered = 1""",
                        (visitor_id, name, state["position"]),
                    )
                    handled = True

                elif action == "skip" and current_media_missing and not state["answered"]:
                    connection.execute(
                        """UPDATE attempts SET position = position + 1, answered = 0,
                           last_correct = 0, last_response = ''
                           WHERE id = ? AND quiz_name = ? AND position = ?""",
                        (visitor_id, name, state["position"]),
                    )
                    handled = True

                elif (
                    action == "submit"
                    and not state["answered"]
                    and current_question is not None
                    and not current_media_missing
                ):
                    submitted = request.form.get("answer", "")
                    if not submitted.strip() or len(submitted) > 300:
                        error = "Please enter an answer (up to 300 characters)."
                    else:
                        if answer_format(current_question["format"]) == "multiple choice":
                            allowed = [part.strip() for part in current_question["answer"].split("|")]
                            if submitted not in allowed:
                                error = "Please select one of the displayed answers."
                        if error is None:
                            right = check_answer(current_question, submitted)
                            connection.execute(
                                """UPDATE attempts SET score = score + ?, answered = 1,
                                   last_correct = ?, last_response = ?
                                   WHERE id = ? AND quiz_name = ? AND position = ? AND answered = 0""",
                                (
                                    int(right), int(right), " ".join(submitted.split()),
                                    visitor_id, name, state["position"],
                                ),
                            )
                            handled = True

        state = connection.execute(
            "SELECT * FROM attempts WHERE id = ? AND quiz_name = ?", (visitor_id, name)
        ).fetchone()

    if handled:
        return with_cookie(
            redirect(url_for("quiz_page", name=name), code=303), visitor_id, is_new
        )

    position = state["position"]
    finished = position >= len(questions)
    question = None if finished else questions[position]
    question_is_audio = bool(question and is_audio_format(question["format"]))
    question_is_video = bool(question and is_video_format(question["format"]))
    question_is_image = bool(question and is_image_format(question["format"]))
    question_is_media = question_is_audio or question_is_video or question_is_image
    question_answer_format = answer_format(question["format"]) if question else None
    media_missing = bool(
        question and question_is_media and not media_is_available(question)
    )

    options: list[str] = []
    if question and question_answer_format == "multiple choice" and not state["answered"]:
        options = [part.strip() for part in question["answer"].split("|")]
        seed = hashlib.sha256(f"{visitor_id}:{name}:{position}:{version}".encode()).digest()
        random.Random(seed).shuffle(options)

    question_prompt = question["question"] if question else ""
    media_filename = None
    media_url = None
    current_media_kind = media_kind(question) if question else None
    if question and question_is_media:
        parts = media_question_parts(question["question"])
        if parts is not None:
            question_prompt, media_filename = parts
            if not media_missing:
                endpoint = {
                    "audio": "quiz_audio",
                    "video": "quiz_video",
                    "image": "quiz_image",
                }[current_media_kind]
                media_url = url_for(endpoint, filename=media_filename, v=version)

    display_media: list[dict[str, object]] = []
    if question and state["answered"]:
        endpoint_by_kind = {
            "audio": "quiz_audio",
            "video": "quiz_video",
            "image": "quiz_image",
        }
        for field, display_kind in DISPLAY_MEDIA_FIELDS.items():
            filename = question.get(field)
            if not filename:
                continue
            path = media_directory(display_kind) / filename
            available = path.is_file()
            display_media.append(
                {
                    "kind": display_kind,
                    "filename": filename,
                    "available": available,
                    "url": (
                        url_for(endpoint_by_kind[display_kind], filename=filename, v=version)
                        if available
                        else None
                    ),
                }
            )

    total = sum(1 for item in questions if question_counts(item))
    completed_before = sum(1 for item in questions[:position] if question_counts(item))
    completed = total if finished else completed_before
    if question and not media_missing and state["answered"]:
        completed += 1
    current_number = completed_before + 1 if question and not media_missing else None
    progress_percent = (100 * completed / total) if total else 0

    response = make_response(render_template(
        "quiz.html",
        quiz_name=name,
        quiz_colour=settings.get("colour"),
        total=total,
        raw_total=len(questions),
        position=position,
        score=state["score"],
        answered=bool(state["answered"]),
        completed=completed,
        current_number=current_number,
        progress_percent=progress_percent,
        finished=finished,
        question=question,
        question_prompt=question_prompt,
        question_is_audio=question_is_audio,
        question_is_video=question_is_video,
        question_is_image=question_is_image,
        question_is_media=question_is_media,
        media_kind=current_media_kind,
        question_answer_format=question_answer_format,
        question_is_multiple_choice=(question_answer_format == "multiple choice"),
        question_is_number=(question_answer_format == "number"),
        options=options,
        media_missing=media_missing,
        media_filename=media_filename,
        media_url=media_url,
        display_media=display_media,
        last_correct=bool(state["last_correct"]),
        last_response=state["last_response"],
        csrf=state["csrf"],
        error=error,
    ))
    return with_cookie(response, visitor_id, is_new)


initialise_database()

if __name__ == "__main__":
    serve(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8080")), threads=8)
