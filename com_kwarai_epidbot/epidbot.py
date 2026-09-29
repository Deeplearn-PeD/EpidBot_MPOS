"""EpidBot chat client activity for MicroPythonOS."""

import os
import random

import lvgl as lv

from mpos import (
    Activity,
    ConnectivityManager,
    DisplayMetrics,
    FontManager,
    Intent,
    InputActivity,
    MposKeyboard,
    SharedPreferences,
    SettingsActivity,
    TaskManager,
)

try:
    import uasyncio as asyncio
except ImportError:
    import asyncio

from . import provision
from . import voice
from .api import (
    AuthError,
    DEFAULT_SERVER,
    DISPLAY_CAP,
    EpidBotClient,
    EpidBotError,
    QuotaError,
    strip_markdown,
    truncate,
)

APP_ID = "com_kwarai_epidbot"
DATA_DIR = "prefs/" + APP_ID + "/cache"
REC_PATH = DATA_DIR + "/recording.wav"
TTS_PATH = DATA_DIR + "/tts_response.wav"

LOCALES = [("English", "en"), ("Portugues", "pt"), ("Espanol", "es")]
TTS_VOICES = [
    ("Alloy", "alloy"),
    ("Ash", "ash"),
    ("Ballad", "ballad"),
    ("Coral", "coral"),
    ("Echo", "echo"),
    ("Sage", "sage"),
    ("Shimmer", "shimmer"),
    ("Verse", "verse"),
]

MAX_RECORD_MS = 15000
HISTORY_MAX = 20

COLOR_USER_BG = lv.color_hex(0x1E88E5)
COLOR_BOT_BG = lv.color_hex(0xE8EAF0)
COLOR_ERR_BG = lv.color_hex(0xFFCDD2)
COLOR_TEXT_DARK = lv.color_hex(0x1B1F24)
COLOR_TEXT_LIGHT = lv.color_hex(0xFFFFFF)

TTS_SYMBOL = getattr(lv, "SYMBOL", None) and (
    getattr(lv.SYMBOL, "VOLUME_MAX", None) or lv.SYMBOL.AUDIO
) or lv.SYMBOL.AUDIO


def _makedirs(path):
    if not path:
        return
    parts = path.split("/")
    current = ""
    for part in parts:
        if not part:
            continue
        current = current + "/" + part if current else part
        try:
            os.mkdir(current)
        except OSError:
            pass


