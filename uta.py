#!/usr/bin/env python3
"""
Uta — Terminal AI coding agent

Key features:
- Agent loop with tool calls
- File ops: read, write, edit (with diff), glob, grep
- Bash sandbox (Docker) with direct fallback
- Web search & fetch
- Git integration (status, diff, commit, log)
- Permission system (ask/allow/deny modes)
- Sub-agent spawning with full context passing
- Multi-tool call chaining (one response → multiple tools)
- Auto-compact conversation history with summaries
- Cost tracking & token usage per session
- Read file at range (offset + limit) for large files

Usage:
  export DEEPSEEK_API_KEY=sk-xxx
  uta "Fix bugs in main.py"
  uta -i  (interactive mode)
"""

import os, sys, json, subprocess, tempfile, re, argparse, time, textwrap
from pathlib import Path

# Auto-deps
for pkg in ["openai", "requests"]:
    try: __import__(pkg)
    except: subprocess.run([sys.executable, "-m", "pip", "install", pkg, "-q", "--break-system-packages"],
                      capture_output=True)

from openai import OpenAI

CONFIG_DIR = Path.home() / ".uta"
CONFIG_DIR.mkdir(exist_ok=True)

DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY")
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")


# ── Cost Tracking ──

MODEL_COSTS = {
    "deepseek-chat": {"input": 0.27, "output": 1.10},    # per 1M tokens
    "deepseek/deepseek-chat": {"input": 0.27, "output": 1.10},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.150, "output": 0.600},
    "sonnet-4": {"input": 3.00, "output": 15.00},
    "default": {"input": 0.50, "output": 1.50},
}

class CostTracker:
    def __init__(self, model: str):
        self.model = model
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_cost = 0.0
        self.start_time = time.time()

    def add_usage(self, prompt: int, completion: int):
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        costs = MODEL_COSTS.get(self.model, MODEL_COSTS["default"])
        self.total_cost += (prompt / 1_000_000 * costs["input"]) + \
                          (completion / 1_000_000 * costs["output"])

    def summary(self) -> str:
        elapsed = time.time() - self.start_time
        mins, secs = divmod(int(elapsed), 60)
        return (
            f"📊 Tokens: {self.prompt_tokens:,} in / {self.completion_tokens:,} out | "
            f"Cost: ${self.total_cost:.4f} | "
            f"Time: {mins}m {secs}s"
        )

    def reset(self):
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_cost = 0.0
        self.start_time = time.time()


# ── Permission System ──

class Perm:
    def __init__(self):
        self.allowed = set()
        self.denied = set()
        self.mode = os.environ.get("UTA_MODE", "ask")

    def check(self, cmd: str, level: str = "medium") -> bool:
        if self.mode == "allow" or cmd in self.allowed: return True
        if self.mode == "deny" or cmd in self.denied: return False
        icons = {"safe": "📖", "medium": "⚠️", "dangerous": "🚨"}
        print(f"\n{icons.get(level, '⚠️')} [{level}] {cmd[:120]}")
        print("  [a]llow | [A]llow always | [d]eny | [D]eny always | s[k]ip")
        c = input("  → ").strip().lower()
        if c == "A": self.allowed.add(cmd); return True
        if c == "a": return True
        if c == "D": self.denied.add(cmd); return False
        return False


# ── Bash Sandbox ──

class BashBox:
    def __init__(self):
        self.use_docker = subprocess.run(["docker", "ps"],
            capture_output=True, timeout=3).returncode == 0

    def run(self, cmd: str, timeout: int = 120) -> str:
        if self.use_docker:
            out = self._docker(cmd, timeout)
            if "<stdout>" in out: return out
        return self._direct(cmd, timeout)

    def _direct(self, cmd: str, timeout: int) -> str:
        r = subprocess.run(["/bin/bash", "-c", cmd],
            capture_output=True, text=True, timeout=timeout)
        parts = []
        if r.stdout: parts.append(r.stdout[:8000])
        if r.stderr: parts.append(f"[stderr]\n{r.stderr[:3000]}")
        out = "\n".join(parts).strip() or "(empty)"
        if r.returncode != 0:
            out += f"\n[exit: {r.returncode}]"
        return out.replace("<stdout>", "[stdout]")

    def _docker(self, cmd: str, timeout: int) -> str:
        sf = tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False)
        sf.write("#!/bin/bash\nset -e\n"); sf.write(cmd); sf.close()
        os.chmod(sf.name, 0o755)
        try:
            r = subprocess.run([
                "docker", "run", "--rm",
                "-v", f"{sf.name}:/script.sh:ro",
                "-v", f"{Path.cwd()}:/workspace",
                "-w", "/workspace",
                "python:3.11-slim", "bash", "/script.sh"
            ], capture_output=True, text=True, timeout=timeout)
            out = ""
            if r.stdout: out += r.stdout[:8000]
            if r.stderr: out += f"\n[stderr]\n{r.stderr[:3000]}"
            if r.returncode != 0: out += f"\n[exit: {r.returncode}]"
            return out.strip() or "(empty)"
        finally:
            os.unlink(sf.name)


