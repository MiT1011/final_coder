"""LLM API service + Session manager with modular providers.

Providers:
- Groq (Qwen 3.6 27B, GPT-OSS 120B/20B, Compound) — always available, also
  powers Whisper STT. Qwen is the only Groq model that accepts images, so it
  serves vision for the text-only entries too.
- OpenAI (GPT-5 / GPT-5 mini) — text + vision via the chat.completions API.
- Anthropic (Opus 5, Sonnet 5, Opus 4.7, Sonnet 4.6, Haiku 4.5) — text +
  vision via the Messages API.

Request parameters differ per model within a provider, not just between
providers — see _GROQ_MODEL_PARAMS and _CLAUDE_MODEL_PARAMS. Sending the wrong
one is a 400, not a silent no-op.

API keys are persisted with DPAPI encryption (see `secure_store`). The file
on disk in %APPDATA%\\InterviewAssistant\\api_keys.dat is opaque bytes; only
the same Windows user can decrypt it.
"""
import os, re, sys, uuid, io, base64, logging
import config as cfg
from secure_store import load_secret_json, save_secret_json

log = logging.getLogger(__name__)

# Vendor SDKs (groq, openai, anthropic) are imported lazily inside the
# corresponding provider's __init__, NOT at module top. Reason: pydantic v2
# in those SDKs is heavy on import (200–600ms each), and we don't want to
# pay that cost unless the user actually selects that provider. Importing
# `llm_service` itself stays cheap so the splash bar can advance smoothly.

try:
    from dotenv import load_dotenv
    if getattr(sys, 'frozen', False):
        _base = sys._MEIPASS
    else:
        _base = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(_base, '.env'))
except Exception:
    pass

_APP_DIR = os.path.join(os.getenv("APPDATA", os.path.expanduser("~")), "InterviewAssistant")
# Note: .dat extension — file is encrypted, not JSON. Old api_keys.json (if
# present from a prior install) is auto-migrated on first read.
_KEYS_FILE = os.path.join(_APP_DIR, "api_keys.dat")
_LEGACY_KEYS_FILE = os.path.join(_APP_DIR, "api_keys.json")


def _migrate_legacy_keys():
    """If a legacy plaintext api_keys.json exists, re-encrypt and delete it."""
    if not os.path.isfile(_LEGACY_KEYS_FILE):
        return
    try:
        import json
        with open(_LEGACY_KEYS_FILE, "r") as f:
            keys = json.load(f)
        save_secret_json(_KEYS_FILE, keys)
        os.remove(_LEGACY_KEYS_FILE)
        log.info("Migrated legacy api_keys.json -> api_keys.dat (DPAPI)")
    except Exception:
        log.exception("Legacy key migration failed")


def load_api_keys():
    """Decrypt and return the API keys dict. Returns {} on any failure."""
    _migrate_legacy_keys()
    return load_secret_json(_KEYS_FILE)


def save_api_keys(keys_dict):
    """Encrypt and persist the API keys dict."""
    os.makedirs(_APP_DIR, exist_ok=True)
    save_secret_json(_KEYS_FILE, keys_dict)


def wipe_api_keys():
    """Delete the stored API keys file (and any legacy plaintext version)."""
    for path in (_KEYS_FILE, _LEGACY_KEYS_FILE):
        try:
            if os.path.isfile(path):
                os.remove(path)
                log.info("Removed %s", path)
        except Exception:
            log.exception("Failed to remove %s", path)


def get_api_key(provider="groq"):
    keys = load_api_keys()
    v = keys.get(provider) or ""
    return v.strip() if isinstance(v, str) else ""


def available_providers():
    """Return the set of provider names with a non-empty key configured."""
    keys = load_api_keys()
    return {p for p, v in keys.items() if isinstance(v, str) and v.strip()}


def available_models():
    """Filter cfg.AVAILABLE_MODELS to entries whose required key is present."""
    have = available_providers()
    return {name: info for name, info in cfg.AVAILABLE_MODELS.items()
            if info.get("requires") in have}


def detect_question_type(text):
    t = (text or "").lower()
    if any(k in t for k in ["write", "implement", "code", "function", "algorithm", "debug", "fix"]):
        return "code"
    if any(k in t for k in ["tell me about", "describe a time", "how did you", "strength", "weakness"]):
        return "behavioral"
    if any(k in t for k in ["design", "architecture", "scale", "system", "database", "microservice"]):
        return "system_design"
    return "general"


