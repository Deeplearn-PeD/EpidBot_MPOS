# Changelog

All notable changes to this project are documented here.

## 0.2.0 - 2026-09-28

- Browser-based provisioning: first run (or long-press on the gear) starts an
  on-device HTTP server (port 8126) serving a setup form; shows URL, QR code
  and a 4-digit PIN on screen. Lets users paste the long `ek_` API key from a
  phone/computer instead of typing it on a touchscreen. Auto-closes after
  5 minutes; PIN-gated.

## 0.1.0 - 2026-09-27

Initial release.

- Chat activity (LVGL) for the EpidBot REST API: submit + poll job pattern,
  cancellable, markdown-stripped reply bubbles, session continuity, local
  history persistence (last 20 messages).
- API key + server URL first-run setup; full settings screen (language, TTS
  voice/length/on-off) via MicroPythonOS SettingsActivity.
- Voice output (TTS, WAV) via AudioManager; voice input (STT) from microphone
  where available, or from a picked WAV file otherwise.
- Desktop and ESP32 install scripts; CPython unit tests for the API client.
