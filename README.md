# OMI FIELD//MEMORY Relay

Private Omi mini-app backend for one person. It keeps a source-backed archive and has two layers inside the official Omi iPhone app:

- **Answer Radar** receives live transcript segments, learns a reversible communication-style profile for named speakers, and returns one compact proactive-notification prompt when another person asks the wearer a question;
- completed conversations are kept as source segments and exposed through four chat tools:

- search the captured archive;
- compile a source-backed dossier/timeline;
- prepare an Obsidian Markdown draft (without automatically writing it anywhere);
- show capture status.

It is not an always-listening tracker, does not read Omi tasks, and does not request access to historical conversations or memories. Communication profiles are observations with confidence, not character diagnoses; they can be deleted with the source archive.

## Run locally

```powershell
Set-Location C:\Users\repki\Documents\ChatGPT\omi
$env:OMI_BRIDGE_SECRET = "generate-a-long-random-secret-before-use"
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8787
```

Open `http://127.0.0.1:8787/health`. Omi itself needs a public HTTPS address, so do not put `localhost` into the iPhone app.

## Production boundary

Copy `.env.example` into the host's secret store; never commit it. Configure a persistent disk at `/data` before receiving a real conversation. A container with an ephemeral disk is not an archive.

`OMI_BRIDGE_SECRET` is a bearer secret placed only in the private Omi webhook URL. The first valid webhook pairs the relay to that Omi account; set `OMI_BRIDGE_ALLOWED_UID` in advance if the UID is known. The public service hides all `/api/*` archive routes unless a separate `X-Omi-Bridge-Key` matches `OMI_BRIDGE_DASHBOARD_KEY`.

## Official Omi App fields

Fill these only after the relay has a real HTTPS root such as `https://relay.example.com`:

| Omi field | Value |
| --- | --- |
| Trigger Event | `Transcript Processed` |
| Webhook URL | `https://relay.example.com/api/webhooks/omi/<OMI_BRIDGE_SECRET>/live` |
| App Home URL | `https://relay.example.com/` |
| Setup Completed URL | `https://relay.example.com/setup?uid={{uid}}` |
| Chat Tools Manifest URL | `https://relay.example.com/.well-known/omi-tools.json` |
| Auth URL | leave empty |
| External-data scopes | all off |
| Notification scopes | User name, User data, User conversations |

Use `Chat`, `Conversations`, `External Integration`, and `Notifications` capabilities. The app stays private. Omi limits proactive notifications, so Answer Radar deliberately sends only one likely addressed question at a time.

### Answer Radar test

1. Deploy the same repository to a private HTTPS endpoint and configure the fields above.
2. Enable the OMI FIELD mini-app in Omi.
3. Ask a named second speaker to say one direct question, then wait at least 30 seconds before repeating a test.
4. Omi should receive a compact suggested reply. The server stores the incoming source segment and its non-diagnostic style observations under that speaker name.

### Chat prompt

```text
You are OMI FIELD//MEMORY, a concise command layer over the user's own captured archive.
For questions about prior conversations, people, projects, decisions, links, or timelines, call the available FIELD tools first. Present only source-grounded claims and show session IDs. Clearly say when the archive has no source. Do not invent memories, give generic coaching, create tasks, or send anything externally without an explicit request.
```

### Conversation prompt

```text
Create a compact, durable field record from this conversation. Preserve exact facts, decisions, names, projects, places, links, numbers, open questions, and explicit next actions. Separate confirmed facts from hypotheses or unclear statements. Do not invent missing details. Keep each item traceable to the conversation and omit filler or routine small talk.
```

## Checks

```powershell
python -m pytest tests -q
python -m compileall -q app.py
```
