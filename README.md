# pwa-client

A PWA (Progressive Web App) chat client for connecting to a local AI assistant from mobile devices.

## Overview

- Chat UI optimized for mobile (iPhone Safari)
- Connects to a locally running AI assistant via a backend API server
- Accessible remotely via Tailscale VPN
- Installable as a PWA for a native app-like experience

## Architecture

```
iPhone (Safari / PWA)
  ↓ HTTPS (Tailscale)
Backend API Server (running on local machine)
  ↓ subprocess
AI assistant (claude CLI)
```

## Setup

### Requirements

- Node.js (frontend)
- Python 3.x (backend)
- [Tailscale](https://tailscale.com/) for remote access
- Claude CLI (`claude`) installed and authenticated

### Configuration

Copy the example config and fill in your values:

```bash
cp config.example.json config.json
```

### Run

```bash
# Backend
cd backend
pip install -r requirements.txt
python main.py

# Frontend
cd frontend
npm install
npm run dev
```

## Development Status

See `DESIGN.md` for architecture decisions and current status.
