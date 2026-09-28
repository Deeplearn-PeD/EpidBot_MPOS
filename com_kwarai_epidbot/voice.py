"""Audio recording and playback helpers built on MicroPythonOS AudioManager."""

from mpos import AudioManager

REC_SAMPLE_RATE = 16000


def has_mic():
    try:
        return AudioManager.get_default_input() is not None
    except Exception:
        return False


def has_speaker():
    try:
        return AudioManager.get_default_output() is not None
    except Exception:
        return False


def start_recording(path, max_ms=15000, on_complete=None):
    recorder = AudioManager.recorder(
        file_path=path,
        duration_ms=max_ms,
        sample_rate=REC_SAMPLE_RATE,
        on_complete=on_complete,
        input=AudioManager.get_default_input(),
    )
    recorder.start()
    return recorder


def stop_recording(recorder):
    if recorder is not None:
        try:
            if recorder.is_recording():
                recorder.stop()
        except Exception:
            pass
    return recorder


def play_file(path, on_complete=None):
    output = AudioManager.get_default_output()
    if output is None:
        raise RuntimeError("No audio output available")
    try:
        AudioManager.stop()
    except Exception:
        pass
    player = AudioManager.player(
        file_path=path,
        stream_type=AudioManager.STREAM_MUSIC,
        on_complete=on_complete,
        output=output,
    )
    player.start()
    return player


def stop_playback(player=None):
    try:
        if player is not None and player.is_active():
            player.stop()
            return
    except Exception:
        pass
    try:
        AudioManager.stop()
    except Exception:
        pass
