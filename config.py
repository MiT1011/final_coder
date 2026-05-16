"""All configurable settings."""

AVAILABLE_MODELS = {
    "Groq (Llama Scout)": {
        "provider": "groq",
        "text": "meta-llama/llama-4-scout-17b-16e-instruct",
        "vision": "meta-llama/llama-4-scout-17b-16e-instruct"
    },
    "OpenAI (GPT-4o)": {
        "provider": "openai",
        "text": "gpt-4o",
        "vision": "gpt-4o"
    },
    "Claude (3.5 Sonnet)": {
        "provider": "claude",
        "text": "claude-3-5-sonnet-20240620",
        "vision": "claude-3-5-sonnet-20240620"
    }
}
MAX_TOKENS   = 1500
TEMPERATURE  = 0.3
CHAT_MEMORY_LIMIT = 5

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