# ── Tools ──

class Tools:
    def __init__(self, cwd: Path, perm: Perm, model: str = "deepseek-chat"):
        self.cwd = cwd
        self.perm = perm
        self.box = BashBox()
        self.model = model

    def bash(self, cmd: str) -> str:
        if not self.perm.check(cmd, "dangerous"): return "Permission denied"
        return self.box.run(cmd)

    def read(self, p: str, offset: int = 0, limit: int = 0) -> str:
        """Read file with optional offset (0-indexed lines) and limit.
           offset=0, limit=0 reads the whole file (with size check)."""
        fp = self._p(p)
        if not fp.exists(): return f"NOT FOUND: {p}"
        if fp.is_dir(): return f"IS DIR: {p}\n" + "\n".join(str(x.name) for x in fp.iterdir())[:1000]

        # Read with encoding detection
        for enc in ['utf-8', 'latin-1', 'cp1252']:
            try:
                content = fp.read_text(encoding=enc)
                break
            except: continue
        else:
            return "(binary)"

        # If range requested
        if offset > 0 or limit > 0:
            lines = content.splitlines(keepends=True)
            start = offset
            end = (offset + limit) if limit > 0 else len(lines)
            selected = lines[start:end]
            result = "".join(selected)
            meta = f"(lines {start}-{min(end, len(lines))-1} of {len(lines)})"
            return f"{meta}\n{result}"

        # Full file: check size
        if fp.stat().st_size > 200_000:
            return f"(file too large: {fp.stat().st_size//1024}KB — use read with offset/limit)"
        return content

    def write(self, p: str, content: str) -> str:
        fp = self._p(p)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)}b → {self._rel(fp)}"

    def edit(self, p: str, old: str, new: str) -> str:
        fp = self._p(p)
        if not fp.exists(): return f"NOT FOUND: {p}"
        content = fp.read_text(encoding='utf-8', errors='replace')
        if old not in content:
            import difflib
            close = difflib.get_close_matches(old, content.splitlines(), n=3, cutoff=0.6)
            return f"Text not found. Closest lines:\n" + "\n".join(close) if close else "Text not found."
        fp.write_text(content.replace(old, new, 1))
        return f"Edited {self._rel(fp)}"

    def grep(self, pat: str, path: str = ".") -> str:
        sp = self._p(path)
        for cmd in [
            ["rg", "-n", "--color=never", pat, str(sp)],
            ["grep", "-rn", "--color=never", pat, str(sp)],
        ]:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if r.returncode == 0:
                    out = r.stdout[:3000]
                    return f"{out.count(chr(10))} matches:\n{out}"
                if r.returncode == 1: return "No matches"
            except: continue
        return "grep/rg not available"

    def glob(self, pat: str) -> str:
        ms = list(self.cwd.glob(pat))
        if not ms: return f"No files matching: {pat}"
        lines = [str(self._rel(m)) for m in ms[:60]]
        if len(ms) > 60: lines.append(f"... +{len(ms)-60} more")
        return "\n".join(lines)

    def web_search(self, q: str) -> str:
        try:
            import requests
            r = requests.get(
                f"https://html.duckduckgo.com/html/?q={requests.utils.quote(q)}",
                headers={"User-Agent": "UTA/1.0"}, timeout=10
            )
            txt = re.sub(r'<[^>]+>', ' ', r.text)
            txt = re.sub(r'\s+', ' ', txt)[:3000]
            return txt
        except Exception as e:
            return f"search error: {e}"

    def web_fetch(self, url: str) -> str:
        try:
            import requests
            r = requests.get(url, timeout=30,
                headers={"User-Agent": "Mozilla/5.0"})
            txt = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', r.text, 0, re.DOTALL)
            txt = re.sub(r'<[^>]+>', '\n', txt)
            txt = "\n".join(l.strip() for l in txt.split('\n') if l.strip())
            return txt[:8000]
        except Exception as e:
            return f"fetch error: {e}"

    def git_status(self) -> str:
        try:
            r = subprocess.run(["git", "status", "--short"],
                capture_output=True, text=True, timeout=10, cwd=self.cwd)
            if r.returncode != 0: return "Not a git repo"
            return r.stdout.strip() or "Clean working tree"
        except: return "git unavailable"

    def git_diff(self) -> str:
        try:
            r = subprocess.run(["git", "diff"],
                capture_output=True, text=True, timeout=10, cwd=self.cwd)
            return r.stdout[:3000] or "No changes"
        except: return "git unavailable"

    def git_log(self, n: int = 10) -> str:
        try:
            r = subprocess.run(
                ["git", "log", f"--max-count={n}", "--oneline", "--graph"],
                capture_output=True, text=True, timeout=10, cwd=self.cwd)
            return r.stdout.strip() or "No commits"
        except: return "git unavailable"

    def agent(self, task: str) -> str:
        """Spawn a sub-agent with the given task. Uses --quiet to minimize output."""
        r = subprocess.run(
            [sys.executable, __file__, "--quiet", "--mode", "allow", task],
            capture_output=True, text=True, timeout=300)
        out = ""
        if r.stdout: out += r.stdout[:5000]
        if r.stderr: out += f"\n[stderr]\n{r.stderr[:2000]}"
        if r.returncode != 0:
            out += f"\n[exit: {r.returncode}]"
        return out.strip() or "(completed silently)"

    def _p(self, p: str) -> Path:
        pt = Path(p)
        return pt if pt.is_absolute() else (self.cwd / pt)

    def _rel(self, p: Path) -> str:
        try: return str(p.relative_to(self.cwd))
        except: return str(p)


