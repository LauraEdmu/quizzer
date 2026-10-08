# Flask Quiz

A multi-quiz Flask + Waitress web app with a pink background, dark/light toggle, audio/video/image questions, and cookie-identified, SQLite-backed progress. No accounts required.

**Installation**

```bash
python -m pip install -r requirements.txt
python app.py
```

Open **http://localhost:8080**. By default, Waitress listens on `0.0.0.0:8080`.

Optional environment variables:
- `HOST`: listening interface (default `0.0.0.0`)
- `PORT`: listening port (default `8080`)
- `COOKIE_SECURE=1`: set when serving behind HTTPS; leave unset on plain HTTP.

**Adding and using quizzes**

Place each quiz in `quizzes/<name>.json`, e.g. `quizzes/general.json` or `quizzes/minecraft.json`. Two sample quizzes, `general` and `technology`, are included.

When visitors open the site they are prompted to enter the quiz name (`general`, `minecraft`, etc.). The app loads that file, and remembers progress independently for each quiz. Names are case-insensitive; spaces turn into hyphens (entering `movie trivia` loads `quizzes/movie-trivia.json`). Only ASCII letters, numbers, spaces, hyphens and underscores are allowed. The `quizzes/` directory must be writable only by trusted administrators.

A quiz file is a non-empty JSON array. It may start with an optional **settings** object at index 0; settings are not treated as a question or included in the score. For example:

```json
[
  {
    "colour": "#FFFFFF"
  },
  {
    "question": "Which planet is known as the Red Planet?",
    "format": "multiple choice",
    "answer": "Mars|Venus|Jupiter|Mercury",
    "display_answer": "Mars"
  },
  {
    "question": "Name the capital of France.",
    "format": "simple",
    "answer": "Paris",
    "display_answer": "Paris",
    "timer": 15
  },
  {
    "question": "Write the British or American spelling of colour.",
    "format": "regex",
    "answer": "colou?r",
    "display_answer": "colour or color"
  },
  {
    "question": "Approximately how many kilometres long is a marathon?",
    "format": "number",
    "answer": "42|43",
    "display_answer": "42.195 km (accepted range: 42–43 km)"
  },
  {
    "question": "Name the song in this clip|song-clip.mp3",
    "format": "audio regex",
    "answer": "(?:example song|the example song)",
    "display_answer": "Example Song"
  },
  {
    "question": "Who is speaking in this clip?|voice.mp3",
    "format": "audio simple",
    "answer": "Laura",
    "display_answer": "Laura"
  },
  {
    "question": "Roughly how many BPM is this clip?|tempo.mp3",
    "format": "audio number",
    "answer": "118|122",
    "display_answer": "120 BPM (accepted range: 118–122)"
  },
  {
    "question": "Which instrument is playing?|instrument.mp3",
    "format": "audio multiple choice",
    "answer": "Piano|Guitar|Violin|Trumpet",
    "display_answer": "Piano"
  },
  {
    "question": "Who appears in this clip?|person.mp4",
    "format": "video simple",
    "answer": "Laura",
    "display_answer": "Laura"
  },
  {
    "question": "What word appears in the clip?|word.mp4",
    "format": "video regex",
    "answer": "colou?r",
    "display_answer": "colour or color"
  },
  {
    "question": "Roughly how many seconds long is this event?|timing.mp4",
    "format": "video number",
    "answer": "9|11",
    "display_answer": "About 10 seconds (accepted range: 9–11)"
  },
  {
    "question": "Which object is shown?|object.mp4",
    "format": "video multiple choice",
    "answer": "Keyboard|Mouse|Monitor|Microphone",
    "display_answer": "Keyboard"
  },
  {
    "question": "Who is shown in this image?|person.jpg",
    "format": "image simple",
    "answer": "Laura",
    "display_answer": "Laura"
  },
  {
    "question": "What word is shown?|word.png",
    "format": "image regex",
    "answer": "colou?r",
    "display_answer": "colour or color"
  },
  {
    "question": "Roughly how many objects are visible?|objects.webp",
    "format": "image number",
    "answer": "9|11",
    "display_answer": "About 10 (accepted range: 9–11)"
  },
  {
    "question": "Which animal is shown?|animal.gif",
    "format": "image multiple choice",
    "answer": "Cat|Dog|Rabbit|Fox",
    "display_answer": "Cat"
  }
]
```

