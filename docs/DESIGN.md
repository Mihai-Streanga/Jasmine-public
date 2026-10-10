# JA.S.Mine — design decisions

This document explains how JA.S.Mine is built and why: the principles behind the code and the
decisions that were closed during development, each with its reason. Many of them reverse an
earlier choice; where they do, the reason for the reversal is given. The private repository
keeps the full record, with dates and measurements; this is the part that shapes the code
published here.

## 1. Principles

**Functions are code; the model phrases.** Every function (mail report, news, the log, project
status, the job briefing) is deterministic code that reads its sources and returns data. The
model receives the result and, at most, puts it into words. If the model states a figure it was
not given by the code, the figure is invented. So every function has two doors: the room on the
map (deterministic, zero tokens, the person commands) and the chat (the model chooses the tool).

**The model proposes, code executes, the person decides.** Figures and anything irreversible
belong to code and to the person.

**Standards before products.** The parts are joined only through standard interfaces: MCP for
tools, an OpenAI-compatible API for models. Open WebUI is the conversation engine and can be
replaced; the JA.S.Mine server is the face precisely so that the engine can change without the
person noticing.

**One source of truth, in readable files.** Persona prompts, settings and the model behind each
layer live in files on disk, not in a user interface. The data (the log, conversations, reports)
lives in files too, not in the browser.

**Independent layers.** Sky, Socrate and Ramana are three layers of the same person, not three
characters, and nothing flows between them: each has its own prompt and its own chapter of the
profile, and nothing more. Whatever passes from one layer to another passes through the person.
An earlier design had facts "distilled" upwards from layer to layer; it was never built, and it
was withdrawn because what happens to a person changes how they think, and that does not
distil.

**One stage at a time.** No stage starts until the previous one is in real use. Ramana's room
was opened only once Socrate was in real use.

**Every warning can reach zero; a dark room always says why.** A warning that is always on
becomes furniture. Each back end reports whether it is alive, where it was looked for and why it
failed, and an error names its source, because each is repaired in a different place.

## 2. Architecture

- **The model is the only part off the laptop.** Without internet, JA.S.Mine opens and its
  functions work; only the voices fall silent. Local language models were tested and abandoned:
  7 tokens per second is unusable.
- **All three layers run on the same Claude model.** A cheaper model for Sky was considered and
  withdrawn: Sky calls tools, and a tool called wrongly costs an extra call and produces a false
  answer with the same confidence as a good one.
- **Long conversations belong in a flat-rate chat subscription, not here.** JA.S.Mine runs on a
  metered API.
