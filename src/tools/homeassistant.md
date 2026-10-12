# Home Assistant

Control the user's smart home through `homeassistant_execute`. The bot talks
to the same Home Assistant server configured for the Panel page
(`HOMEASSISTANT_URL` + `HOMEASSISTANT_TOKEN` in Settings).

## homeassistant_execute

One tool, four actions. Keep responses small by filtering.

- `status` — connection check.
- `list_entities` — compact one-line-per-entity listing (`entity_id = state unit (name)`).
  Use `domain` (e.g. `light`, `sensor`, `climate`, `cover`, `media_player`, `switch`,
  `automation`) and/or `search` (matched against entity_id and friendly name) to
  narrow the list. Discover exact entity_ids here before controlling anything.
- `get_state` — full state and attributes of one `entity_id`.
- `call_service` — control a device. Requires `domain`, `service`, `entity_id`,
  optional `data` object for extra parameters.

## Common service examples

- Lights: `domain=light`, `service=turn_on`, `data={"brightness": 200}`
- Climate: `domain=climate`, `service=set_temperature`, `data={"temperature": 21}`
- Covers: `domain=cover`, `service=open_cover`
- Media: `domain=media_player`, `service=media_play`
- Automations: `domain=automation`, `service=trigger`
- Any scene/script: `domain=scene`, `service=turn_on`

## Notes

- Always `list_entities` first to get the exact `entity_id` — never guess.
- `call_service` is fire-and-forget; use `get_state` afterwards to confirm.
- If the tool reports Home Assistant is not configured, tell the user to set it
  up in Settings — do not retry.
