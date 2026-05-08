
      ██╗   ██╗████████╗ █████╗ 
      ██║   ██║╚══██╔══╝██╔══██╗
      ██║   ██║   ██║   ███████║
      ██║   ██║   ██║   ██╔══██║
      ╚██████╔╝   ██║   ██║  ██║
       ╚═════╝    ╚═╝   ╚═╝  ╚═╝

    Terminal AI Coding Agent 🐉

---

**Uta** is a self-hosted AI coding agent that works in your terminal. Give it a task — it reads your code, writes files, runs commands, searches the web, and gets things done.

Like Claude Code, but: zero subscription, no vendor lock-in, runs on your own API keys or local models.

---

## Quick Start

```bash
# 1. Get a single file
curl -L https://github.com/yahyasaqban-lab/uta/raw/main/uta.py -o uta.py

# 2. Set your API key
export DEEPSEEK_API_KEY="sk-..."

# 3. Run it
python3 uta.py "Fix all bugs in main.py"
```

Or install as a command:

```bash
chmod +x uta.py && sudo mv uta.py /usr/local/bin/uta
uta "Refactor this codebase"
```

---

## Features

| Capability | Description |
|-----------|-------------|
| **Agent Loop** | Plans → uses tools → observes results → continues |
| **File Reading** | Read files, view specific line ranges |
| **File Writing** | Write/overwrite files with proper content |
| **Smart Edit** | Replace exact text in files (like Claude's edit tool) |
| **Bash** | Run shell commands (with permission prompts) |
| **Code Search** | `grep` and `glob` for patterns across your project |
| **Git** | Status, diff, log — works inside git repos |
| **Web Search** | DuckDuckGo search + URL fetching |
| **Sub-Agents** | Spawn child agents for parallel tasks |
| **Cost Tracking** | Token usage + estimated cost per session |

---

## Usage

### Single task
```bash
uta "Create a Flask API with 3 endpoints and test it"
```

### Interactive mode
```bash
uta -i
🎯 > Find all Python files with syntax errors
🎯 > Fix them one by one
🎯 > Run the tests
```

### Permission modes
```bash
uta "Run a script"                          # Ask before each command (default)
export UTA_MODE=allow; uta "Deploy..."      # Auto-approve all commands
export UTA_MODE=deny;  uta "Read files"     # Block all commands, read-only
```

### Change working directory
```bash
uta -C /path/to/project "Fix the bug"
```

---

## Models

Uta supports multiple backends — set through environment variables:

| Backend | Env Var | Model |
|---------|---------|-------|
| **DeepSeek** | `DEEPSEEK_API_KEY` | `deepseek-chat` (default) |
| **OpenRouter** | `OPENROUTER_API_KEY` | Any model via OpenRouter |
| **Local Ollama** | (none, auto-detect) | `llama3` (fallback) |

Uta auto-detects Ollama if it's running on `localhost:11434` and no API key is set.

---

## Why Uta?

- **$0.0004 per task** with DeepSeek (vs $0.03+ with Claude API)
- **$0 forever** with local Ollama models
- **No subscription** — no $20/month for Claude Pro or Cursor
- **No vendor lock-in** — switch models anytime
- **Single file** — one Python file, zero dependencies (auto-installs)
- **Cross-platform** — Linux, macOS, Windows

---

## How It Works

```
You: "Write a test suite for my API"
         │
         ▼
    ┌─────────────┐
    │    Model     │  ← DeepSeek / OpenRouter / Ollama
    │   (thinks)   │
    └──────┬──────┘
           │ tool call
           ▼
    ┌─────────────┐
    │   Tools     │  ← read, write, edit, bash, grep, git, web
    │  (execute)  │
    └──────┬──────┘
           │ result
           ▼
    ┌─────────────┐
    │   Model     │  ← observes, plans next step
    │  (reasons)  │
    └──────┬──────┘
           │
           ▼
       DONE: "Test suite created and passing"
```

The agent iterates through tool calls until it completes the task or hits the turn limit (50 by default).

---

## Security

Uta has a built-in permission system:

- **Safe** (read, search) — no prompt
- **Medium** (write files) — asks permission
- **Dangerous** (bash, network) — asks permission with warning

Set `UTA_MODE=allow` to skip prompts in trusted environments.

---

## License

MIT — do whatever you want with it.
