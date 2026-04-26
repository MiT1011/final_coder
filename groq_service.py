"""Groq API service + Session manager."""
import os, sys, uuid, io, base64
from groq import Groq
from dotenv import load_dotenv
import config as cfg

# Resolve .env path for both normal Python and PyInstaller exe
if getattr(sys, 'frozen', False):
    _base = sys._MEIPASS
else:
    _base = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_base, '.env'))

# --- Persistent API key storage (works on any system) ---
_APP_DIR = os.path.join(os.getenv("APPDATA", os.path.expanduser("~")), "InterviewAssistant")
_KEY_FILE = os.path.join(_APP_DIR, "api_key.txt")

def get_api_key():
    """Check: 1) saved local file  2) env variable  3) bundled .env"""
    # 1. Local saved key (user entered it before)
    if os.path.isfile(_KEY_FILE):
        with open(_KEY_FILE, "r") as f:
            key = f.read().strip()
            if key: return key
    # 2. Environment / .env
    key = os.getenv("GROQ_API_KEY", "").strip()
    if key: return key
    # 3. Not found
    return None

def save_api_key(key):
    """Save key to local AppData so it persists across runs."""
    os.makedirs(_APP_DIR, exist_ok=True)
    with open(_KEY_FILE, "w") as f:
        f.write(key.strip())

def detect_question_type(text):
    t = text.lower()
    if any(k in t for k in ["write","implement","code","function","algorithm","debug","fix"]):
        return "code"
    if any(k in t for k in ["tell me about","describe a time","how did you","strength","weakness"]):
        return "behavioral"
    if any(k in t for k in ["design","architecture","scale","system","database","microservice"]):
        return "system_design"
    return "general"

def img_to_b64(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()

class SessionManager:
    def __init__(self):
        self.session_id = None
        self.memory = []
        self.message_count = 0
        self.job_role = cfg.DEFAULT_JOB_ROLE
        self.candidate_name = cfg.DEFAULT_CANDIDATE_NAME
        self.tech_stack = cfg.DEFAULT_TECH_STACK

    def start_session(self):
        self.session_id = str(uuid.uuid4())
        self.memory = []
        self.message_count = 0
        return self.session_id

    def reset_session(self):
        self.memory = []
        self.message_count = 0

    def push_memory(self, role, content):
        self.memory.append({"role": role, "content": content})

    def get_recent_memory(self):
        return self.memory[-(cfg.CHAT_MEMORY_LIMIT * 2):]

    def build_system_prompt(self):
        stack = ", ".join(self.tech_stack) or "general"
        return cfg.SYSTEM_PROMPT_TEMPLATE.format(
            name=self.candidate_name, role=self.job_role, stack=stack)

class GroqService:
    def __init__(self, api_key=None):
        self.client = Groq(api_key=api_key or get_api_key())

    def chat(self, message, session):
        messages = [{"role":"system","content":session.build_system_prompt()}]
        messages += session.get_recent_memory()
        messages.append({"role":"user","content":message})
        response = self.client.chat.completions.create(
            model=cfg.TEXT_MODEL, messages=messages,
            max_tokens=cfg.MAX_TOKENS, temperature=cfg.TEMPERATURE)
        answer = response.choices[0].message.content
        session.push_memory("user", message)
        session.push_memory("assistant", answer)
        session.message_count += 1
        return {"answer":answer,"question_type":detect_question_type(message),
                "memory_depth":len(session.get_recent_memory())//2}

    def analyze_screenshots(self, images_b64, prompt, session):
        content = []
        for b64 in images_b64:
            content.append({"type":"image_url","image_url":{"url":f"data:image/png;base64,{b64}"}})
        prompt_text = prompt or "Analyze these interview screenshots. Identify all questions/problems and give concise answers."
        recent = session.get_recent_memory()
        ctx = ""
        if recent:
            pairs = []
            for i in range(0, len(recent)-1, 2):
                u = recent[i].get("content","")
                a = recent[i+1].get("content","") if i+1<len(recent) else ""
                if isinstance(u,list): u = next((x["text"] for x in u if x["type"]=="text"),"")
                if isinstance(a,list): a = next((x["text"] for x in a if x["type"]=="text"),"")
                pairs.append(f"User: {u}\nAssistant: {a}")
            if pairs: ctx = "Previous conversation context:\n"+"\n---\n".join(pairs)+"\n\n"
        content.append({"type":"text","text":ctx+prompt_text})
        messages = [{"role":"system","content":session.build_system_prompt()},
                     {"role":"user","content":content}]
        response = self.client.chat.completions.create(
            model=cfg.VISION_MODEL, messages=messages,
            max_tokens=cfg.MAX_TOKENS, temperature=cfg.TEMPERATURE)
        answer = response.choices[0].message.content
        session.push_memory("user", f"[{len(images_b64)} screenshot(s)] {prompt_text}")
        session.push_memory("assistant", answer)
        session.message_count += 1
        return {"answer":answer,"question_type":detect_question_type(answer),
                "screenshots_analyzed":len(images_b64),
                "memory_depth":len(session.get_recent_memory())//2}