- Optional index-0 quiz settings currently support `"colour": "#RRGGBB"`. This changes the quiz theme while that quiz is open. In light mode the selected colour is used as the page background; dark mode derives a dark palette from the same colour. If the settings object is omitted, the normal `#F5A9B8` theme is used.
- Questions may optionally include `"example": "like this"`. For text-entry questions this becomes the input placeholder as `e.g. like this`. Number questions default to the neutral placeholder `e.g. 42.5` when no example is supplied. Multiple-choice questions do not use `example`.
- Questions may optionally include `"timer": 15`, where the value is a positive number of seconds. The countdown starts when that question is first displayed and is saved server-side, so refreshing the page does **not** reset it. When it reaches zero, the question is automatically marked incorrect and the normal answer/reveal media are shown, but the app does **not** move to the next question until the visitor presses the usual button. Timers work with normal, audio, video, and image question formats. Missing-media questions are skipped before their timer starts.
- `simple`: direct comparison ignoring case, leading/trailing whitespace, and repeated whitespace.
- `multiple choice`: correct choice **first**, followed by incorrect choices separated by `|`. Choices are shuffled consistently per visitor/question.
- `regex`: `answer` is a Python regular expression with a case-insensitive **full match**. Whitespace in the user's submitted answer is trimmed/collapsed. In JSON, escape regex backslashes (e.g. `"\\s+"`). Only use trusted, admin-authored patterns.
- `number`: `answer` is an **inclusive** numeric range written as `"minimum|maximum"`. For example, `"42|43"` accepts **42**, **43**, and everything between, including decimals such as **42.195**. Numbers are compared after removing all characters except ASCII digits and decimal points (including commas, currency symbols, spaces and letters). For example, `£1,290.50` becomes `1290.50`. Commas are removed as thousands separators, not interpreted as decimal marks. This means negative signs are also removed, so use **non-negative ranges only**. Bounds must be valid and the minimum must not exceed the maximum. To require exactly one number, repeat it (e.g. `"5|5"`).
- Audio questions use the same `question` syntax: `"the visible question|audiofile.mp3"`. The **final** `|` separates the visible question text from the filename, and the file must be directly inside `quizzes/audio/`. The page uses the browser's normal audio controls, including play/pause, seeking and volume where the browser exposes it. Four audio answer formats are available:
  - `audio simple`: same direct, case-insensitive comparison as `simple`.
  - `audio regex`: same case-insensitive Python regex **full match** as `regex`.
  - `audio number`: same inclusive `minimum|maximum` numeric range as `number`.
  - `audio multiple choice`: same rule as `multiple choice`; the **first** `|`-separated option is correct and the choices are shuffled before display.
  - Plain `audio` is still accepted as a backwards-compatible alias for `audio regex`.
