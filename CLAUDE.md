# JA.S.Mine — instructions for Claude Code

A personal AI assistant that runs full-screen on a Windows laptop: one page with a map of rooms,
each room a single job, on three layers built on Claude: **Sky** (facts and tools), **Socrate**
(questions your reasoning) and **Ramana** (the spirit of Ramana Maharshi's teaching).
Code comments and the interface are in Romanian; keep new ones in Romanian.
The reasons behind the rules below are in [docs/DESIGN.md](docs/DESIGN.md).

## Names

- **Two forms, on purpose.** `JA.S.Mine` where the name is *read*: titles, interface, messages.
  `Jasmine` where it is *typed*: paths, file names, code.
- **layer** (`strat`) — Sky, Socrate, Ramana: what the bridge asks for and what is saved in
  conversations.
- **model** — the id in Open WebUI (`sky`, `-socrate`). It lives in `straturi.json` and in the
  server; the bridge only shows it and sends the layer, not the model.
- **voice** (`voce`) — reserved for audio (dictation), never used for layers.

## Map

| file | what it holds |
|---|---|
| `server/server.py` | the server (FastAPI): the rooms' data, the tools, the personas |
| `punte.html` | the bridge: the single page of the interface |
| `server/mail.py`, `server/stiri.py` | COMMS (mail report) and SENSORS (news) |
| `persona/` | the layers' prompts and `straturi.json` |
| `date/proiecte.json` | the project list shown on the bridge |

Open WebUI is the conversation engine; [Agent 051](https://github.com/Mihai-Streanga/Agent051-public)
is a separate program, asked over MCP.

## Commands

```bat
.venv\Scripts\python.exe tests\test_conversatii.py
.venv\Scripts\python.exe tests\test_mail.py
.venv\Scripts\python.exe tests\test_securitate.py
.venv\Scripts\python.exe tests\test_stiri.py
.venv\Scripts\python.exe tests\test_recon.py
```

The first four need no keys, no network and no running server. `test_recon.py` checks the
contract with Agent 051 and is skipped, with its reason, when the agent is not running.

## Rules

- **Functions are code; the model phrases.** Mail, news, the log and project status are read by
  deterministic code; the model receives the result and, at most, puts it into words. Every
  function has two doors: its room on the map (zero tokens) and the chat (the model calls the
  tool).
- **Independent layers.** Sky, Socrate and Ramana share nothing: each has its own prompt and its
  own chapter of the profile. Do not add a channel between them.
- **Agents are separate programs.** JA.S.Mine asks Agent 051 over MCP and never copies its data.
- **It answers only at home.** The server listens on `127.0.0.1` and refuses any request whose
  `Host` or `Origin` is not its own; `test_securitate.py` proves it. Links from outside feeds
  become links only when they are `http(s)`.
- **Nothing is lost on a crash.** Conversations and the log are written to a temporary file and
  swapped in with `os.replace`.
- **A dark room always says why.** Each back end reports whether it is alive, where it was looked
  for and why it failed. Every warning must be able to reach zero.
- **One source of truth, in readable files.** Persona prompts, settings and the model behind
  each layer live in files on disk, not in a user interface.
- **Dictation for the whole system stays out of JA.S.Mine**: it lives in
  [Iris](https://github.com/Mihai-Streanga/Iris-public).
