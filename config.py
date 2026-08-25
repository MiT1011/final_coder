"""All configurable settings."""

# Model catalogue. `text`/`vision` hold the provider-side model IDs; `requires`
# names the API key that must be present for the entry to show up in the picker.
#
# Per-model request parameters (reasoning knobs, whether `temperature` is even
# accepted) are NOT here — they live in llm_service.py next to the provider that
# sends them, because a single entry can point `text` and `vision` at two models
# that take different parameters.
AVAILABLE_MODELS = {
    # --- Groq: always-available free tier. Its key is collected as mandatory
    # after login and also powers Whisper STT. -------------------------------
    #
    # These IDs were verified against Groq's live model catalogue. Groq retires
    # models without notice — the Llama Scout and Llama 3.3 70B entries this
    # file used to carry no longer exist on the account and would 404.
    #
    # Qwen 3.6 27B is the ONLY vision-capable model Groq currently serves, so
    # every text-only Groq entry below falls back to it for screenshot
    # analysis. It is a reasoning model: GroqProvider suppresses the <think>
    # block so it never reaches the UI.
    "Groq Qwen 3.6 27B": {
        "provider": "groq",
        "text": "qwen/qwen3.6-27b",
        "vision": "qwen/qwen3.6-27b",
        "requires": "groq",
    },
    "Groq GPT-OSS 120B": {
        "provider": "groq",
        "text": "openai/gpt-oss-120b",
        "vision": "qwen/qwen3.6-27b",   # text-only model — Qwen handles images
        "requires": "groq",
    },
    "Groq GPT-OSS 20B": {
        "provider": "groq",
        "text": "openai/gpt-oss-20b",
        "vision": "qwen/qwen3.6-27b",
        "requires": "groq",
    },
    # Compound is an agentic system rather than a bare model: it can run web
    # search and code execution on Groq's side, which is useful for factual
    # lookups mid-interview but costs extra latency on every turn.
    "Groq Compound": {
        "provider": "groq",
        "text": "groq/compound",
        "vision": "qwen/qwen3.6-27b",
        "requires": "groq",
    },
    "GPT-5": {
        "provider": "openai",
        "text": "gpt-5",
        "vision": "gpt-5",
        "requires": "openai",
    },
    "GPT-5 mini": {
        "provider": "openai",
        "text": "gpt-5-mini",
        "vision": "gpt-5-mini",
        "requires": "openai",
    },
    # --- Anthropic. Every model below is vision-capable, so text and vision
    # point at the same ID. See _CLAUDE_MODEL_PARAMS in llm_service.py: the
    # current flagships reject `temperature` outright and default to adaptive
    # thinking, both of which need per-model handling. --------------------
    "Claude Opus 5": {
        "provider": "claude",
        "text": "claude-opus-5",
        "vision": "claude-opus-5",
        "requires": "claude",
    },
    "Claude Sonnet 5": {
        "provider": "claude",
        "text": "claude-sonnet-5",
        "vision": "claude-sonnet-5",
        "requires": "claude",
    },
    "Claude Opus 4.7": {
        "provider": "claude",
        "text": "claude-opus-4-7",
        "vision": "claude-opus-4-7",
        "requires": "claude",
    },
    "Claude Sonnet 4.6": {
        "provider": "claude",
        "text": "claude-sonnet-4-6",
        "vision": "claude-sonnet-4-6",
        "requires": "claude",
    },
    # Cheapest and fastest Claude — 200K context (the others are 1M), which is
    # far more than CHAT_MEMORY_LIMIT will ever produce.
    "Claude Haiku 4.5": {
        "provider": "claude",
        "text": "claude-haiku-4-5",
        "vision": "claude-haiku-4-5",
        "requires": "claude",
    },
}
DEFAULT_MODEL = "Groq Qwen 3.6 27B"
MAX_TOKENS   = 1500
TEMPERATURE  = 0.3
CHAT_MEMORY_LIMIT = 15

DEFAULT_JOB_ROLE       = "Software Engineer"
DEFAULT_CANDIDATE_NAME = "Candidate"
DEFAULT_TECH_STACK     = ["Python", "JavaScript", "System Design"]

WINDOW_WIDTH  = 520
WINDOW_HEIGHT = 700
WINDOW_X      = 30
WINDOW_Y      = 30
DEFAULT_OPACITY = 0.95
MOVE_STEP       = 20
MAX_SCREENSHOTS = 3

BG="#0d1117"; BG2="#161b22"; BG3="#21262d"
ACCENT="#58a6ff"; GREEN="#3fb950"; RED="#f85149"
YELLOW="#d29922"; PURPLE="#bc8cff"
FG="#e6edf3"; FG2="#8b949e"; USER_BG="#1a3a5c"; AI_BG="#161b22"

FONT_MONO=("Consolas",10); FONT_UI=("Segoe UI",10)
FONT_SML=("Segoe UI",8); FONT_TINY=("Segoe UI",7)

# --- Audio (system loopback) settings ---
WHISPER_MODEL            = "whisper-large-v3-turbo"
AUDIO_BLOCK_MS           = 100      # callback chunk size
AUDIO_SILENCE_THRESHOLD  = 0.012    # RMS threshold to call a chunk "silent"
AUDIO_SILENCE_DURATION_S = 1.2      # silence required to close a speech segment
AUDIO_MIN_SPEECH_S       = 0.6      # min duration to treat as real speech (drops clicks/blips)
AUDIO_MAX_SEGMENT_S      = 30       # hard cap on a single segment
AUDIO_QUEUE_MAX          = 8        # backlog cap to avoid runaway memory

# Whisper request size limit. Groq's free tier caps file upload at 25 MB; we
# stay below that. Manual-mode recordings longer than this are split into
# multiple WAV chunks and transcribed sequentially, then merged.
WHISPER_MAX_BYTES        = 24 * 1024 * 1024    # 24 MB
MANUAL_RECORD_MAX_S      = 3 * 60              # 3-min hard cap — recorder auto-stops at this point

SYSTEM_PROMPT_TEMPLATE = """You are a smart, concise interview assistant helping {name} ace a {role} interview.

Rules:
- Answer CONCISELY (under 200 words unless code is needed)
- For coding questions: give working code + brief explanation
- For behavioral: STAR-format bullet points
- For system design: key components + trade-offs
- For MCQ: answer + 1-line reason
- Use bullet points, avoid markdown headers
- Remember previous messages in the conversation and be coherent
- Tech stack: {stack}"""