- If an audio file is missing, the visitor is shown an explicit **Audio unavailable — question skipped** message. The question is excluded from the quiz total and cannot affect the score, regardless of which audio answer format it uses. For safety, audio filenames cannot contain `/` or `\` path separators.
- Video questions use the same syntax: `"the visible question|videofile.mp4"`. Video files must be `.mp4` files directly inside `quizzes/video/`. The browser's native video player provides play/pause, seeking, volume, fullscreen and other controls supported by that browser. Four video answer formats are available:
  - `video simple`: same direct, case-insensitive comparison as `simple`.
  - `video regex`: same case-insensitive Python regex **full match** as `regex`.
  - `video number`: same inclusive `minimum|maximum` numeric range as `number`.
  - `video multiple choice`: same rule as `multiple choice`; the **first** `|`-separated option is correct and the choices are shuffled before display.
- If a video file is missing, the visitor is shown an explicit **Video unavailable — question skipped** message. The question is excluded from the quiz total and cannot affect the score. Video filenames cannot contain `/` or `\` path separators.
- Image questions use the same syntax: `"the visible question|imagefile.png"`. Image files live directly inside `quizzes/image/`. Supported formats are **JPG/JPEG/JFIF, PNG/APNG, GIF, WebP, AVIF, BMP, SVG, and ICO**. Animated GIFs/WebP files animate normally in browsers. Four image answer formats are available:
  - `image simple`: same direct, case-insensitive comparison as `simple`.
  - `image regex`: same case-insensitive Python regex **full match** as `regex`.
  - `image number`: same inclusive `minimum|maximum` numeric range as `number`.
  - `image multiple choice`: same rule as `multiple choice`; the **first** `|`-separated option is correct and the choices are shuffled before display.
- If an image file is missing, the visitor is shown an explicit **Image unavailable — question skipped** message. The question is excluded from the quiz total and cannot affect the score. Image filenames cannot contain `/` or `\` path separators.
- `display_answer`: what visitors see after they submit an answer.
- Questions may also include optional answer-reveal media fields. These appear only **after the visitor submits their answer**, alongside `display_answer`:
  - `"display audio": "answer.mp3"` loads from `quizzes/audio/`.
  - `"display video": "answer.mp4"` loads from `quizzes/video/`; video display files must be `.mp4`.
  - `"display image": "answer.png"` loads from `quizzes/image/` and supports the same image extensions as image questions.
  - More than one display-media field may be used on the same question. Missing display media shows a small **Display … unavailable** message, but it does **not** skip the question or affect scoring. Filenames must refer directly to the appropriate media directory and cannot contain path separators.

For example, a normal text question can reveal an image with its answer:

```json
{
  "question": "Which planet is known as the Red Planet?",
  "format": "simple",
  "answer": "Mars",
  "display_answer": "Mars",
  "display image": "mars.png"
}
```

Or an audio question can reveal a second audio clip and a video explanation:

```json
{
  "question": "Name this sound|mystery.mp3",
  "format": "audio regex",
  "answer": "thunder",
  "display_answer": "Thunder",
  "display audio": "thunder-full.mp3",
  "display video": "thunder-explanation.mp4"
}
```

**Editing quizzes**

New quizzes and changes to quiz JSON are loaded automatically without restarting the server. Changing a quiz file resets progress for **that quiz only**. Adding, removing, or replacing an audio, video, or image file referenced by that quiz — including optional display media — also resets that quiz's progress so its question/reveal state stays consistent. Renaming a quiz file creates a new quiz name with separate progress.

**Progress, themes and privacy**

- Each browser receives a random, HTTP-only, SameSite=Lax `quiz_id` cookie (1-year expiry).
- Scores and position are saved in `data/progress.sqlite3`, not in the cookie or tied to an IP address.
- Returning with the same browser cookie resumes the same progress in each quiz.
- You can restart a quiz while taking it or reset an individual quiz from the main menu; the main menu also has a **Reset all quiz progress** button. Both menu reset actions require a confirmation and a CSRF token.
- Light/dark mode is remembered in browser `localStorage` and doesn't affect other users.
- Previously installed single-quiz databases automatically migrate their history to `general` (assuming the original questions are now in `quizzes/general.json`).
- Existing multi-quiz databases are automatically extended with timer state columns on startup; saved quiz progress is preserved.
- To clear all progress, shut down the app and delete `data/progress.sqlite3`, `data/progress.sqlite3-wal`, and `data/progress.sqlite3-shm` if present.

For public deployments, place Waitress behind an HTTPS reverse proxy and set `COOKIE_SECURE=1`.

### Updating an existing installation

When replacing `app.py`, restart the Waitress process so the new Python code is loaded. Templates are configured to auto-reload, so subsequent HTML template edits are picked up automatically.
