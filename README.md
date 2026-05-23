# Wing Manager Discord Bot

Star Citizen org ship-assignment bot with slash-command mission boards.

## Features implemented

- `/shipassignment <ship_name>` creates a public embed tracker for a ship preset.
- Main embed buttons:
  - **Pick Assignment** (ephemeral slot picker)
  - **Unassign Self**
  - **Manage Ship** (role-gated)
- Users can hold only one assignment per active ship board.
- Multiple different ships can run at the same time.
- Duplicate mission for the same ship name is blocked.
- Manager-only controls (role IDs hardcoded per request):
  - Clear All Assignments
  - Clear Specific Assignment
  - Complete Mission (adds +1 mission completion to every assigned user)
  - Cancel Mission (deletes tracker)
- Mission completion totals are persisted in `missions.db` (SQLite).

## Setup

1. Create a Discord bot in the Discord Developer Portal.
2. Enable **Message Content Intent** if you plan to add prefix commands later (not required currently).
3. Invite the bot with `applications.commands` and `bot` scopes.
4. Install deps:
   ```bash
   pip install -r requirements.txt
   ```
5. Copy env template and set token:
   ```bash
   cp .env.example .env
   export DISCORD_TOKEN="..."
   ```
6. Edit `ships.json` presets to match your org ship staffing slots.
7. Run:
   ```bash
   python bot.py
   ```

## Notes

- `Manage Ship` is restricted to these role IDs:
  - `1498007839539466331`
  - `1497959044185587723`
  - `1496745530452086844`
- Active ship boards are in-memory; mission completions persist in SQLite.
