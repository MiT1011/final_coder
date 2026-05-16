# ⚡ Interview Assistant

A stealth, always-on-top desktop overlay that helps you ace technical interviews using LLM-powered AI. Built with Python & Tkinter.

---

## ✨ Features

- **Google OAuth2 Login** — Secure authentication via your Google account on first launch.
- **Text Chat** — Ask coding, behavioral, system design, or MCQ questions and get concise AI-generated answers.
- **Screenshot Analysis** — Capture up to 3 screenshots and let AI analyze & answer questions visible on screen.
- **Dynamic Model Selector** — Switch between LLM providers (Groq / OpenAI / Claude) from the title bar dropdown.
- **Stealth Mode** — Window is invisible to screen-sharing tools (e.g., Zoom, Teams).
- **Click-Through Mode** — Make the overlay transparent to mouse clicks so you can interact with apps behind it.
- **Customizable Opacity** — Adjust window transparency via the Settings panel.
- **Keyboard-Driven** — Operate the entire app without touching the mouse.

---

## 🗂 Project Structure

```
├── main.py              # Main Tkinter application & UI
├── auth.py              # Google OAuth2 login window
├── llm_service.py       # Modular LLM provider architecture (Groq, OpenAI, Claude)
├── config.py            # All configurable settings, models, prompts
├── client_secret.json   # Google OAuth2 credentials (DO NOT commit)
├── .env                 # API keys (DO NOT commit)
├── requirement.txt      # Python dependencies
├── InterviewAssistant.spec  # PyInstaller build spec
└── .gitignore
```

---

## 🚀 Getting Started

### Prerequisites

- Python 3.10+
- A [Groq API Key](https://console.groq.com/) (free tier available)
- Google Cloud OAuth2 Desktop credentials (`client_secret.json`)

### Installation

```bash
# Clone the repository
git clone <repo-url>
cd new_chat

# Install dependencies
pip install -r requirement.txt
```

### Running

```bash
python main.py
```

On first launch:
1. A **Google Login** window will appear — authenticate via your browser.
2. You will be prompted for your **Groq API key** (saved locally for future sessions).
3. The main overlay window appears — start chatting!

---

## ⌨️ Keyboard Shortcuts

| Shortcut         | Action                  |
|------------------|-------------------------|
| `Alt+Shift+S`    | Switch to Screenshot mode |
| `Alt+G`          | Capture screenshot      |
| `Alt+B`          | Back to Chat            |
| `Alt+E`          | Submit / Send           |
| `Alt+T`          | Toggle click-through    |
| `Alt+P`          | Focus prompt input      |
| `Alt+H`          | Hide / Un-hide window   |
| `Alt+Q`          | Quit                    |
| `Alt+S`          | Open Settings           |
| `Alt+↑↓←→`      | Move window             |

---

## ⚙️ Settings

Open with `Alt+S` or the ⚙ button in the title bar:

- **API Keys** — Manage Groq, OpenAI, and Claude API keys (masked with `****` for security).
- **Opacity** — Adjust overlay transparency (10%–100%).
- **Shortcuts Reference** — Quick reminder of all hotkeys.

---

## 🏗 Building as Standalone EXE

### Prerequisites

```bash
pip install pyinstaller
```

### Build Command

```bash
# Using the spec file (recommended)
python -m PyInstaller InterviewAssistant.spec --noconfirm

# Or build from scratch (one-file, no console)
python -m PyInstaller main.py --onefile --noconsole --name InterviewAssistant --add-data ".env;." --add-data "client_secret.json;." --hidden-import groq --hidden-import dotenv --hidden-import PIL --hidden-import keyboard --hidden-import google.auth --hidden-import google_auth_oauthlib --hidden-import google_auth_oauthlib.flow --noconfirm
```

The executable will be created at `dist/InterviewAssistant.exe`.

> **Note:** The `.env` and `client_secret.json` files are bundled into the EXE automatically via the spec file.

---

## 🔌 Supported LLM Providers

| Provider | Model                    | Status        |
|----------|--------------------------|---------------|
| Groq     | Llama 4 Scout 17B        | ✅ Active     |
| OpenAI   | GPT-4o                   | 🔜 Coming Soon |
| Claude   | Claude 3.5 Sonnet        | 🔜 Coming Soon |

To add a new provider in the future, implement the `LLMProvider` base class in `llm_service.py`.

---

## 🔒 Security Notes

- API keys are stored locally in `%APPDATA%/InterviewAssistant/api_keys.json`.
- Google OAuth tokens are stored in `%APPDATA%/InterviewAssistant/google_token.json`.
- Neither `.env` nor `client_secret.json` should be committed to version control.

---

## 📄 License

This project is for personal/educational use.