- **Agents are separate programs.** JA.S.Mine asks [Agent 051](https://github.com/Mihai-Streanga/Agent051-public)
  over MCP and never copies its data; the agent keeps its own folder and its own port. A future
  room (AMFITEATRUL) follows the same pattern: built as its own project and connected when ready.
- **System-wide dictation is not done inside JA.S.Mine.** The server runs in the global Python,
  and any package installed for dictation would change what the server sees. It lives in a
  separate program ([Iris](https://github.com/Mihai-Streanga/Iris-public)).

## 3. Voice and dictation

- **The configuration is closed and measured.** No value changes without a new measurement, in a
  series of three.
- **The quality criterion is that the meaning reaches Sky correctly**, not word-for-word identity.
  Missing words, invented words and loops are unacceptable; capitals and punctuation are not
  counted.
- **Whisper transcribes the whole sentence, not a stream.** English terms in Romanian speech come
  out right because the model sees the full context.
- **Silence alone triggers nothing**, and the ear closes on a consequence (a sentence has ended),
  not on a property of the signal: loudness plus duration cannot tell a fan from a person.
- **One engine: OpenVINO on the integrated GPU, in fp16.** It beats whisper.cpp on its own ground,
  short clips, so whisper.cpp left. fp16 rather than int8: on 45 short clips int8 got 2 wrong and
  fp16 got 1, for a gain of about 0.13 s per sentence.
- **faster-whisper is the fallback, loaded lazily** on the first failure, once, under a lock.
  Removed, a failure would mean no dictation that day; loaded at start-up, it would cost 1.5 GB
  every day for a day that may never come.
- **`hotwords` is required on OpenVINO, and is set on the pipeline object**, not passed to
  `generate()`, where it is silently swallowed. One inference at a time, with a lock.
- **Compiling the pipeline holds the GIL**, so the server answers nothing while it runs. It is
  compiled with a disk cache, outside the repository, and loading is requested only after the
  bridge has been drawn.
- **Missing dictation does not stop the server.** A microphone must not take the office down.
- **Socrate gets no ear.** In his room, one writes.

## 4. The two words

- **The vocabulary is two words, and nothing else to remember.** More command words would be
  command words nobody remembers; any proposal for a third word or a new key is measured against
  that first.
- **Each word has two doors:** "engage" or "start" to wake, "disengage" or "stop" to sleep. A
  second door to the same room, not a third word.
- **Spoken, both are looked for at the end of an utterance.** "Anywhere in the sentence" failed in
  use: the word in the middle of a sentence that was talking *about* it swallowed the sentence.
- **Tolerance to transcription errors is asymmetric.** Sleeping tolerates two errors; waking
  exactly one, on a whole word. Measured on 1,961 utterances: at distance 1, three missed wakes
  and no false ones; at 2, three false wakes; at 3, twenty-one. A missed sleep costs one paid call;
  a false wake costs more.
- **The sleep word does not reach Sky; what was said before it does.** Otherwise the last thing
  said to Sky would be the order to stop, and paid for. Under two words, the rest is filler and is
  not sent.
- **Typed, the whole message must match exactly, and only in Sky's room.** "what does disengage
  mean?" must reach Sky; in Socrate's room "stop" is his brake.
- **Leaving the command room ends the thread.** The thread stays on disk and can be reopened;
  only the automatic continuation stops. Before, nothing ended a thread, and a live thread of an
  hour came to cost as much as a day.
- **The microphone stays open while JA.S.Mine is open**, with one exception, MUNTELE. Audio is
  local and costs nothing; the price, written down and still to be learned in use, is that while
  the command room is engaged, anything said in the room reaches Sky.
- **Better a lost utterance than a clipped one.** One is repeated; the other makes the tool seem
  deaf.
- **Loops are cut after the command words, and only on the way to Sky.** Of 168 loops on disk,
  four carried the command itself, so a filter earlier in the chain would have killed a wake to
  save a call.

## 5. The bridge

- **`Escape` minimizes; it does not close.** One press, not two: a ladder that cancels the
  sending, then closes the panel, then minimizes.
- **The window closes politely (`WM_CLOSE`), not by force.** Force leaves Chrome marked as crashed,
  and it then restores into a normal window instead of kiosk mode.
- **Processes are found by their command line, never by image name.** If the filter finds nothing,
  nothing is killed; there is no fallback to "kill all of Chrome".
- **The bridge is never cached**, and the kiosk cache is cleared at every start.
- **The bridge asks for nothing from outside to draw itself.** The fonts are served by the local
  server: a link to an external font service blocked the first paint for 2.0 s at every opening.
- **The screen is paid for before the ear.** The window opens cold and dictation loads in the
  background; otherwise the screen stayed empty for 15.3 s. "Loading" is never shown as "failed".
- **Exit stops the window, the conversation engine and the server**, the server last, and each
  exit writes what happened to it. A log that cannot be written does not block the door.
- **Chrome profile settings, including the microphone permission, are written by
  `server/pregateste-chrome.py`**, not on the command line: the profile is not in git, so a setting
  placed there would vanish silently on a restore. Nothing is written under a running Chrome.
- **The × on a log entry or on a conversation removes it at once, without confirmation**,
  because nothing is thrown away: the entry goes to an archive file, the thread to an archive
  folder. A confirmation is asked only where an action cannot be undone. The archive is written
  before the rewrite; if it cannot be, the deletion is refused.
- **The manual lives in the panels on the walls**, read like a conversation but with no input
  field: it is the deterministic door, at zero tokens. Nothing in it is typed by hand that the
  server can report: ports, models and states are read live.
- **A room's colour says, beforehand, whether it can be used.** The command room stays cold until
  Sky can answer and the ear is armed; each signal has its own probe, under a second.
- **JA.S.Mine is not launched over an open one.** A second Chrome launch on the same profile
  opens a second window in the same process, which means two ears and two clips; the launcher
  raises the existing window instead.

## 6. The rooms

**MUNTELE (Ramana).** The ear is silent while the room is open, because the room makes sound and
a microphone that hears its own speaker transcribes music. The clips are files served by the
server, not an embedded video site, which would need internet and bring ads into a room made for
quiet. They loop from the first at every opening, and stop on exit. The volume is saved in
`server/munte.json`, not in the browser, whose cache is cleared at every start.

**COMMS (mail).** Read, never send, not even as a parameter. Gmail stays untouched: `readonly`
and `BODY.PEEK`, two guarantees, not one. The mail reader is ported from Agent 051, not imported,
so that each can die without the other. The marker moves by itself at each report, which is safe
only because nothing can be lost silently: the mail stays in Gmail and the report on disk. There is
no deterministic test for "advertising" in this mailbox (four were measured, all failed), so Sky
judges it, at no extra call.

**SENSORS (news).** A source is judged by the date of its newest item, not by its response code:
some RSS feeds are dead and still answer 200 with valid XML, which is why one source is read from
its news sitemap. The model sees the whole catalogue and the code does not choose the news. The
catalogue carries no links; the link, the costly part, is paid only for what reaches the screen.
Code deduplicates by link, Sky by meaning. Sources and caps are in `server/stiri.json`.

**RECON (Agent 051).** The latest run is shown on top and all jobs below, always both: a tool that
finds and does not show is worse than one that does not find, because the second one gets
repaired. "Busy" is a fourth state: a timeout on a port that accepts connections is not "not
responding", which would send the person to restart a process that is working. One run at a time,
with the lock on the server; a second request is shown as "running for N seconds". "New" means
something was learned, not that something arrived, so the bridge does not deduplicate by ID, and
how incomplete the briefing may be is shown in figures, and only when it is.

## 7. The server and the personas

- **Prompts are written in `persona/*.md`, never in Open WebUI.** The server pushes them at
  start-up; anything written by hand in the interface is replaced, but not silently: the old copy
  is saved first.
- **A tool's description says when to call it; how to present the result travels with the result**
  (the `instructiuni` field). The schema is paid at every message, the result only when used.
- **One tool, not one to read and one to write**, and no `write: true/false` parameter: a switch
  set by the model is a decision moved from the person to the model.
- **The profile is cut by layer**, one chapter each, called with `{{dosar:capitol}}`, and nothing
  else runs. No retrieval: separating sources from extracts does the same with no code.
- **Lines starting with `>` never enter any prompt.** Evidence and sources stay there, and the
  cut is kept by code, not by discipline.
- **Every tool declares its server**, so an error always names it; an error without a name gets
  one invented by the model.
- **The local server is never said to be "not responding"**: it is the one answering. The chat
  separates three causes, because they are repaired in three places.
- **The probes are real calls, not open-port checks**, capped and run in parallel.

## 8. The machine

- **The paging file must exist, fixed at 16 GB.** Disabled, the commit limit equals the RAM, and a
  third Whisper instance failed to allocate with 15.4 GB free. "System managed" grows lazily and
  can fail before it grows.
- **WSL is capped** (`memory=6GB` in `.wslconfig`); without it, Docker's virtual machine commits up
  to half the RAM.
- **A request to a closed local port costs 2.0 s on this machine**, so the launcher first asks
  whether anyone is listening, with `netstat`.
- **Any folder move ends with a check of every container's mounts.** Docker does not fail on a
  missing source folder; it recreates it empty.

## 9. What is not in this repository

The private repository also keeps the profile and the transcripts the personas are built from,
the measurement archive, the log of lessons with their dates and the decisions about the
development environment. They are left out on purpose; the decisions above are the ones the
published code follows.
