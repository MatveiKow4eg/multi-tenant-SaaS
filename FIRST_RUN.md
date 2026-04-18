# FIRST RUN GUIDE (Windows) - bot_lertisento

This guide assumes you only have project code and nothing else configured.

## 1) What you need to install first

1. Docker Desktop for Windows (with Docker Compose support).
2. Git (optional, but recommended).
3. A mailbox for outreach in Zone (required for real sending/receiving):
   - Example: sales@yourdomain.com
4. Optional (recommended): OpenAI API key for better qualification and reply analysis.

If Docker Desktop is not running, start it before all commands below.

## 2) Open project in terminal

In PowerShell:

```powershell
cd C:\Users\matve\bot_lertisento
```

## 3) Create environment file

Copy template:

```powershell
Copy-Item .env.example .env
```

Then open .env and set at least:

1. ZONE_EMAIL=your_real_mailbox
2. ZONE_PASSWORD=your_real_password

Recommended to set:

1. OPENAI_API_KEY=your_key
2. OPENAI_MAIN_MODEL=gpt-5.4
3. OPENAI_MINI_MODEL=gpt-5.4-mini

You can keep defaults for PostgreSQL and Redis for local start.

### Feature toggles (JSON dictionary)

Set all toggles in one env variable using a JSON object where key is toggle name and value is true/false:

```env
FEATURE_TOGGLES={"DELETE_USERS":true,"AUTH_STRICT_MODE":false}
```

Only toggles with value `true` are shown on the UI page:

1. http://localhost:8000/ui/feature-toggles

## 4) Start all services

```powershell
docker compose up --build
```

What should happen:

1. db and redis containers start.
2. api container runs Alembic migrations automatically.
3. worker and beat start Celery tasks.
4. API becomes available at http://localhost:8000

If this is first run, image build can take several minutes.

## 5) Quick health checks

Open in browser:

1. http://localhost:8000/api/health/
2. http://localhost:8000/api/health/mail

Expected:

1. /api/health/ returns status ok.
2. /api/health/mail returns smtp_ok and imap_ok true (if credentials are correct).

If mail check fails, verify ZONE_EMAIL/ZONE_PASSWORD in .env.

## 6) First real run (manual pipeline)

Use PowerShell Invoke-RestMethod.

### Step A: Run Finder

```powershell
$finderBody = @{
  countries = @("Lithuania", "Latvia")
  results_per_query = 10
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri "http://localhost:8000/api/companies/finder/run" -ContentType "application/json" -Body $finderBody
```

This schedules background search task.

### Step B: Check found companies

```powershell
Invoke-RestMethod -Method Get -Uri "http://localhost:8000/api/companies/"
```

Take one company id from result (example below uses 1).

### Step C: Run Research + Qualify

```powershell
Invoke-RestMethod -Method Post -Uri "http://localhost:8000/api/companies/1/research-qualify"
```

### Step D: Inspect company and contacts

```powershell
Invoke-RestMethod -Method Get -Uri "http://localhost:8000/api/companies/1"
Invoke-RestMethod -Method Get -Uri "http://localhost:8000/api/companies/1/contacts"
```

### Step E: Generate outreach sequence

```powershell
Invoke-RestMethod -Method Post -Uri "http://localhost:8000/api/companies/1/outreach/generate"
```

### Step F: Send campaign email

Find campaign id in DB flow (or via logs), then:

```powershell
Invoke-RestMethod -Method Post -Uri "http://localhost:8000/api/operations/mail/send-campaign/1"
```

### Step G: Ingest and classify replies

```powershell
Invoke-RestMethod -Method Post -Uri "http://localhost:8000/api/operations/replies/ingest"
```

### Step H: Check warm handoffs and analytics

```powershell
Invoke-RestMethod -Method Get -Uri "http://localhost:8000/api/operations/handoff/warm"
Invoke-RestMethod -Method Get -Uri "http://localhost:8000/api/operations/analytics/kpi"
Invoke-RestMethod -Method Get -Uri "http://localhost:8000/api/operations/analytics/funnel?period=week"
Invoke-RestMethod -Method Get -Uri "http://localhost:8000/api/operations/analytics/stage-funnel?period=week"
```

## 7) Day-1 troubleshooting

### Problem: api container fails on startup

Check logs:

```powershell
docker compose logs api --tail 200
```

Common reason: invalid DATABASE_URL or broken .env values.

### Problem: smtp_ok/imap_ok false

1. Wrong mailbox credentials.
2. Mailbox not active.
3. Network/firewall blocks.

### Problem: no AI quality

If OPENAI_API_KEY is empty, system uses fallback heuristics by design.

### Problem: Finder returns no useful companies

Try broader countries and increase results_per_query.

## 8) Stop and restart

Stop services:

```powershell
docker compose down
```

Start again:

```powershell
docker compose up --build
```

## 9) Optional reset for clean local start

Warning: this removes local Postgres data volume.

```powershell
docker compose down -v
```

Then start again with build.

## 10) What is already automated

1. DB migrations on API startup.
2. Celery beat periodic tasks for follow-up schedules and reply ingestion.
3. Stop logic on replies/blacklist.
4. Warm handoff card creation.

## 11) Recommended first success criterion

A good first pass is:

1. Finder found companies.
2. At least one company became qualified.
3. Contact extracted.
4. Outreach sequence generated.
5. One outbound email sent.
6. Reply ingestion ran without errors.

After this, tune targeting and messaging quality.
