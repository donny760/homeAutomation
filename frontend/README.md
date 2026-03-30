# Powerwall Dashboard — Next.js Frontend

React/Next.js frontend for the Powerwall home automation dashboard.
The Python backend (`server.py`, `rules.py`, etc.) remains unchanged.

## Development

Run both processes side by side:

```bash
# Terminal 1 — Python backend (port 5000)
cd D:\Projects\homeAutomation
py server.py

# Terminal 2 — Next.js dev server (port 3000)
cd D:\Projects\homeAutomation\frontend
npm install
npm run dev
```

The Next.js dev server proxies all `/api/*` requests to `http://localhost:5000`
via the rewrite rule in `next.config.js`, so API URLs in React code stay as-is.

Open http://localhost:3000 in the browser.

## Production

Build the static/optimized output, then serve it from Flask:

```bash
cd D:\Projects\homeAutomation\frontend
npm run build
```

Copy the build output (`.next/` or the exported folder) into the path
configured in `server.py`'s static folder setting. You can also use
`next export` (if configured) to produce a fully static build.

Alternatively, run the Next.js production server (`npm start`) behind
a reverse proxy alongside Flask.