class EpidBotChat(Activity):

    def __init__(self):
        super().__init__()
        self.prefs = None
        self.api_key = ""
        self.server = DEFAULT_SERVER
        self.locale = "en"
        self.tts_enabled = False
        self.tts_voice = "alloy"
        self.tts_max_chars = 800
        self.session_id = None
        self.history = []
        self.pending = False
        self.cancel_requested = False
        self.recorder = None
        self.recording = False
        self.player = None
        self.speaking = False
        self.modal = None
        self.provision = None
        self._setup_prompted = False

        self.screen = None
        self.header = None
        self.status_label = None
        self.messages_container = None
        self.input_textarea = None
        self.keyboard = None
        self.send_btn = None
        self.send_lbl = None
        self.mic_btn = None
        self.mic_lbl = None
        self.tts_btn = None

    def onCreate(self):
        self.prefs = SharedPreferences(self.appFullName)
        self._load_prefs()
        _makedirs(DATA_DIR)
        self._setup_ui()

    def onResume(self, screen):
        super().onResume(screen)
        self._load_prefs()
        self._sync_tts_btn()
        if not self.api_key and not self._setup_prompted:
            lv.async_call(lambda x=None: self._start_first_run(), None)

    def onPause(self, screen):
        super().onPause(screen)
        if self.recording:
            self._stop_mic()
        self._stop_playback()

    def onDestroy(self, screen):
        self.cancel_requested = True
        if self.recording:
            self._stop_mic()
        self._stop_playback()

    def onBackPressed(self, screen):
        if self.modal is not None:
            self._close_modal()
            return True
        if self.pending or self.recording or self.speaking:
            self._show_modal(
                "Busy",
                "Stop the current activity and leave?",
                [
                    ("Leave", lambda e: self._force_exit()),
                    ("Stay", lambda e: self._close_modal()),
                ],
            )
            return True
        return False

    def _load_prefs(self):
        self.api_key = self.prefs.get_string("api_key", "")
        self.server = self.prefs.get_string("server_url", DEFAULT_SERVER)
        self.locale = self.prefs.get_string("locale", "en")
        self.tts_enabled = self.prefs.get_string("tts_enabled", "0") == "1"
        self.tts_voice = self.prefs.get_string("tts_voice", "alloy")
        try:
            self.tts_max_chars = int(self.prefs.get_string("tts_max_chars", "800"))
        except ValueError:
            self.tts_max_chars = 800
        sid = self.prefs.get_string("session_id", "")
        self.session_id = int(sid) if sid.isdigit() else None
        try:
            self.history = self.prefs.get_list("history") or []
        except Exception:
            self.history = []

    def _save_state(self):
        try:
            editor = self.prefs.edit()
            editor.put_string("api_key", self.api_key)
            editor.put_string("server_url", self.server)
            editor.put_string("locale", self.locale)
            editor.put_string("tts_enabled", "1" if self.tts_enabled else "0")
            editor.put_string("tts_voice", self.tts_voice)
            editor.put_string("tts_max_chars", str(self.tts_max_chars))
            editor.put_string("session_id", str(self.session_id) if self.session_id else "")
            while len(self.history) > HISTORY_MAX:
                self.history.pop(0)
            editor.put_list("history", self.history)
            editor.commit()
        except Exception as e:
            print("EpidBot: failed to save state:", e)

    def _client(self):
        return EpidBotClient(self.api_key, self.server, self.locale)

    def _setup_ui(self):
        screen = lv.obj()
        screen.set_style_pad_all(0, lv.PART.MAIN)
        screen.set_flex_flow(lv.FLEX_FLOW.COLUMN)
        screen.remove_flag(lv.obj.FLAG.SCROLLABLE)

        self.header = lv.obj(screen)
        self.header.set_width(lv.pct(100))
        self.header.set_height(lv.SIZE_CONTENT)
        self.header.set_flex_flow(lv.FLEX_FLOW.ROW)
        self.header.set_style_flex_main_place(lv.FLEX_ALIGN.SPACE_BETWEEN, lv.PART.MAIN)
        self.header.set_style_border_width(0, lv.PART.MAIN)
        self.header.set_style_pad_all(DisplayMetrics.pct_of_width(1), lv.PART.MAIN)
        self.header.set_style_pad_column(DisplayMetrics.pct_of_width(1), lv.PART.MAIN)

        title = lv.label(self.header)
        title.set_text("EpidBot")
        title.set_style_text_font(FontManager.getFont(size=16, emoji=True), lv.PART.MAIN)

        self.status_label = lv.label(self.header)
        self.status_label.set_text("")
        self.status_label.set_long_mode(
            getattr(lv.label.LONG_MODE, "DOTS", None)
            or getattr(lv.label.LONG_MODE, "DOT", None)
            or lv.label.LONG_MODE.WRAP
        )
        self.status_label.set_flex_grow(1)
        self.status_label.set_style_text_align(lv.TEXT_ALIGN.RIGHT, lv.PART.MAIN)

        self._icon_button(self.header, lv.SYMBOL.REFRESH, lambda e: self.on_new_chat())
        gear_btn = self._icon_button(
            self.header, lv.SYMBOL.SETTINGS, lambda e: self.open_settings()
        )
        gear_btn.add_event_cb(
            lambda e: self._open_provisioning(), lv.EVENT.LONG_PRESSED, None
        )

        self.messages_container = lv.obj(screen)
        self.messages_container.set_width(lv.pct(100))
        self.messages_container.set_flex_grow(1)
        self.messages_container.set_flex_flow(lv.FLEX_FLOW.COLUMN)
        self.messages_container.set_style_pad_all(DisplayMetrics.pct_of_width(1), lv.PART.MAIN)
        self.messages_container.set_style_pad_row(DisplayMetrics.pct_of_width(1), lv.PART.MAIN)

        input_row = lv.obj(screen)
        input_row.set_width(lv.pct(100))
        input_row.set_height(lv.SIZE_CONTENT)
        input_row.set_flex_flow(lv.FLEX_FLOW.ROW)
        input_row.set_style_border_width(0, lv.PART.MAIN)
        input_row.set_style_pad_all(DisplayMetrics.pct_of_width(1), lv.PART.MAIN)
        input_row.set_style_pad_column(DisplayMetrics.pct_of_width(1), lv.PART.MAIN)
        input_row.remove_flag(lv.obj.FLAG.SCROLLABLE)

        self.input_textarea = lv.textarea(input_row)
        self.input_textarea.set_one_line(True)
        self.input_textarea.set_width(lv.pct(40))
        self.input_textarea.set_placeholder_text("Ask EpidBot...")
        self.input_textarea.set_max_length(1000)
        self.input_textarea.set_flex_grow(1)

        self.send_btn = lv.button(input_row)
        self.send_btn.set_size(lv.SIZE_CONTENT, lv.SIZE_CONTENT)
        self.send_lbl = lv.label(self.send_btn)
        self.send_lbl.set_text("Send")
        self.send_lbl.center()
        self.send_btn.add_event_cb(lambda e: self.on_send_clicked(), lv.EVENT.CLICKED, None)

        self.mic_lbl = lv.label()
        self.mic_lbl.set_text(lv.SYMBOL.AUDIO)
        self.mic_btn = lv.button(input_row)
        self.mic_btn.set_size(
            DisplayMetrics.pct_of_width(15), DisplayMetrics.pct_of_width(15)
        )
        self.mic_btn.set_ext_click_area(10)
        self.mic_lbl.center()
        self.mic_btn.add_event_cb(lambda e: self.on_mic_clicked(), lv.EVENT.CLICKED, None)
        self.mic_btn.add_event_cb(
            lambda e: self.on_file_transcribe(), lv.EVENT.LONG_PRESSED, None
        )

        self.tts_btn = lv.button(input_row)
        self.tts_btn.set_size(
            DisplayMetrics.pct_of_width(15), DisplayMetrics.pct_of_width(15)
        )
        self.tts_btn.set_ext_click_area(10)
        tts_lbl = lv.label(self.tts_btn)
        tts_lbl.set_text(TTS_SYMBOL)
        tts_lbl.center()
        self.tts_btn.add_event_cb(lambda e: self.on_tts_toggle(), lv.EVENT.CLICKED, None)
        self._sync_tts_btn()

        self.keyboard = MposKeyboard(screen)
        self.keyboard.add_flag(lv.obj.FLAG.HIDDEN)
        self.keyboard.set_textarea(
            self.input_textarea,
            on_show=self._on_keyboard_show,
            on_hide=self._on_keyboard_hide,
        )

        self.screen = screen
        self.setContentView(screen)
        self._render_history()

    def _icon_button(self, parent, symbol, cb):
        btn = lv.button(parent)
        btn.set_size(DisplayMetrics.pct_of_width(12), DisplayMetrics.pct_of_width(12))
        lbl = lv.label(btn)
        lbl.set_text(symbol)
        lbl.center()
        btn.add_event_cb(cb, lv.EVENT.CLICKED, None)
        return btn

    def _on_keyboard_show(self):
        self.header.add_flag(lv.obj.FLAG.HIDDEN)

    def _on_keyboard_hide(self):
        self.header.remove_flag(lv.obj.FLAG.HIDDEN)
        self._scroll_bottom()

    def _render_history(self):
        for item in self.history:
            try:
                text = item.get("t", "")
                user = item.get("r") == "u"
            except AttributeError:
                continue
            self._append_bubble(text, user, error=False, scroll=False)
        self._scroll_bottom()

    def _append_bubble(self, text, user, error=False, scroll=True):
        row = lv.obj(self.messages_container)
        row.set_width(lv.pct(100))
        row.set_height(lv.SIZE_CONTENT)
        row.set_flex_flow(lv.FLEX_FLOW.ROW)
        row.set_style_border_width(0, lv.PART.MAIN)
        row.set_style_pad_all(0, lv.PART.MAIN)
        row.set_style_bg_opa(lv.OPA.TRANSP, lv.PART.MAIN)
        row.set_style_flex_main_place(
            lv.FLEX_ALIGN.END if user else lv.FLEX_ALIGN.START, lv.PART.MAIN
        )
        row.remove_flag(lv.obj.FLAG.SCROLLABLE)

        bubble = lv.obj(row)
        bubble.set_width(lv.pct(84))
        bubble.set_height(lv.SIZE_CONTENT)
        bubble.set_style_radius(10, lv.PART.MAIN)
        bubble.set_style_border_width(0, lv.PART.MAIN)
        bubble.set_style_pad_all(DisplayMetrics.pct_of_width(2), lv.PART.MAIN)
        bubble.set_style_bg_color(
            COLOR_USER_BG if user else (COLOR_ERR_BG if error else COLOR_BOT_BG),
            lv.PART.MAIN,
        )
        bubble.remove_flag(lv.obj.FLAG.SCROLLABLE)

        label = lv.label(bubble)
        label.set_text(text)
        label.set_width(lv.pct(100))
        label.set_long_mode(lv.label.LONG_MODE.WRAP)
        label.set_style_text_color(
            COLOR_TEXT_LIGHT if user else COLOR_TEXT_DARK, lv.PART.MAIN
        )
        label.set_style_text_font(FontManager.getFont(emoji=True), lv.PART.MAIN)

        if scroll:
            self._scroll_bottom()

    def _scroll_bottom(self):
        try:
            count = self.messages_container.get_child_count()
            if count > 0:
                self.messages_container.get_child(count - 1).scroll_to_view_recursive(True)
        except Exception:
            pass

    def _set_status(self, text):
        try:
            self.status_label.set_text(text)
        except Exception:
            pass

    def _if_fg(self, func, *args):
        if self.has_foreground():
            try:
                func(*args)
            except Exception as e:
                print("EpidBot: UI update failed:", e)

    def _ui_safe(self, func, *args):
        self.update_ui_threadsafe_if_foreground(func, *args)

    @staticmethod
    def _status_text(s):
        return {"submit": "Sending", "think": "Thinking"}.get(s, "")

    def _set_sending_ui(self, pending):
        try:
            if pending:
                self.send_lbl.set_text(lv.SYMBOL.STOP)
            else:
                self.send_lbl.set_text("Send")
            if self.mic_btn is not None:
                if pending:
                    self.mic_btn.add_state(lv.STATE.DISABLED)
                else:
                    self.mic_btn.remove_state(lv.STATE.DISABLED)
        except Exception:
            pass

    def on_send_clicked(self):
        if self.pending:
            self.cancel_requested = True
            self._set_status("Cancelling")
            return
        try:
            text = self.input_textarea.get_text().strip()
        except Exception:
            return
        if not text:
            return
        if not self.api_key:
            self._start_first_run()
            return
        if not self._is_online():
            self._set_status("Offline")
            return
        self._send_message(text)

    @staticmethod
    def _is_online():
        try:
            return ConnectivityManager.get().is_online()
        except Exception:
            return True

    def _send_message(self, text):
        self._stop_playback()
        self.pending = True
        self.cancel_requested = False
        self._set_sending_ui(True)
        self._set_status("Sending")
        self._append_bubble(text, True)
        self.history.append({"r": "u", "t": text})
        self._save_state()
        self.input_textarea.set_text("")
        try:
            if not self.keyboard.has_flag(lv.obj.FLAG.HIDDEN):
                self.keyboard.hide_keyboard()
        except Exception:
            pass
        self._scroll_bottom()
        TaskManager.create_task(self._chat_task(text))

    async def _chat_task(self, text):
        client = self._client()
        try:
            result = await client.chat_and_wait(
                text,
                session_id=self.session_id,
                on_status=lambda s: self._if_fg(self._set_status, self._status_text(s)),
                should_cancel=lambda: self.cancel_requested,
            )
        except AuthError as e:
            self._if_fg(self._chat_failed, "Auth error: %s" % e)
            self._if_fg(self._set_status, "Auth err - long-press gear")
            return
        except QuotaError as e:
            self._if_fg(self._chat_failed, "Quota: %s" % e)
            return
        except EpidBotError as e:
            self._if_fg(self._chat_failed, "Error: %s" % e)
            return
        except Exception as e:
            self._if_fg(self._chat_failed, "Error: %s" % e)
            return
        self._if_fg(self._chat_done, result)

    def _chat_done(self, result):
        self.pending = False
        self._set_sending_ui(False)
        status = result.get("status")
        sid = result.get("session_id")
        if sid:
            self.session_id = sid
        if status == "completed":
            content = result.get("content") or "(empty response)"
            plain = truncate(strip_markdown(content), DISPLAY_CAP)
            self.history.append({"r": "b", "t": plain})
            self._append_bubble(plain, False)
            self._set_status("")
            if self.tts_enabled:
                self._speak(plain)
        elif status == "failed":
            err = str(result.get("error") or "Request failed")
            self._append_bubble(err, False, error=True)
            self._set_status("Failed")
        elif status == "cancelled":
            self._set_status("Cancelled")
        elif status == "timeout":
            self._set_status("Timed out")
        else:
            self._set_status(str(status))
        self._save_state()
        self._scroll_bottom()

    def _chat_failed(self, msg):
        self.pending = False
        self._set_sending_ui(False)
        self._append_bubble(str(msg), False, error=True)
        self._set_status("Error")
        self._scroll_bottom()

    def on_new_chat(self):
        if self.pending or self.recording:
            self._set_status("Busy")
            return
        self._stop_playback()
        self.session_id = None
        self.history = []
        self._save_state()
        self.messages_container.clean()
        self._set_status("")

    def on_tts_toggle(self):
        self.tts_enabled = not self.tts_enabled
        self._sync_tts_btn()
        self._save_state()

    def _sync_tts_btn(self):
        try:
            if self.tts_btn is None:
                return
            if self.tts_enabled:
                self.tts_btn.add_state(lv.STATE.CHECKED)
            else:
                self.tts_btn.remove_state(lv.STATE.CHECKED)
        except Exception:
            pass

    def _speak(self, text):
        if not voice.has_speaker():
            self._set_status("No audio out")
            return
        self._stop_playback()
        self.speaking = True
        self._set_status("Fetching TTS")
        TaskManager.create_task(self._tts_task(text))

    async def _tts_task(self, text):
        client = self._client()
        try:
            await client.tts_to_file(
                text,
                TTS_PATH,
                voice=self.tts_voice or None,
                max_chars=self.tts_max_chars,
            )
        except Exception as e:
            self._if_fg(self._tts_failed, str(e))
            return
        self._if_fg(self._tts_play)

    def _tts_play(self):
        try:
            self.player = voice.play_file(
                TTS_PATH,
                on_complete=lambda msg: self._ui_safe(self._tts_finished),
            )
            self._set_status("Speaking")
        except Exception as e:
            self._tts_failed(str(e))

    def _tts_finished(self):
        self.speaking = False
        self.player = None
        self._set_status("")
        try:
            os.remove(TTS_PATH)
        except OSError:
            pass

    def _tts_failed(self, msg):
        self.speaking = False
        self._set_status("TTS: " + str(msg)[:20])

    def _stop_playback(self):
        if self.player is not None or self.speaking:
            voice.stop_playback(self.player)
            self.player = None
            self.speaking = False

    def on_mic_clicked(self):
        if self.pending:
            return
        if self.recording:
            self._stop_mic()
            return
        if voice.has_mic():
            self._start_mic()
        else:
            self.on_file_transcribe()

    def _start_mic(self):
        _makedirs(DATA_DIR)
        try:
            self.recorder = voice.start_recording(
                REC_PATH,
                MAX_RECORD_MS,
                on_complete=lambda msg: self._ui_safe(self._rec_auto_stopped),
            )
        except Exception as e:
            self._set_status("Mic: " + str(e)[:20])
            return
        self.recording = True
        self.mic_lbl.set_text(lv.SYMBOL.STOP)
        self._set_status("Recording")

    def _stop_mic(self):
        self.recording = False
        voice.stop_recording(self.recorder)
        self.recorder = None
        self._begin_transcribe()

    def _rec_auto_stopped(self):
        if self.recording:
            self.recording = False
            self.recorder = None
            self._begin_transcribe()

    def _begin_transcribe(self):
        self.mic_lbl.set_text(lv.SYMBOL.AUDIO)
        self._set_status("Transcribing")
        TaskManager.create_task(self._transcribe_task())

    async def _transcribe_task(self):
        try:
            with open(REC_PATH, "rb") as f:
                data = f.read()
        except Exception as e:
            self._if_fg(self._transcribe_failed, str(e))
            return
        if len(data) < 1000:
            self._if_fg(self._transcribe_failed, "Recording too short")
            return
        client = self._client()
        try:
            text = await client.transcribe(data, language=self.locale)
        except Exception as e:
            self._if_fg(self._transcribe_failed, str(e))
            return
        self._if_fg(self._transcribe_ok, text)

    def _transcribe_ok(self, text):
        self._set_status("")
        if not text:
            self._set_status("No speech detected")
            return
        self._show_modal(
            "Transcribed",
            text,
            [
                ("Send", lambda e: self._confirm_transcript(text)),
                ("Cancel", lambda e: self._close_modal()),
            ],
        )

    def _confirm_transcript(self, text):
        self._close_modal()
        if not self.pending and text:
            self._send_message(text)

    def _transcribe_failed(self, msg):
        print("EpidBot: STT failed:", msg)
        self._set_status("STT: " + str(msg)[:60])

    def on_file_transcribe(self):
        intent = Intent(
            action="pick_file",
            extras={"start_dir": "/", "path_pattern": [".wav"]},
        )
        self.startActivityForResult(intent, self._on_wav_picked)

    def _on_wav_picked(self, result):
        if not result or not result.get("result_code"):
            return
        paths = (result.get("data") or {}).get("paths", [])
        if not paths:
            return
        path = paths[0]
        self._set_status("Transcribing")
        TaskManager.create_task(self._transcribe_file_task(path))

    async def _transcribe_file_task(self, path):
        print("EpidBot: transcribing file:", path)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except Exception as e:
            self._if_fg(self._transcribe_failed, str(e))
            return
        print("EpidBot: WAV bytes:", len(data))
        if len(data) > 24_000_000:
            self._if_fg(self._transcribe_failed, "File too large")
            return
        client = self._client()
        try:
            text = await client.transcribe(data, language=self.locale)
        except Exception as e:
            self._if_fg(self._transcribe_failed, str(e))
            return
        self._if_fg(self._transcribe_ok, text)

    def open_settings(self):
        settings = [
            {
                "title": "API key",
                "key": "api_key",
                "ui": "textarea",
                "default_value": self.api_key,
            },
            {
                "title": "Server URL",
                "key": "server_url",
                "ui": "textarea",
                "default_value": self.server,
            },
            {
                "title": "Language",
                "key": "locale",
                "ui": "radiobuttons",
                "ui_options": LOCALES,
                "default_value": self.locale,
            },
            {
                "title": "TTS voice",
                "key": "tts_voice",
                "ui": "dropdown",
                "ui_options": TTS_VOICES,
                "default_value": self.tts_voice,
            },
            {
                "title": "TTS max chars",
                "key": "tts_max_chars",
                "ui": "slider",
                "min": 200,
                "max": 2000,
                "default_value": str(self.tts_max_chars),
            },
            {
                "title": "Speak replies (TTS)",
                "key": "tts_enabled",
                "ui": "radiobuttons",
                "ui_options": [("On", "1"), ("Off", "0")],
                "default_value": "1" if self.tts_enabled else "0",
            },
        ]
        intent = Intent(activity_class=SettingsActivity)
        intent.putExtra("prefs", self.prefs)
        intent.putExtra("settings", settings)
        self.startActivity(intent)

    def _start_first_run(self):
        if self.pending or self.api_key:
            return
        self._setup_prompted = True
        self._show_modal(
            "Set up EpidBot",
            "Provide your ek_ API key. Typing it on a touchscreen is painful "
            "- Browser setup lets you paste it on a phone or computer "
            "connected to the same network.",
            [
                ("Browser setup", lambda e: self._first_run_browser()),
                ("Type key", lambda e: self._first_run_type()),
                ("Cancel", lambda e: self._close_modal()),
            ],
        )

    def _first_run_browser(self):
        self._close_modal()
        self._open_provisioning()

    def _first_run_type(self):
        self._close_modal()
        self._type_key()

    def _type_key(self):
        self._open_input(
            "EpidBot API key",
            "api_key",
            self.api_key,
            "ek_...",
            self._setup_key_result,
        )

    def _open_input(self, title, key, value, placeholder, cb):
        intent = Intent(activity_class=InputActivity)
        intent.putExtra(
            "setting",
            {"title": title, "key": key, "placeholder": placeholder},
        )
        intent.putExtra("value", value or "")
        self.startActivityForResult(intent, cb)

    def _setup_key_result(self, result):
        if result and result.get("result_code"):
            value = ((result.get("data") or {}).get("value") or "").strip()
            if value:
                self.api_key = value
                self._save_state()
                self._set_status("")
                self._open_input(
                    "Server URL",
                    "server_url",
                    self.server,
                    "https://api.epidbot.kwar-ai.com.br",
                    self._setup_server_result,
                )
                return
        if not self.api_key:
            self._set_status("Set API key (gear)")

    def _setup_server_result(self, result):
        if result and result.get("result_code"):
            value = ((result.get("data") or {}).get("value") or "").strip()
            if value:
                self.server = value.rstrip("/")
                self._save_state()
        self._set_status("Testing...")
        TaskManager.create_task(self._validate_task())

    async def _validate_task(self):
        client = self._client()
        try:
            await client.validate()
            self._if_fg(self._set_status, "Key OK")
        except AuthError:
            self._if_fg(self._set_status, "Invalid key")
        except Exception as e:
            self._if_fg(self._set_status, "Conn error")

    def _open_provisioning(self):
        if self.provision is not None:
            self._set_status("Setup already running")
            return
        pin = str(random.getrandbits(14) % 9000 + 1000)
        server = provision.ProvisionServer(
            pin, server_url=self.server, locale=self.locale
        )
        try:
            url = server.start()
        except Exception as e:
            print("EpidBot: provisioning server failed:", e)
            self._set_status("Setup port busy")
            return
        self._show_provision_screen(url, pin)
        self.provision = server
        TaskManager.create_task(self._provision_watch(server))

    async def _provision_watch(self, server):
        waited = 0.0
        while server.active and waited < provision.TIMEOUT_S:
            await asyncio.sleep(0.5)
            waited += 0.5
            if server.result is not None:
                break
        result = server.result
        server.stop()
        if self.provision is server:
            self.provision = None
        if result:
            self._if_fg(self._apply_provision, result)
        elif waited >= provision.TIMEOUT_S:
            self._if_fg(self._provision_timeout)

    def _provision_timeout(self):
        self._close_modal()
        self._set_status("Setup timed out")

    def _show_provision_screen(self, url, pin):
        self._close_modal()
        overlay = lv.obj(lv.layer_top())
        overlay.set_size(lv.pct(94), lv.SIZE_CONTENT)
        overlay.set_style_radius(10, lv.PART.MAIN)
        overlay.set_style_pad_all(DisplayMetrics.pct_of_width(2), lv.PART.MAIN)
        overlay.set_style_pad_row(3, lv.PART.MAIN)
        overlay.set_flex_flow(lv.FLEX_FLOW.COLUMN)
        overlay.set_flex_align(
            lv.FLEX_ALIGN.CENTER, lv.FLEX_ALIGN.CENTER, lv.FLEX_ALIGN.CENTER
        )
        overlay.align(lv.ALIGN.CENTER, 0, 0)

        title = lv.label(overlay)
        title.set_text("Browser setup")
        title.set_style_text_font(FontManager.getFont(size=15, emoji=True), lv.PART.MAIN)

        hint = lv.label(overlay)
        hint.set_text("Open in a browser (same network):")
        url_lbl = lv.label(overlay)
        url_lbl.set_text(url)
        url_lbl.set_style_text_font(FontManager.getFont(size=14, emoji=True), lv.PART.MAIN)
        url_lbl.set_width(lv.pct(100))
        url_lbl.set_long_mode(lv.label.LONG_MODE.WRAP)
        url_lbl.set_style_text_align(lv.TEXT_ALIGN.CENTER, lv.PART.MAIN)

        try:
            qr = lv.qrcode(overlay)
            qr_size = round(DisplayMetrics.min_dimension() * 0.45)
            qr.set_size(qr_size)
            qr.update(url, len(url))
        except Exception as e:
            print("EpidBot: QR unavailable:", e)

        pin_lbl = lv.label(overlay)
        pin_lbl.set_text("PIN: " + pin)
        pin_lbl.set_style_text_font(FontManager.getFont(size=18, emoji=True), lv.PART.MAIN)

        stop_btn = lv.button(overlay)
        stop_btn.set_size(lv.SIZE_CONTENT, lv.SIZE_CONTENT)
        stop_lbl = lv.label(stop_btn)
        stop_lbl.set_text("Stop")
        stop_lbl.center()
        stop_btn.add_event_cb(lambda e: self._close_modal(), lv.EVENT.CLICKED, None)

        self.modal = overlay

    def _apply_provision(self, result):
        self._close_modal()
        api_key = (result.get("api_key") or "").strip()
        if api_key:
            self.api_key = api_key
        server_url = (result.get("server_url") or "").strip().rstrip("/")
        if server_url:
            self.server = server_url
        locale = result.get("locale")
        if locale:
            self.locale = locale
        self._save_state()
        self._set_status("Testing key...")
        TaskManager.create_task(self._validate_task())

    def _show_modal(self, title, body, buttons):
        self._close_modal()
        overlay = lv.obj(lv.layer_top())
        overlay.set_size(lv.pct(90), lv.SIZE_CONTENT)
        overlay.set_style_radius(10, lv.PART.MAIN)
        overlay.set_style_pad_all(DisplayMetrics.pct_of_width(2), lv.PART.MAIN)
        overlay.set_style_pad_row(4, lv.PART.MAIN)
        overlay.set_flex_flow(lv.FLEX_FLOW.COLUMN)
        overlay.align(lv.ALIGN.CENTER, 0, 0)

        title_lbl = lv.label(overlay)
        title_lbl.set_text(title)

        body_obj = lv.obj(overlay)
        body_obj.set_width(lv.pct(100))
        body_obj.set_height(DisplayMetrics.pct_of_height(45))
        body_lbl = lv.label(body_obj)
        body_lbl.set_text(body)
        body_lbl.set_width(lv.pct(100))
        body_lbl.set_long_mode(lv.label.LONG_MODE.WRAP)

        btn_row = lv.obj(overlay)
        btn_row.set_width(lv.pct(100))
        btn_row.set_height(lv.SIZE_CONTENT)
        btn_row.set_flex_flow(lv.FLEX_FLOW.ROW)
        btn_row.set_style_pad_all(0, lv.PART.MAIN)
        btn_row.set_style_border_width(0, lv.PART.MAIN)
        btn_row.set_style_pad_column(6, lv.PART.MAIN)
        for label_text, cb in buttons:
            btn = lv.button(btn_row)
            btn.set_size(lv.SIZE_CONTENT, lv.SIZE_CONTENT)
            lbl = lv.label(btn)
            lbl.set_text(label_text)
            lbl.center()
            btn.add_event_cb(cb, lv.EVENT.CLICKED, None)

        self.modal = overlay

    def _close_modal(self):
        if self.provision is not None:
            self.provision.stop()
            self.provision = None
        if self.modal is not None:
            try:
                self.modal.delete()
            except Exception:
                pass
            self.modal = None

    def _force_exit(self):
        self.cancel_requested = True
        self._stop_playback()
        if self.recording:
            self._stop_mic()
        self._close_modal()
        self.finish()