def img_to_b64(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def transcribe_audio(wav_bytes):
    """Transcribe a WAV blob using Groq Whisper. STT always uses Groq regardless of the active LLM provider."""
    try:
        from groq import Groq
    except Exception as e:
        raise RuntimeError(f"groq SDK not installed: {e}")
    key = get_api_key("groq")
    if not key:
        raise ValueError("Groq API key required for audio transcription.")
    client = Groq(api_key=key)
    log.info("Whisper request: %d bytes, model=%s", len(wav_bytes), cfg.WHISPER_MODEL)
    try:
        response = client.audio.transcriptions.create(
            file=("audio.wav", wav_bytes, "audio/wav"),
            model=cfg.WHISPER_MODEL,
        )
    except Exception:
        log.exception("Whisper API call failed")
        raise
    text = getattr(response, "text", None)
    if text is None:
        text = str(response) if response else ""
    text = text.strip()
    log.info("Whisper response: %d chars", len(text))
    return text


# ---------------------------------------------------------------------------
# Session manager — shared across all providers.
# ---------------------------------------------------------------------------
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
        # `content` is always plain text in memory — images are dropped after
        # the turn that sent them, leaving only a textual breadcrumb.
        self.memory.append({"role": role, "content": content})

    def get_recent_memory(self):
        return self.memory[-(cfg.CHAT_MEMORY_LIMIT * 2):]

    def build_system_prompt(self):
        stack = ", ".join(self.tech_stack) or "general"
        return cfg.SYSTEM_PROMPT_TEMPLATE.format(
            name=self.candidate_name, role=self.job_role, stack=stack)


# ---------------------------------------------------------------------------
# Provider base class
# ---------------------------------------------------------------------------
class LLMProvider:
    def __init__(self, text_model, vision_model):
        self.text_model = text_model
        self.vision_model = vision_model

    def chat(self, message, session):
        raise NotImplementedError

    def analyze_screenshots(self, images_b64, prompt, session):
        raise NotImplementedError


def _memory_breadcrumb(images_b64, prompt_text):
    n = len(images_b64)
    return f"[{n} screenshot{'s' if n != 1 else ''}] {prompt_text}"


# ---------------------------------------------------------------------------
# Groq
# ---------------------------------------------------------------------------
# Qwen is a reasoning model: without suppression its content starts with a
# "<think>...</think>" block. We disable thinking at the API level
# (reasoning_effort="none" + reasoning_format="hidden"), and strip any block
# that still leaks through before the answer reaches the UI or chat memory.
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def _strip_thinking(text):
    return _THINK_RE.sub("", text or "").strip()


# Groq's reasoning knobs are per-model, and sending the wrong value is a hard
# 400 rather than a silent no-op. Verified against the live API:
#
#   qwen/qwen3.6-27b      reasoning_effort must be "none" or "default"
#   openai/gpt-oss-*      reasoning_effort must be "low", "medium" or "high"
#   groq/compound*        reasoning_effort is rejected entirely
#
# So these cannot be hardcoded on the request — a single dropdown entry can
# route text at gpt-oss and vision at Qwen, which need different values.
#
# "none" on Qwen is doing real work: with reasoning left on, thinking eats the
# whole MAX_TOKENS budget and the answer comes back empty. gpt-oss keeps its
# reasoning in a separate `reasoning` field rather than in the content, so it
# only needs "hidden" to stop that field being billed back to us; "low" keeps
# it quick, which is what a live interview needs.
_GROQ_MODEL_PARAMS = {
    "qwen/qwen3.6-27b":    {"reasoning_effort": "none", "reasoning_format": "hidden"},
    "openai/gpt-oss-120b": {"reasoning_effort": "low",  "reasoning_format": "hidden"},
    "openai/gpt-oss-20b":  {"reasoning_effort": "low",  "reasoning_format": "hidden"},
    # groq/compound and groq/compound-mini take neither parameter.
}


def _groq_params(model):
    """Extra request kwargs for a Groq model. Unknown models get none, which
    is the shape every Groq chat model accepts."""
    return _GROQ_MODEL_PARAMS.get(model, {})


class GroqProvider(LLMProvider):
    def __init__(self, text_model, vision_model):
        super().__init__(text_model, vision_model)
        try:
            from groq import Groq
        except Exception as e:
            raise RuntimeError(f"groq SDK not installed: {e}")
        key = get_api_key("groq")
        if not key:
            raise ValueError("Groq API Key is not set.")
        self.client = Groq(api_key=key)

    def chat(self, message, session):
        messages = [{"role": "system", "content": session.build_system_prompt()}]
        messages += session.get_recent_memory()
        messages.append({"role": "user", "content": message})
        response = self.client.chat.completions.create(
            model=self.text_model, messages=messages,
            max_tokens=cfg.MAX_TOKENS, temperature=cfg.TEMPERATURE,
            **_groq_params(self.text_model))
        answer = _strip_thinking(response.choices[0].message.content)
        session.push_memory("user", message)
        session.push_memory("assistant", answer)
        session.message_count += 1
        return {"answer": answer,
                "question_type": detect_question_type(message),
                "memory_depth": len(session.get_recent_memory()) // 2}

    def analyze_screenshots(self, images_b64, prompt, session):
        prompt_text = prompt or "Analyze these screenshots. Identify questions/problems and give concise answers."
        content = []
        for b64 in images_b64:
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"}})
        # Inline prior conversation as text — Groq images-in-history is flaky.
        recent = session.get_recent_memory()
        ctx = ""
        if recent:
            pairs = []
            for i in range(0, len(recent) - 1, 2):
                u = recent[i].get("content", "")
                a = recent[i + 1].get("content", "") if i + 1 < len(recent) else ""
                pairs.append(f"User: {u}\nAssistant: {a}")
            if pairs:
                ctx = "Previous conversation context:\n" + "\n---\n".join(pairs) + "\n\n"
        content.append({"type": "text", "text": ctx + prompt_text})

        messages = [{"role": "system", "content": session.build_system_prompt()},
                    {"role": "user", "content": content}]
        response = self.client.chat.completions.create(
            model=self.vision_model, messages=messages,
            max_tokens=cfg.MAX_TOKENS, temperature=cfg.TEMPERATURE,
            **_groq_params(self.vision_model))
        answer = _strip_thinking(response.choices[0].message.content)
        session.push_memory("user", _memory_breadcrumb(images_b64, prompt_text))
        session.push_memory("assistant", answer)
        session.message_count += 1
        return {"answer": answer,
                "question_type": detect_question_type(answer),
                "screenshots_analyzed": len(images_b64),
                "memory_depth": len(session.get_recent_memory()) // 2}


# ---------------------------------------------------------------------------
# OpenAI (GPT-5 family)
# ---------------------------------------------------------------------------
class OpenAIProvider(LLMProvider):
    def __init__(self, text_model, vision_model):
        super().__init__(text_model, vision_model)
        try:
            from openai import OpenAI
        except Exception as e:
            raise RuntimeError(f"openai SDK not installed (pip install openai): {e}")
        key = get_api_key("openai")
        if not key:
            raise ValueError("OpenAI API Key is not set.")
        self.client = OpenAI(api_key=key)

    def chat(self, message, session):
        messages = [{"role": "system", "content": session.build_system_prompt()}]
        messages += session.get_recent_memory()
        messages.append({"role": "user", "content": message})
        response = self.client.chat.completions.create(
            model=self.text_model, messages=messages,
            max_tokens=cfg.MAX_TOKENS, temperature=cfg.TEMPERATURE)
        answer = response.choices[0].message.content
        session.push_memory("user", message)
        session.push_memory("assistant", answer)
        session.message_count += 1
        return {"answer": answer,
                "question_type": detect_question_type(message),
                "memory_depth": len(session.get_recent_memory()) // 2}

    def analyze_screenshots(self, images_b64, prompt, session):
        prompt_text = prompt or "Analyze these screenshots. Identify questions/problems and give concise answers."
        content = [{"type": "text", "text": prompt_text}]
        for b64 in images_b64:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"}
            })
        messages = [{"role": "system", "content": session.build_system_prompt()}]
        messages += session.get_recent_memory()
        messages.append({"role": "user", "content": content})

        response = self.client.chat.completions.create(
            model=self.vision_model, messages=messages,
            max_tokens=cfg.MAX_TOKENS, temperature=cfg.TEMPERATURE)
        answer = response.choices[0].message.content
        session.push_memory("user", _memory_breadcrumb(images_b64, prompt_text))
        session.push_memory("assistant", answer)
        session.message_count += 1
        return {"answer": answer,
                "question_type": detect_question_type(answer),
                "screenshots_analyzed": len(images_b64),
                "memory_depth": len(session.get_recent_memory()) // 2}


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------
# Two model-dependent rules make a single hardcoded request shape impossible:
#
# 1. `temperature` (and top_p/top_k) was REMOVED on the current flagships —
#    Opus 5, Sonnet 5, Opus 4.8 and Opus 4.7 return a 400 if it is sent. Only
#    Sonnet 4.6, Opus 4.6 and Haiku 4.5 still accept it.
# 2. Opus 5 and Sonnet 5 run adaptive thinking by DEFAULT. Thinking tokens are
#    drawn from max_tokens, so at MAX_TOKENS=1500 the model can spend the whole
#    budget reasoning and return a response with no text block at all. Opus 4.7
#    and the 4.x models below it do not think unless asked, so they only need
#    the temperature fix.
#
# Anything not listed gets {} — no temperature, no thinking override — which is
# the shape most likely to be accepted by a model added here later.
_CLAUDE_MODEL_PARAMS = {
    "claude-opus-5":     {"thinking": {"type": "disabled"}},
    "claude-sonnet-5":   {"thinking": {"type": "disabled"}},
    "claude-opus-4-7":   {},
    "claude-sonnet-4-6": {"temperature": cfg.TEMPERATURE},
    "claude-haiku-4-5":  {"temperature": cfg.TEMPERATURE},
}

# Anthropic's guidance for running with thinking turned off: the model can
# occasionally leak an internal tag into the visible answer. Naming the tags or
# adding a "do not reason" rule makes it worse, so this is the whole mitigation.
_NO_TAGS_RULE = "\n- Do not include internal or system XML tags in your response."


def _claude_params(model):
    return _CLAUDE_MODEL_PARAMS.get(model, {})


def _claude_system_prompt(model, session):
    prompt = session.build_system_prompt()
    thinking = _claude_params(model).get("thinking") or {}
    if thinking.get("type") == "disabled":
        prompt += _NO_TAGS_RULE
    return prompt


class ClaudeProvider(LLMProvider):
    def __init__(self, text_model, vision_model):
        super().__init__(text_model, vision_model)
        try:
            import anthropic
        except Exception as e:
            raise RuntimeError(f"anthropic SDK not installed (pip install anthropic): {e}")
        key = get_api_key("claude")
        if not key:
            raise ValueError("Claude API Key is not set.")
        self.client = anthropic.Anthropic(api_key=key)

    @staticmethod
    def _memory_to_anthropic(recent):
        """Re-shape the shared session.memory into Anthropic's messages array.

        Anthropic forbids a leading system role and uses `messages=[{role, content}]`
        with `system=...` passed separately. Roles alternate user/assistant.
        """
        out = []
        for m in recent:
            role = m.get("role")
            content = m.get("content", "")
            if role not in ("user", "assistant"):
                continue
            out.append({"role": role, "content": content})
        return out

    def chat(self, message, session):
        messages = self._memory_to_anthropic(session.get_recent_memory())
        messages.append({"role": "user", "content": message})
        response = self.client.messages.create(
            model=self.text_model,
            system=_claude_system_prompt(self.text_model, session),
            messages=messages,
            max_tokens=cfg.MAX_TOKENS,
            **_claude_params(self.text_model),
        )
        answer = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        session.push_memory("user", message)
        session.push_memory("assistant", answer)
        session.message_count += 1
        return {"answer": answer,
                "question_type": detect_question_type(message),
                "memory_depth": len(session.get_recent_memory()) // 2}

    def analyze_screenshots(self, images_b64, prompt, session):
        prompt_text = prompt or "Analyze these screenshots. Identify questions/problems and give concise answers."
        user_content = []
        for b64 in images_b64:
            user_content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": b64,
                },
            })
        user_content.append({"type": "text", "text": prompt_text})

        messages = self._memory_to_anthropic(session.get_recent_memory())
        messages.append({"role": "user", "content": user_content})

        response = self.client.messages.create(
            model=self.vision_model,
            system=_claude_system_prompt(self.vision_model, session),
            messages=messages,
            max_tokens=cfg.MAX_TOKENS,
            **_claude_params(self.vision_model),
        )
        answer = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        session.push_memory("user", _memory_breadcrumb(images_b64, prompt_text))
        session.push_memory("assistant", answer)
        session.message_count += 1
        return {"answer": answer,
                "question_type": detect_question_type(answer),
                "screenshots_analyzed": len(images_b64),
                "memory_depth": len(session.get_recent_memory()) // 2}


# ---------------------------------------------------------------------------
# Service facade
# ---------------------------------------------------------------------------
_PROVIDER_REGISTRY = {
    "groq": GroqProvider,
    "openai": OpenAIProvider,
    "claude": ClaudeProvider,
}


class LLMService:
    def __init__(self, model_key):
        self.model_key = None
        self.set_model(model_key)

    def set_model(self, model_key):
        model_info = cfg.AVAILABLE_MODELS.get(model_key)
        if not model_info:
            raise ValueError(f"Model {model_key} not found in configuration.")
        provider_cls = _PROVIDER_REGISTRY.get(model_info["provider"])
        if provider_cls is None:
            raise ValueError(f"Unknown provider: {model_info['provider']}")
        self.provider = provider_cls(model_info["text"], model_info["vision"])
        self.model_key = model_key

    def chat(self, message, session):
        return self.provider.chat(message, session)

    def analyze_screenshots(self, images_b64, prompt, session):
        return self.provider.analyze_screenshots(images_b64, prompt, session)
