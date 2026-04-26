"""LLM API service + Session manager with modular providers."""
import os, sys, uuid, io, base64
from groq import Groq
from dotenv import load_dotenv
import config as cfg
import json

if getattr(sys, 'frozen', False):
    _base = sys._MEIPASS
else:
    _base = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_base, '.env'))

_APP_DIR = os.path.join(os.getenv("APPDATA", os.path.expanduser("~")), "InterviewAssistant")
_KEYS_FILE = os.path.join(_APP_DIR, "api_keys.json")

def load_api_keys():
    """Load API keys from local storage."""
    if os.path.isfile(_KEYS_FILE):
        with open(_KEYS_FILE, "r") as f:
            try:
                return json.load(f)
            except:
                pass
    return {}

def save_api_keys(keys_dict):
    """Save API keys to local storage."""
    os.makedirs(_APP_DIR, exist_ok=True)
    with open(_KEYS_FILE, "w") as f:
        json.dump(keys_dict, f)

def get_api_key(provider="groq"):
    keys = load_api_keys()
    if provider in keys and keys[provider]:
        return keys[provider]
    return ""

def detect_question_type(text):
    t = text.lower()
    if any(k in t for k in ["write","implement","code","function","algorithm","debug","fix"]): return "code"
    if any(k in t for k in ["tell me about","describe a time","how did you","strength","weakness"]): return "behavioral"
    if any(k in t for k in ["design","architecture","scale","system","database","microservice"]): return "system_design"
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

class LLMProvider:
    def __init__(self, text_model, vision_model):
        self.text_model = text_model
        self.vision_model = vision_model
    
    def chat(self, message, session):
        raise NotImplementedError
    
    def analyze_screenshots(self, images_b64, prompt, session):
        raise NotImplementedError

class GroqProvider(LLMProvider):
    def __init__(self, text_model, vision_model):
        super().__init__(text_model, vision_model)
        key = get_api_key("groq")
        if not key:
            raise ValueError("Groq API Key is not set.")
        self.client = Groq(api_key=key)

    def chat(self, message, session):
        messages = [{"role":"system","content":session.build_system_prompt()}]
        messages += session.get_recent_memory()
        messages.append({"role":"user","content":message})
        
        response = self.client.chat.completions.create(
            model=self.text_model, messages=messages,
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
            model=self.vision_model, messages=messages,
            max_tokens=cfg.MAX_TOKENS, temperature=cfg.TEMPERATURE)
        answer = response.choices[0].message.content
        
        session.push_memory("user", f"[{len(images_b64)} screenshot(s)] {prompt_text}")
        session.push_memory("assistant", answer)
        session.message_count += 1
        return {"answer":answer,"question_type":detect_question_type(answer),
                "screenshots_analyzed":len(images_b64),
                "memory_depth":len(session.get_recent_memory())//2}

class OpenAIProvider(LLMProvider):
    # Skeleton to be implemented later
    def chat(self, message, session):
        return {"answer": "OpenAI not implemented yet.", "question_type": "general", "memory_depth": 0}
    def analyze_screenshots(self, images_b64, prompt, session):
        return {"answer": "OpenAI not implemented yet.", "question_type": "general", "memory_depth": 0, "screenshots_analyzed": 0}

class ClaudeProvider(LLMProvider):
    # Skeleton to be implemented later
    def chat(self, message, session):
        return {"answer": "Claude not implemented yet.", "question_type": "general", "memory_depth": 0}
    def analyze_screenshots(self, images_b64, prompt, session):
        return {"answer": "Claude not implemented yet.", "question_type": "general", "memory_depth": 0, "screenshots_analyzed": 0}

class LLMService:
    def __init__(self, model_key):
        self.set_model(model_key)
        
    def set_model(self, model_key):
        model_info = cfg.AVAILABLE_MODELS.get(model_key)
        if not model_info:
            raise ValueError(f"Model {model_key} not found in configuration.")
            
        provider_name = model_info["provider"]
        t_model = model_info["text"]
        v_model = model_info["vision"]
        
        if provider_name == "groq":
            self.provider = GroqProvider(t_model, v_model)
        elif provider_name == "openai":
            self.provider = OpenAIProvider(t_model, v_model)
        elif provider_name == "claude":
            self.provider = ClaudeProvider(t_model, v_model)
        else:
            raise ValueError(f"Unknown provider: {provider_name}")

    def chat(self, message, session):
        return self.provider.chat(message, session)

    def analyze_screenshots(self, images_b64, prompt, session):
        return self.provider.analyze_screenshots(images_b64, prompt, session)
