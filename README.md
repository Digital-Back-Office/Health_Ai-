# Health AI: AI voice receptionist (prototype)

A web-based AI receptionist for a UK private clinic. Patients talk (or type) to **Sophie**, who books, reschedules and cancels appointments and answers questions about the clinic.

| Part | Technology |
|---|---|
| Voice, speech-to-text, turn-taking | ElevenLabs Agents (British female voice, English only) |
| LLM | Vertex AI Gemini (`europe-west2`, London) through an OpenAI-compatible proxy, used as ElevenLabs' *Custom LLM* |
| Scheduling source of truth | PostgreSQL (double booking is prevented by an exclusion constraint) |
| Staff diary mirror + staff busy blocks | Google Calendar (one calendar per clinician) |
| Backend | Python 3.12, FastAPI, SQLAlchemy 2 (async), Alembic |
| Frontend | React 19 + Vite, `@elevenlabs/react` |
| Phone line, SMS, SOS calls | Twilio (the number is answered by the same ElevenLabs agent; browser calls use the Twilio Voice SDK) |

Phone calls, text confirmations, reminder calls and the **SOS emergency line** are optional; see [6. Phone calls, SMS and the SOS line](#6-phone-calls-sms-and-the-sos-line-twilio).

## Daily use: start everything

Once the setup steps below are done, **double-click `dev.cmd`** (or run `.\dev.ps1`). It:

1. starts Postgres in Docker
2. starts the backend (http://localhost:8000) and the frontend (http://localhost:5173), each in its own window
3. starts the ngrok tunnel, using `ngrok` if it's installed or `npx ngrok` otherwise. If the public URL has changed, it updates `PUBLIC_BASE_URL` and runs `setup-agent`, so the ElevenLabs agent always points at the right place.
4. opens the app in your browser

Options:
* `.\dev.ps1 -NoTunnel`: UI work only.
* `.\dev.ps1 -Ngrok "C:\path\to\ngrok.exe"`: use this if ngrok isn't on your PATH.

To stop, close the windows. The ElevenLabs agent itself runs in ElevenLabs' cloud; there's nothing to start for it.

Manual equivalent (three terminals):

```powershell
docker compose up -d db
cd backend; .venv\Scripts\activate; uvicorn app.main:app --port 8000 --reload --reload-dir app
cd frontend; npm run dev
npx ngrok http 8000      # then: put the https URL in PUBLIC_BASE_URL and run  python -m app.cli setup-agent
```

---

## How it works

```
Browser (React)                        Backend (FastAPI)                          External
───────────────                        ─────────────────                          ────────
1. POST /api/sessions  ───────────────► creates session + random token ──────────► ElevenLabs: conversation token
   ◄── token, conversation token,        (stores only a hash of the token)
       dynamic variables
2. connect to ElevenLabs (WebRTC) ───────────────────────────────────────────────► ElevenLabs agent "Sophie"
3. GET /api/sessions/{id}/events  ◄──── live UI events (Postgres NOTIFY → SSE)          │
                                                                                      │ needs an answer
                                        POST /v1/chat/completions  ◄──────────────────┤ (Custom LLM)
                                        └─► Vertex AI Gemini (OAuth, europe-west2)      │
                                                                                      │ calls a tool
                                        POST /tools/<name>  ◄─────────────────────────┘
                                          X-Tool-Secret: <workspace secret>
                                          X-Session-Token: {{secret__session_token}}
                                        └─► Postgres (bookings) ──► Google Calendar (mirror, async + retried)
                                        POST /webhooks/elevenlabs ◄─── post-call summary + analysis (HMAC-signed)
```

Key design points:

* **The LLM never chooses which patient it acts for.** Each browser session gets a random token. It reaches the agent as a `secret__` dynamic variable, which ElevenLabs only uses in request headers and never shows to the LLM, and it comes back on every tool call. The backend binds the verified patient to that session, so one conversation can only ever see or change its own patient's appointments.
* **No double booking, even with many simultaneous conversations.** The `appointments` table has a Postgres `EXCLUDE USING gist` constraint, so no two booked appointments can overlap for one clinician. A losing booking is told the slot was just taken and gets alternatives. This is tested with 8 concurrent sessions racing for one slot.
* **Identity checks:** surname + date of birth + the phone number on the patient's record. After 3 failed attempts the session locks and the agent offers a callback instead.
* **Google Calendar is a mirror, not the source of truth.** Bookings commit to Postgres first. The calendar event is then written after the tool has already answered, so the agent isn't slowed down. Writes are idempotent (the event id comes from the appointment id), serialised per appointment, and retried every minute if Google is unavailable. Staff "busy" blocks in Google Calendar (leave, meetings) are removed from availability. If a calendar can't be read, the system fails closed and offers a callback.
* **Gemini 3 tool calling through the proxy:** Gemini 3 rejects tool-call history that's missing its "thought signatures". The proxy remembers them and puts them back on the next turn.
* **Live UI:** tool results are published with Postgres `NOTIFY` in the same transaction, so the patient's screen shows slots, confirmations and appointments as they happen, across any number of backend workers.
* **Phone calls use the same agent and tools.** ElevenLabs answers the Twilio number and calls our call-start webhook, which creates a session for the call exactly like a browser session. SOS and transfers are handled by the backend through Twilio (a conference for SOS, `<Dial>` for reception), so the AI is never on those calls.

---

## Project layout

```
HealthCare-Ai/
├── docker-compose.yml            Postgres 18 (port 5433)
├── backend/
│   ├── app/
│   │   ├── main.py               FastAPI app (lifespan: event bus, calendar resync loop)
│   │   ├── config.py             settings (env / backend/.env)
│   │   ├── models.py             tables incl. the no-double-booking constraint
│   │   ├── api/
│   │   │   ├── tools.py          ← the agent's tools (webhooks)
│   │   │   ├── sessions.py       browser sessions, SSE events, heartbeat
│   │   │   ├── webhooks.py       ElevenLabs post-call + call-start webhooks
│   │   │   ├── twilio_webhooks.py  Twilio callbacks: SOS conference, transfers, browser calls
│   │   │   ├── admin.py          staff dashboard API
│   │   │   └── clinic.py         public clinic info
│   │   ├── llm/vertex_proxy.py   OpenAI-compatible proxy → Vertex AI
│   │   ├── services/             availability, booking, patients, calendar, events, sessions,
│   │   │                         telephony, emergency (SOS), handoff, notifications (SMS), reminders
│   │   ├── agent/
│   │   │   ├── system_prompt.md  Sophie's prompt (UK English, guardrails, 999/111)
│   │   │   ├── knowledge_base/   clinic info, policies, preparation, FAQ
│   │   │   └── setup.py          creates/updates the ElevenLabs agent
│   │   ├── seed.py               sample clinic + synthetic test patients
│   │   └── cli.py                python -m app.cli <command>
│   ├── alembic/                  migrations
│   └── tests/                    127 tests (real Postgres)
└── frontend/
    └── src/
        ├── App.tsx               patient page, staff dashboard (#/staff), dev preview (#/preview)
        ├── lib/useReceptionist.ts  session → ElevenLabs → transcript/events/heartbeat
        ├── lib/useHumanCall.ts   SOS / reception calls from the browser (Twilio Voice SDK)
        └── components/           AssistantCard, HumanCall (SOS), VoiceOrb, VisitPanel, ClinicCard,
                                  StaffDashboard, StaffOnCall
```

---

## 1. Run it locally (no external accounts needed yet)

Prerequisites: Docker Desktop, Python 3.12, Node 20+.

```powershell
# Database
docker compose up -d db

# Backend
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env          # then edit it (see below)
python -m app.cli migrate
python -m app.cli seed
uvicorn app.main:app --reload --port 8000

# Frontend (second terminal)
cd frontend
npm install
npm run dev                      # http://localhost:5173
```

Without ElevenLabs configured you can already use:
* http://localhost:5173: the patient page (starting a conversation shows "isn't configured yet")
* http://localhost:5173/#/preview: replays every live UI state with sample data (dev only, uses no voice minutes)
* http://localhost:5173/#/staff: the staff dashboard (sign in with `ADMIN_TOKEN`)
* http://localhost:8000/docs: the API, including all tool endpoints

Test patients (synthetic), confirmed by surname + date of birth + phone: **Alice Testington**, 12/04/1990, 07700 900123 · **Ben Sample**, 03/11/1985, 07700 900456 · **Chloe Example** (child), 01/06/2016, 07700 900789.

## 2. Expose the backend to ElevenLabs (tunnel)

ElevenLabs' servers call your tool webhooks and the LLM proxy, so the backend needs a public HTTPS URL. Use either:

```powershell
# ngrok (a free account gives you one fixed domain, so you only configure it once)
ngrok http 8000 --url=https://your-name.ngrok-free.app

# or Cloudflare (no account; the URL changes on every start)
winget install --id Cloudflare.cloudflared
cloudflared tunnel --url http://localhost:8000
```

Put the URL in `PUBLIC_BASE_URL` in `backend/.env`. If the URL changes, re-run `python -m app.cli setup-agent`.

Tip: run `npm run build` in `frontend/` and the backend also serves the patient page at the tunnel URL. You can then try the assistant on your phone over HTTPS.

## 3. Vertex AI (the custom LLM)

1. In your GCP project, make sure the **Vertex AI API** is enabled.
2. Credentials, using either option:
   * **Your gcloud login (simplest for local development):** run `gcloud auth application-default login` with an account that has the Vertex AI User role, and leave `VERTEX_CREDENTIALS_FILE` **empty**. The quota project saved by gcloud is sent automatically. No key file is needed.
   * **A service account (for servers):** grant it **Vertex AI User**, download a JSON key to `backend/secrets/vertex-sa.json`, and set `VERTEX_CREDENTIALS_FILE` to that path. On Google Cloud (e.g. Cloud Run), leave it empty and the attached service account is used.
3. Set `VERTEX_PROJECT_ID` (e.g. `dataflow-zone1`). `VERTEX_LOCATION=europe-west2` keeps inference in London.
4. Keep `VERTEX_REASONING_EFFORT=minimal` for voice: replies start in about 1 s. `low` adds roughly 2 s per turn.
5. Check it: `python -m app.cli check-vertex`. This tests a reply and a tool call and prints the latency. If the model isn't available in London, it suggests `VERTEX_LOCATION=global` or another `VERTEX_MODEL`. Gemini 3.5 Flash-Lite, for example, isn't offered in `europe-west2`.

Why a proxy: Vertex accepts short-lived Google OAuth tokens (1 hour), not a static API key that ElevenLabs could store. The proxy refreshes tokens automatically, pins the model and region, and keeps Gemini 3 tool calling intact.

## 4. ElevenLabs agent

1. In ElevenLabs create an **API key** (Profile → API keys, with Agents permissions) and set `ELEVENLABS_API_KEY`.
2. Set strong random values for `TOOL_SECRET`, `LLM_PROXY_SECRET` and `ADMIN_TOKEN`.
3. (Optional) choose a voice: `python -m app.cli voices search` lists young British female conversational voices with preview links. Then run `python -m app.cli voices add <owner_id> <voice_id> "Sophie"`. If you skip this, setup picks a British female voice from your library.
4. Create or update the agent: `python -m app.cli setup-agent`. This creates the workspace secrets, the 8 webhook tools, the knowledge base documents and the agent itself:
   * Custom LLM → your proxy
   * timezone Europe/London
   * English, `eleven_flash_v2` TTS
   * auth required
   * text-only override allowed
   * evaluation criteria and data collection
   * voice recordings off, 30-day retention

   Re-run it whenever prices, services, prompts or the tunnel URL change.
5. (Optional) Post-call webhook: ElevenLabs → Agents → Settings → Webhooks, add `https://<PUBLIC_BASE_URL>/webhooks/elevenlabs` and put its secret in `ELEVENLABS_WEBHOOK_SECRET`. Conversation summaries then appear on the staff dashboard.
6. `python -m app.cli doctor` shows what's configured and what's missing.

Then open http://localhost:5173 and press **Start talking**.

### Free plan notes
* **15 voice minutes a month** and **4 concurrent conversations**, non-commercial. Keep `MAX_CONCURRENT_SESSIONS=4`.
* Save voice minutes: use **Type** mode (text-only chat, billed per message) and `#/preview` for UI work. Use voice for the final checks.
* For real testing, Starter (75 min, 6 concurrent) or Creator (275 min, 10 concurrent) are inexpensive upgrades.
* The ElevenLabs dashboard's "Test agent" button uses a placeholder session token, so tool calls answer "session expired". Test through the web app instead.

## 5. Google Calendar

**Prototype route: your own Google login, no key file.**

1. Enable the **Google Calendar API** for your project:
   https://console.cloud.google.com/apis/library/calendar-json.googleapis.com?project=dataflow-zone1
2. Log in again, adding the Calendar permission. Keep `cloud-platform` so Vertex AI keeps working:
   ```powershell
   gcloud auth application-default login --scopes=https://www.googleapis.com/auth/calendar,https://www.googleapis.com/auth/cloud-platform
   ```
3. Leave `GOOGLE_SERVICE_ACCOUNT_FILE` empty in `backend/.env`.
4. Create and link one calendar per clinician: `python -m app.cli setup-calendars`.
   * It creates `Health AI - Dr Sarah Patel` and the others in your Google Calendar.
   * It reuses any that already exist with that name or the clinician's name.
   * Add `--dry-run` to preview without changing anything.
5. Verify with `python -m app.cli check-calendar`. It reads free/busy, then creates and deletes a test event in each calendar.
6. Set `GOOGLE_CALENDAR_ENABLED=true` in `backend/.env` and restart the backend.

**Production route: a service account.**
* On Cloud Run, the attached service account is used automatically, so no key file is needed. Elsewhere, set `GOOGLE_SERVICE_ACCOUNT_FILE` to a JSON key.
* Then either:
  * share each clinician's calendar with the service account's email (**Make changes to events**), or
  * run `setup-calendars --share-with you@yourclinic.com`, so the calendars it creates are visible to staff.
* If your Workspace blocks sharing with external accounts, use domain-wide delegation instead and set `GOOGLE_IMPERSONATE_USER`.

To link a specific existing calendar by hand, use `python -m app.cli link-calendar <clinician_id> <calendar_id>`. The calendar ID is in the calendar's settings → *Integrate calendar*.

What staff see: bookings appear as `Private GP consultation - A. Testington (7K3P9Q)`, with no clinical details. Any event the clinician adds (leave, meetings, lunch) blocks those times for booking. Changes must be made through the assistant or the database; edits in Google Calendar aren't synced back.

## 6. Phone calls, SMS and the SOS line (Twilio)

All of this is optional. Without the `TWILIO_*` settings the web app works as before: phone features are off and the SOS button only offers 999.

| Feature | How it works |
|---|---|
| **Inbound calls** | The clinic's Twilio number is answered by Sophie (same agent, tools and backend). A call-start webhook creates a session for each call. If the caller's number belongs to a patient, Sophie only needs their surname and date of birth. |
| **Transfer to a person** | "Can I speak to someone?" puts the call through to `RECEPTION_PHONE_NUMBER` during opening hours. On the website, the browser calls reception instead. Outside hours, or if nobody answers, a callback request is logged for staff. |
| **Text confirmations** | An SMS after every booking, reschedule and cancellation (recorded against the appointment). |
| **Reminder calls** | From `REMINDER_CALL_HOUR` (17:00 by default) Sophie rings patients about tomorrow's appointment; they can confirm, reschedule or cancel. Staff can also ring one now from the dashboard. Off by default (`REMINDER_CALLS_ENABLED`). |
| **SOS emergency line** | The patient presses **SOS** on the website, or tells Sophie it's an emergency (website or phone). Every clinician switched to *on call* is rung at the same time. Each hears who needs help and presses 1 to join. Everyone who joins is on one call with the patient, and Sophie leaves the call. If nobody joins within `SOS_MAX_WAIT_SECONDS` (90 s), the patient is told to call 999. |

> **Safety:** the system never dials 999 itself. Patients are told to call 999 for anything life-threatening on the SOS button, in Sophie's replies and on every SOS screen. The SOS line is an extra route to the clinic's own doctors, not an emergency service.

### Set-up

1. Create a **Twilio account** at https://console.twilio.com (a trial is fine for testing; see the limits below).
2. **Buy a UK number** with Voice and SMS (*Phone Numbers → Buy a number*). UK numbers need a regulatory bundle (proof of address), which can take a few days to approve.
3. Create an **API key**: *Account → API keys & tokens → Create API key* (Standard). Copy its SID (`SK…`) and secret.
4. Fill in `backend/.env`:
   ```
   TWILIO_ACCOUNT_SID=AC...
   TWILIO_AUTH_TOKEN=...            # also used to check that webhooks really come from Twilio
   TWILIO_API_KEY_SID=SK...
   TWILIO_API_KEY_SECRET=...
   TWILIO_PHONE_NUMBER=+44...
   RECEPTION_PHONE_NUMBER=+44...    # optional
   ```
5. Restart with `dev.cmd`. Its `setup-agent` run now also:
   * connects the number to Sophie in ElevenLabs, so ElevenLabs answers it
   * points the call-start webhook at this backend
   * creates the TwiML app the browser uses for SOS and reception calls

   Check the result with `python -m app.cli doctor`.
6. In the **staff dashboard → On call & phone**, add each doctor's mobile number and switch on who is on call for SOS.
7. Optional: `REMINDER_CALLS_ENABLED=true` for reminder calls, and `SMS_SENDER=HealthAI` to text from a name rather than the number.

### Test it

* **Inbound call:** ring the clinic number and book an appointment. Calling from a number that belongs to a test patient skips the phone-number check.
* **SOS from the website:** put your own mobile in the On call tab, then press **SOS → Call our doctors now**. Your mobile rings; press 1 and you're on a call with the browser.
* **SOS by voice:** tell Sophie "this is an emergency, I need a doctor", on the website or on the phone.
* **Reception:** set `RECEPTION_PHONE_NUMBER` to your mobile and ask Sophie to speak to someone during opening hours.
* **Reminder call:** book an appointment for tomorrow, then press *Call now* in the staff dashboard's List tab.

### Trial account limits

* Calls and texts only reach **verified numbers** (*Phone Numbers → Verified Caller IDs*). Verify every doctor's and tester's mobile.
* Calls start with a short Twilio trial message. On outgoing calls the doctor presses a key first, then hears the SOS prompt and presses 1.
* Add credit (upgrade) before using it with real people.

### Notes

* Twilio calls this backend at `PUBLIC_BASE_URL/twilio/...`, so the tunnel must be running. With a free static ngrok domain (`NGROK_DOMAIN`) nothing needs re-pointing.
* Optional data residency: `TWILIO_REGION=ie1` with `TWILIO_EDGE=dublin` (Twilio Regional, Ireland; it must be enabled on your account).
* Costs: an SOS call rings every on-call doctor's mobile at once (UK mobile minutes each), plus the patient's leg.

## Deploy (Docker)

Production runs with Docker Compose behind the host's nginx (`deploy/nginx-health-ai.conf`, TLS via certbot). On the server:

```bash
cp .env.example .env            # then fill it in: PUBLIC_BASE_URL, secrets, ElevenLabs, Vertex, Twilio
docker compose up -d --build    # database, migrations, backend (port 8000) and website (port 8080)
docker compose run --rm backend python -m app.cli seed --no-demo-patients   # first deploy only
docker compose run --rm backend python -m app.cli setup-agent               # first deploy, and after URL or voice changes
docker compose run --rm backend python -m app.cli doctor                    # check what's configured
```

* Updating: `git pull` then `docker compose up -d --build` (migrations run automatically).
* `setup-agent` keeps its state (agent, TwiML app, phone number) on the `agentstate` volume; don't delete it.
* **Production gets its own agent.** On a fresh server `setup-agent` creates a new agent, and it moves the Twilio number to that agent. After that, take the `TWILIO_*` settings out of your laptop's `backend/.env` (or use a different number there). Otherwise `dev.cmd` moves the number back to your laptop the next time its ngrok address changes.
* On a Google Cloud VM, leave `VERTEX_CREDENTIALS_FILE` empty: the VM's service account is used. Elsewhere, use a service-account key; a `gcloud` login expires.
* Run one backend container. SOS timers, reminder calls and rate limits run inside the process.
* Needs Docker Compose 2.24 or newer.

---

## The agent's tools

| Tool | Purpose |
|---|---|
| `verify_patient` | surname + DOB + phone number → binds the patient to this session; returns upcoming appointments |
| `register_patient` | new patient (or verifies an existing record with the same details) |
| `check_availability` | slots for a service, optional clinician / dates / part of day; returns alternatives if none |
| `book_appointment` | books a slot returned by `check_availability`, after a spoken read-back and a "yes" |
| `get_my_appointments` | the verified patient's upcoming appointments |
| `reschedule_appointment` | moves an appointment (same reference) |
| `cancel_appointment` | cancels; flags late cancellations (< 24 h) |
| `request_callback` | logs a callback for staff (prescriptions, results, complaints, anything else) |
| `start_emergency_call` | SOS: rings every on-call doctor at once and puts the patient on a call with them (Sophie leaves); the patient is always told to call 999 if it's life-threatening |
| `connect_to_reception` | puts the patient through to reception (phone transfer or browser call), or offers a callback when closed |
| `end_call`, `skip_turn`, `voicemail_detection` | ElevenLabs system tools |

Every tool returns `{"ok": true|false, ...}` plus a `message` telling the agent what to do next. Dates come back both as ISO values (to pass back into other tools) and as spoken text ("Tuesday 14th October at 2:30pm").

## Tests

```powershell
docker compose up -d db
cd backend; .venv\Scripts\activate
pytest
```

127 tests run against real Postgres (database `healthcare_test`). They cover:
* availability rules, including the BST→GMT change
* concurrent double-booking races
* reschedule and cancel rules
* Google Calendar mirroring and retries
* identity lockout
* the complete tool journey over HTTP
* live events
* the Vertex proxy (streaming, thought signatures, retries)
* webhook signatures
* the agent configuration builder
* telephony with a fake Twilio: SOS (all doctors rung, press 1 to join, 999 fallback, patient hangs up), inbound calls, transfers, SMS, reminder calls and Twilio signatures

Frontend: `npm run typecheck` and `npm run build`.

---

## UK data protection: before real patients

This is a prototype. **Use synthetic data only** until the following are in place:

* **UK GDPR / DPA 2018:** health data is *special category* data, so you need an Article 9 condition (typically 9(2)(h)), a **DPIA**, ICO registration and an updated privacy notice.
* **Processor agreements (DPAs)** with ElevenLabs, Google Cloud and your hosting provider. ElevenLabs stores data in the US by default; EU data residency and Zero Retention Mode are Enterprise features. Vertex AI inference stays in `europe-west2`.
* **Keep the agent administrative.** It books and signposts, and never triages or advises. Once it starts influencing clinical decisions it may count as a medical device (MHRA).
* **Retention:** the agent is set to 30-day retention with no voice recordings. Transcripts are not stored by the backend unless `STORE_TRANSCRIPTS=true`.
* **Security:**
  * set strong secrets (`ENVIRONMENT=production` refuses the dev defaults)
  * restrict `/api/admin` to staff networks or put it behind SSO
  * use real hosting (not a tunnel)
  * use managed Postgres with backups

## Troubleshooting

| Symptom | Fix |
|---|---|
| "The voice assistant isn't configured yet" | Set `ELEVENLABS_API_KEY` and run `python -m app.cli setup-agent`. |
| Agent says the session expired on every tool | You're testing from the ElevenLabs dashboard; test via the web app. Or `PUBLIC_BASE_URL` is stale: restart the tunnel and re-run `setup-agent`. |
| Agent replies slowly | Run `python -m app.cli check-vertex`. Keep `VERTEX_REASONING_EFFORT=minimal` and use a Flash model. In production, host the backend in London (e.g. Cloud Run `europe-west2`) next to Vertex. |
| "All of our virtual receptionists are busy" | You've hit `MAX_CONCURRENT_SESSIONS` (your plan's concurrency). |
| Calendar badge shows `failed` on the staff dashboard | Check sharing/permissions with `python -m app.cli check-calendar`; failed syncs retry automatically every minute. |
| `setup-agent` reports HTTP 422 | The ElevenLabs API rejected a field. The full error is printed; the agent config lives in `app/agent/setup.py`. |
| The SOS button only offers 999 | Twilio isn't set up, or nobody is on call. See *On call & phone* in the staff dashboard and `python -m app.cli doctor`. |
| Twilio webhooks get 403 | `TWILIO_AUTH_TOKEN` is missing or wrong, or `PUBLIC_BASE_URL` isn't the URL Twilio called (restart `dev.cmd`). |
| SOS or reception call from the browser doesn't connect | Set `TWILIO_API_KEY_SID`/`TWILIO_API_KEY_SECRET` and run `setup-agent` (it creates the TwiML app). Allow the microphone. |

## Next steps

* **Existing clinic phone system:** connect it to the same agent over a SIP trunk instead of a Twilio number.
* **Email confirmations** and text reminders (SMS confirmations and reminder calls are done).
* **Staff SSO** for the dashboard, and syncing changes made in Google Calendar back into Postgres.
* **Real practice-management system:** swap the scheduling backend behind the same tool API.