# ── Agent Loop ──

class Agent:
    def __init__(self, cwd: Path, mode: str = "ask"):
        self.cwd = cwd
        self.perm = Perm()
        if mode: self.perm.mode = mode
        self.cost = CostTracker("deepseek-chat")
        self.tools = Tools(cwd, self.perm)
        self.msg = []
        self.max_turns = int(os.environ.get("MAX_TOOL_TURNS", "50"))
        self.turn_count = 0
        self.client, self.model = self._init_model()

    def _init_model(self):
        if DEEPSEEK_KEY:
            return OpenAI(api_key=DEEPSEEK_KEY,
                base_url="https://api.deepseek.com/v1"), "deepseek-chat"
        if OPENROUTER_KEY:
            return OpenAI(api_key=OPENROUTER_KEY,
                base_url="https://openrouter.ai/api/v1"), "deepseek/deepseek-chat"
        try:
            c = OpenAI(api_key="x", base_url="http://localhost:11434/v1")
            c.models.list()
            return c, "llama3"
        except:
            print("❌ No API key. Set DEEPSEEK_API_KEY or OPENROUTER_API_KEY")
            sys.exit(1)

    def system_prompt(self) -> str:
        return f"""You are a coding agent running at {self.cwd}.

TOOLS:
- bash("command") — Run shell commands
- read("path", offset=0, limit=0) — Read a file (optional line offset/limit)
- write("path", "content") — Write a file
- edit("path", "old_text", "new_text") — Edit by replacement
- grep("pattern", "path") — Search files
- glob("pattern") — Find files by glob
- web_search("query") — Web search
- web_fetch("url") — Fetch a URL
- git_status() — Git status
- git_diff() — Uncommitted changes
- git_log(n=10) — Recent commits
- agent("task") — Spawn a sub-agent for a subtask

IMPORTANT — Multiple tool calls:
You can call MULTIPLE tools in one response. Put each on its own line:
TOOL: read("file1.py")
TOOL: read("file2.py")

They will be executed in order and all results returned.

RULES:
- Read files before modifying them
- Verify results after tool calls
- When done, end with: DONE: <summary>
- Tool format: TOOL: funcName("arg1", "arg2")
- Use read with offset/limit for large files

Example:
TOOL: read("main.py")
TOOL: grep("def ", "src/")
TOOL: bash("python3 main.py")"""

    def run(self, task: str) -> str:
        print(f"\n{'='*50}\n  Uta — {self.cwd.name}\n  Model: {self.model}\n  {task[:80]}\n{'='*50}")

        self.cost.reset()
        self.turn_count = 0
        self.msg = [
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": task}
        ]

        compact_summaries = []

        for turn in range(self.max_turns):
            self.turn_count = turn + 1
            print(f"\n── Turn {turn+1}/{self.max_turns} ──")

            try:
                r = self.client.chat.completions.create(
                    model=self.model, messages=self.msg,
                    temperature=0.3, max_tokens=4096, timeout=120)
                resp = r.choices[0].message.content

                # Track costs
                if hasattr(r, 'usage') and r.usage:
                    pt = r.usage.prompt_tokens or 0
                    ct = r.usage.completion_tokens or 0
                    self.cost.add_usage(pt, ct)
            except Exception as e:
                return f"API error: {e}"

            if not resp: continue
            print(resp)

            if "DONE:" in resp.upper():
                m = re.search(r'DONE:\s*(.*)', resp, re.DOTALL | re.IGNORECASE)
                result = m.group(1).strip() if m else resp
                cost_line = self.cost.summary()
                print(f"\n{cost_line}")
                return result

            results = self._do_tools(resp)
            if not results:
                if turn == 0 and len(resp) < 10:
                    self.msg.append({"role": "assistant", "content": resp})
                    cost_line = self.cost.summary()
                    return f"{resp}\n{cost_line}"
                self.msg.append({"role": "assistant", "content": resp})
                self.msg.append({"role": "user", "content": "Continue. Use tools. Say DONE when finished."})
            else:
                combined = ""
                for i, (tool_str, tool_result) in enumerate(results):
                    rtxt = tool_result[:1500] + ("..." if len(tool_result) > 1500 else "")
                    combined += f"\n<tool_{i+1}> {tool_str}\n{rtxt}\n</tool_{i+1}>"
                combined = combined.strip()
                print(f"\n📎 {combined[:2000]}")

                self.msg.append({"role": "assistant", "content": resp})
                self.msg.append({"role": "user", "content": combined})

                # Smart reactive compaction: if messages > 120K chars, compact
                total_chars = sum(len(m.get("content","")) for m in self.msg)
                if total_chars > 120_000:
                    summary = self._smart_compact()
                    compact_summaries.append(summary)
                elif total_chars > 60_000 and turn > 5 and turn % 5 == 0:
                    summary = self._smart_compact()
                    compact_summaries.append(summary)

        cost_line = self.cost.summary()
        print(f"\n{cost_line}")
        return f"Max turns reached.\n{cost_line}"

    def _do_tools(self, resp: str):
        """Find ALL tool calls in the response, execute them in order.
        Returns list of (tool_str, result) tuples, or empty list if no tools found."""
        # Match all TOOL: funcName("arg1", ...) patterns
        tool_pattern = re.compile(
            r'TOOL:\s*(\w+)\(([\s\S]*?)\)\s*(?=$|\nTOOL:)',
            re.IGNORECASE | re.MULTILINE
        )

        # First try: find all TOOL: lines explicitly
        matches = list(tool_pattern.finditer(resp))
        if not matches:
            # Try alternates: lines starting with specific tool keywords (for freeform text)
            return []

        results = []
        for m in matches:
            name = m.group(1).lower()
            args = m.group(2).strip()
            parsed = self._parse_args(args)
            tool_str = f"{name}({', '.join(p[:60] for p in parsed)})"
            result = self._call_tool(name, parsed, args)
            results.append((tool_str, result))

        return results

    def _call_tool(self, name: str, parsed: list, raw_args: str) -> str:
        tool_map = {
            "bash": lambda: self.tools.bash(parsed[0] if parsed else raw_args),
            "read": lambda: self._read_with_range(parsed),
            "write": lambda: self.tools.write(parsed[0], parsed[1] if len(parsed) > 1 else ""),
            "edit": lambda: self.tools.edit(*parsed[:3]),
            "grep": lambda: self.tools.grep(parsed[0], parsed[1] if len(parsed) > 1 else "."),
            "glob": lambda: self.tools.glob(parsed[0] if parsed else raw_args),
            "web_search": lambda: self.tools.web_search(parsed[0] if parsed else raw_args),
            "search": lambda: self.tools.web_search(parsed[0] if parsed else raw_args),
            "web_fetch": lambda: self.tools.web_fetch(parsed[0] if parsed else raw_args),
            "fetch": lambda: self.tools.web_fetch(parsed[0] if parsed else raw_args),
            "git_status": lambda: self.tools.git_status(),
            "git_diff": lambda: self.tools.git_diff(),
            "git_log": lambda: self.tools.git_log(int(parsed[0]) if parsed else 10),
            "agent": lambda: self.tools.agent(parsed[0] if parsed else raw_args),
        }

        fn = tool_map.get(name)
        if not fn: return f"Unknown tool: {name}"
        try:
            r = fn()
            return str(r) if not isinstance(r, str) else r[:3000]
        except Exception as e:
            return f"Tool error ({name}): {e}"

    def _read_with_range(self, parsed):
        """Handle read(path) and read(path, offset, limit) signatures."""
        path = parsed[0] if parsed else ""
        offset = int(parsed[1]) if len(parsed) > 1 and parsed[1] else 0
        limit = int(parsed[2]) if len(parsed) > 2 and parsed[2] else 0
        return self.tools.read(path, offset, limit)

    def _parse_args(self, s: str):
        """Parse comma-separated quoted arguments, preserving escape sequences."""
        args = []
        i = 0
        cur = ""
        in_q = None
        while i < len(s):
            c = s[i]
            if in_q:
                if c == in_q:
                    args.append(cur)
                    cur = ""
                    in_q = None
                elif c == '\\' and i+1 < len(s):
                    nxt = s[i+1]
                    if nxt in ('n', 't', 'r', '0', '"', "'", '\\'):
                        cur += '\\' + nxt
                        i += 1
                    else:
                        cur += c
                else:
                    cur += c
            elif c in ('"', "'"):
                in_q = c
            elif c == ',':
                if cur.strip():
                    args.append(cur.strip())
                cur = ""
                # Skip whitespace after comma
                while i+1 < len(s) and s[i+1] in (' ', '\t'):
                    i += 1
            else:
                cur += c
            i += 1
        if cur.strip():
            args.append(cur.strip())
        # Decode escape sequences
        result = []
        for a in args:
            try:
                decoded = a.encode('utf-8').decode('unicode_escape')
                result.append(decoded)
            except:
                result.append(a)
        return result

    def _smart_compact(self):
        """Reactive compaction: keep system + user task, recent messages,
        but also generate a compact summary of what was done."""
        # Find DONE lines and key actions from previous messages
        key_actions = []
        for m in self.msg[2:]:  # Skip system + initial user
            c = m.get("content", "")
            if m["role"] == "assistant":
                # Extract tool calls mentioned
                tools_used = re.findall(r'TOOL:\s*(\w+)', c, re.IGNORECASE)
                if tools_used:
                    key_actions.append(f"[{m['role']}] used: {', '.join(tools_used[:3])}")
                # Extract any DONE-like summaries
                done_m = re.search(r'DONE[:\.]?\s*(.*)', c, re.IGNORECASE | re.DOTALL)
                if done_m:
                    key_actions.append(f"[summary] {done_m.group(1).strip()[:100]}")
            elif m["role"] == "user":
                # Tool results — just note the count
                tool_count = len(re.findall(r'<tool_\d+>', c))
                if tool_count:
                    key_actions.append(f"[result] {tool_count} tool results returned")

        summary_text = "; ".join(key_actions[-10:]) if key_actions else "continued working"

        # Keep: system, user task, last 6 exchanges
        kept = self.msg[:2] + self.msg[-12:]
        kept.insert(1, {"role": "system",
            "content": f"(Compacted {len(self.msg)-14} messages. Summary: {summary_text})"})
        self.msg = kept
        return summary_text


