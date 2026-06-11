# PA System Tool

Speak a message through the machine running TALOS.

This runs on the host/server process and uses local speaker backends.
It does not play audio in the browser tab.

## Capabilities

- Announces short text out loud on host speakers
- Supports repeat count
- Supports optional backend selection
- Works with local OS speech tools when available

## Parameters

- `text` (required): The message to speak
- `repeat` (optional): Number of repeats, default `1`, max `5`
- `backend` (optional): `auto`, `windows_sapi`, `say`, `spd_say`, `espeak`
- `voice` (optional): Voice name, if backend supports it
- `rate` (optional): Speaking rate, backend specific

## Notes

- `auto` selects the best available backend for the host OS.
- If no supported speech backend is installed, the tool returns an error.
- This tool is intended for local notifications, alerts, and spoken updates.

## Examples

Speak once:

```json
{"text":"Build finished successfully."}
```

Repeat twice:

```json
{"text":"Standup starts in five minutes.","repeat":2}
```

Force backend:

```json
{"text":"Backup completed.","backend":"espeak"}
```
