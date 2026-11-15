from tracecut.media import split_caption, srt_timestamp


def test_srt_timestamp():
    assert srt_timestamp(61.234) == "00:01:01,234"


def test_split_caption_has_bounded_chunks():
    chunks = split_caption("这是一段很长的字幕，需要被安全地切分成多行。", max_chars=8)
    assert chunks
    assert all(len(chunk) <= 8 for chunk in chunks)
