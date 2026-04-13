# Design Decisions

## Backend approach

**Decision: `claude -p` subprocess (not direct API)**

- Runs within Claude Max Plan — no API token cost
- Tradeoff: no native streaming support (response arrives all at once)
- Workaround: show a loading indicator while waiting; explore line-by-line streaming later

## Access

- Remote access via Tailscale (already configured)
- Backend runs on local machine, exposed only within Tailscale network

## Phases

### Phase 1 (current)
- Single AI assistant connection
- Basic chat UI: send message → receive response
- Conversation history managed in-memory (per session)
- PWA manifest for iPhone home screen installation

### Phase 2 (future)
- Multi-agent support
- Persistent conversation history
- Background session / push notification

## Frontend

**TBD** — candidates:
- Vanilla JS + HTML/CSS (minimal, no build step)
- React (more maintainable for complex UI)

## Open questions

- How to pass AI assistant context (persona, instructions) at startup
- Session persistence across page reloads
- Streaming UX improvement