# ── CLI ──

def main():
    p = argparse.ArgumentParser(description="Uta")
    p.add_argument("task", nargs="*")
    p.add_argument("--cwd", "-C")
    p.add_argument("--mode", "-m", choices=["ask", "allow", "deny"])
    p.add_argument("--interactive", "-i", action="store_true")
    p.add_argument("--quiet", "-q", action="store_true")
    p.add_argument("--version", "-V", action="store_true")
    a = p.parse_args()

    if a.version: print("Uta v2.0"); return

    cwd = Path(a.cwd).resolve() if a.cwd else Path.cwd()
    mode = a.mode or os.environ.get("UTA_MODE", "ask")

    if a.quiet or a.interactive:
        mode = "allow"

    ag = Agent(cwd=cwd, mode=mode)

    if a.interactive:
        print(f"💻 Uta — {cwd}")
        while True:
            try:
                t = input("\n🎯 > ").strip()
                if t.lower() in ("quit", "exit", "q"): break
                if t: print(f"\n✅ {ag.run(t)}")
            except KeyboardInterrupt:
                print("\n👋"); break
    elif a.task:
        r = ag.run(" ".join(a.task))
        print(f"\n✅ {r}" if not a.quiet else r)
    else:
        p.print_help()

if __name__ == "__main__":
    main()
