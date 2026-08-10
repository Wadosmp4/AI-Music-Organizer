# AI Music Organizer

A personal tool that organizes a YouTube Music library into playlists automatically. It watches your liked songs, classifies each one against your existing and AI-suggested playlists (by artist history, genre rules, or description match), and puts every suggestion through an always-review approval flow that learns from your corrections over time.

## Stack

- **Backend:** FastAPI + SQLModel (SQLite), OAuth against the YouTube Data API v3 for the write path, `litellm` for LLM-assisted classification and playlist clustering.
- **Web:** React + TypeScript + Vite, styled with Tailwind CSS.
- **Run locally:** `docker compose up` (see `docker-compose.yml`).
