"""All configurable settings."""

TEXT_MODEL   = "meta-llama/llama-4-scout-17b-16e-instruct"
VISION_MODEL = "meta-llama/llama-4-maverick-17b-128e-instruct"
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
