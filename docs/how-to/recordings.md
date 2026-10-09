# Videos, podcasts and recordings

A recording is read into a transcript, and the transcript is indexed like any document, with one difference: every chunk knows when it was said. A hit in a call is cited `call.wav#t=63`, the second it starts at, which a media player goes straight to, and a person is shown `call.wav, 1:03`.

This page covers the recording itself: audio and video files, YouTube videos and podcast episodes, and the engines that hear them. For who reads which file type in general, see [Extract, keep, index](extract-keep-index.md). For the same jobs served over HTTP, see [Run an extraction service](extraction-service.md).

!!! warning "Recordings you have the right to use"
    Index only recordings you have the right to use: your own calls and meetings, with the consent your jurisdiction asks for, and media whose owner allows it. A platform's terms can restrict downloading what it hosts. YouTube's terms restrict downloading its content outside YouTube's own features, and which videos you read is yours to weigh. YouTube also refuses more and more requests from cloud addresses as coming from a bot, so a video that reads on a laptop can fail in a function app or a container.

## What to install

| Extra | Brings | For |
| --- | --- | --- |
| `asr` | faster-whisper | speech to text on this machine |
| `ffmpeg` | imageio-ffmpeg, which ships an ffmpeg binary, about 90 MB | the sound out of a video, when a service does the hearing |
| `video` | `ffmpeg` and faster-whisper | the sound out of a video, heard on this machine |
| `youtube` | yt-dlp | YouTube videos, captions and sound |
| `feeds` | feedparser | RSS and Atom feeds, podcasts among them |
| `extract` | `documents`, `ocr`, `asr` and `video` | every local reader at once |

```bash
pip install "vectrixdb[asr]"              # recordings, heard on this machine
pip install "vectrixdb[video]"            # and videos
pip install "vectrixdb[youtube]"          # and YouTube
```

Azure AI Speech needs no extra: it is one HTTPS request. Amazon Transcribe needs `boto3`, which `asr-aws` brings.

`vectrixdb check` says, before a server starts, who reads recordings, videos and YouTube addresses with its settings, and what is missing: faster-whisper not installed, or installed without its model on the machine, an ffmpeg that does not run, yt-dlp missing. It imports packages and runs `ffmpeg -version`, and never reads a file or fetches a model.

## A transcript is a document

Speech becomes a document whose pages are minutes. Everything said within one minute is one paragraph, minute `n` starts page `n + 1`, and a silent minute still counts, so page twelve is always the twelfth minute. Every phrase keeps its start and end in seconds, in `doc.segments`.

`segments_to_document()` builds one from timed phrases, which is what every engine here does with what it heard:

```python
from vectrixdb import Vectrix
from vectrixdb.extract.engines import segments_to_document, transcript_markdown

said = [
    (0.0, 4.2, "Thanks for calling, this is the billing team."),
    (4.2, 9.8, "My card was charged twice for the March invoice."),
    (63.5, 70.1, "I have refunded the second charge; it takes three to five days."),
]
call = segments_to_document(said)
call.metadata["filename"] = "call.wav"

db = Vectrix("calls")
db.add_document(call, doc_id="call-0412", chunk="sentence", chunk_size=120, overlap=0)

hit = db.search("when will the refund arrive", limit=1).top
hit.citation, hit.readable_citation               # ('call.wav#t=63', 'call.wav, 1:03')
hit.metadata["start_seconds"], hit.metadata["end_seconds"]   # (63.5, 70.1)
```

Each chunk carries `start_seconds`, from the start of the phrase it opens in, and `end_seconds`, the end of the last phrase it holds. The citation is `name#t=S`, whole seconds, which a browser's media player and most others read as a place to start. `segments_to_document(segments, seconds_per_page=60.0)` takes another page length, and so does every engine below.

`transcript_markdown(doc)` writes the transcript for a person to read: a heading, the URL, channel, duration and language when they are known, the full text, then every phrase timed:

```python
print(transcript_markdown(call))
```

```text
# Transcript: call.wav

- Duration: 1:10

---

## Full Text

Thanks for calling, this is the billing team. My card was charged twice for the March invoice.

I have refunded the second charge; it takes three to five days.

## Segments

**[0:00 → 0:04]** Thanks for calling, this is the billing team.

**[0:04 → 0:09]** My card was charged twice for the March invoice.

**[1:03 → 1:10]** I have refunded the second charge; it takes three to five days.
```

A YouTube video is headed `YouTube:` and its title, anything else `Transcript:` and its file name. A line whose value is not known is left out. This is for people; what a collection indexes is the document itself.

## Audio files

`.wav`, `.mp3`, `.m4a`, `.flac`, `.ogg`, `.opus` and `.aac` are recordings. One with no extractor registered goes to `Whisper()` on this machine by itself. Register an engine for its suffixes to choose:

```python
from vectrixdb import Vectrix
from vectrixdb.extract.engines import Whisper

db = Vectrix("calls", extractors={".wav": Whisper("small", language="en"), ".mp3": Whisper("small", language="en")})
db.add_document("recordings/call-0412.wav", chunk="sentence")
```

The engines:

| Engine | Where it hears | Takes | Notes |
| --- | --- | --- | --- |
| `Whisper(model="base", language=None)` | this machine, faster-whisper | audio | the model is downloaded from Hugging Face the first time it loads; `language=None` lets it detect |
| `AzureSpeech(endpoint, key, locales=("en-US",), speakers=0)` | Azure AI Speech, fast transcription | audio | one request a file; `speakers` tells up to that many voices apart |
| `Transcribe(client, s3, output_bucket, language_code="en-US")` | Amazon Transcribe | audio and video | reads media from S3, so the file needs an `s3://` source: run it behind the worker with an S3 fetcher |
| `Video(audio=None, frames=None)` | ffmpeg here, then `audio` | video | the sound goes to `audio`, `Whisper()` by default |

**Whisper.** `model` is a faster-whisper size, `tiny`, `base`, `small`, `medium` and so on, loaded once and kept. `engine=` replaces faster-whisper with any callable from a file's path to `(start, end, text)` segments, which is how a model of your own fits in. Fetch the model ahead of the first recording on a machine with a network:

```bash
python -c "from faster_whisper import download_model; download_model('base')"
```

With `VECTRIXDB_OFFLINE=1` it is read from the Hugging Face cache alone, and a model that is not there raises `ModelDownloadError` naming that command. See [The speech model](offline.md#the-speech-model).

**Azure Speech.** `endpoint` is the resource's https address and `key` its key; nothing is read from the environment. `locales` are the languages a recording may be in, and the transcript's `language` is the one it heard most. With `speakers` above 1, each phrase starts with who said it, `Speaker 1: ...`, each turn on a line of its own, and `speakers` in the metadata counts the voices; a locale that cannot tell voices apart is asked again without. A `429` or `503` is asked again when the service says to come back, three times. A recording with nothing said has `speech: none` in its metadata.

**Transcribe.** `language_code=None` asks Transcribe to identify the language. The bytes handed to it are not used: Transcribe fetches the media from S3 itself, and writes its result to `output_bucket` under `output_prefix`.

## Video files

`.mp4`, `.mov`, `.webm`, `.mkv` and `.avi` are videos. `Video` takes the sound out with ffmpeg, mono at 16 kHz, and hands it to an audio engine:

```python
from vectrixdb import Vectrix
from vectrixdb.extract.engines import AzureSpeech, RapidOcr, Video

speech = AzureSpeech("https://your-speech.cognitiveservices.azure.com", key="your-key", locales=("en-US", "fr-CA"))
db = Vectrix("webinars", extractors={".mp4": Video(audio=speech, frames=RapidOcr())})
db.add_document("webinars/q3-results.mp4")
```

`frames=` reads the screen too. A frame is taken at the start and at each moment the picture changes, a slide coming in or a cut, half a second after it settles, at most `max_frames`, twelve by default, spread over the whole video. What each frame says joins the transcript at its second as an `On screen:` line, and a slide still showing is not read twice. `scene`, 0.02 by default, is the least change that counts: a slide whose words change scores about 0.03, a cut in filmed footage 0.3 or more. Any picture reader works as `frames`, `RapidOcr()` on this machine or Document Intelligence; every frame is a page it reads.

A video with no sound track is read for its screen alone and says `sound_track: false`. Every video's metadata has `video: true`, and `on_screen` counts the screen lines.

`pip install "vectrixdb[ffmpeg]"` alone is enough when the hearing is done by a service; `vectrixdb[video]` adds faster-whisper for hearing it here. `ffmpeg=` replaces the binary with any callable from the video's path and a WAV path to write.

## YouTube

`load_youtube(url)` reads one video's words: its captions when they will do, its sound when they will not. Captions cost nothing, no sound is downloaded, no ffmpeg runs and no speech engine is paid, so they are read first.

```python
from vectrixdb import Vectrix, load_youtube
from vectrixdb.extract.engines import AzureSpeech

speech = AzureSpeech("https://your-speech.cognitiveservices.azure.com", key="your-key")
doc = load_youtube("https://youtu.be/q3Results01", audio=speech, language="en-US")
doc.metadata["transcript_source"]        # 'captions' or 'speech'

db = Vectrix("videos")
db.add_document(doc, doc_id=doc.metadata["youtube_id"], chunk="sentence")
```

`captions=` says which captions will do. The table is in [A YouTube video](extract-keep-index.md#a-youtube-video): `"uploaded"`, the default, takes the uploader's own; `"automatic"` takes YouTube's automatic ones too; `"translated"` takes YouTube's machine translation as well; `"never"` always reads the sound.

How a track is chosen:

- **By kind first.** The uploader's, then YouTube's automatic, then a translation, as far as `captions` allows.
- **Then by language.** `language` picks the track, the same code first, `en-GB` for `en-GB`, then `en`, then any English. Several, `"en-US,fr-CA"`, are tried in turn, and Azure Speech is given them all. A language with no track is heard from the sound in that language, never read from captions in another.
- **Left out,** the track is the video's own language, then English, then the uploader's first track whatever its language.
- **The format** is YouTube's own timed text, json3, then WebVTT, then SubRip. A rolling caption repeats each line on the next cue, and each line is kept once, ending when the next one starts.

When no caption the choice allows is there, or a track cannot be read, the sound is downloaded, m4a when YouTube has it since Azure Speech reads it as it comes, and anything else through ffmpeg first. `audio=` hears it, `Whisper()` on this machine by default. The engine passed in is never changed: `language` is set on a copy, because an engine is usually shared by every request at once.

The metadata says where the words came from:

| Key | What it holds |
| --- | --- |
| `transcript_source` | `captions` or `speech` |
| `caption_language`, `caption_automatic` | with captions: the track's language, and whether YouTube made it |
| `caption_translated_from` | with a translation: the language it was translated from |
| `captions_unused` | with speech, when captions were allowed: why they were not read |
| `youtube_id`, `title`, `channel`, `duration`, `published` | the video, as YouTube describes it; `source` is the plain watch address |
| `audio_file` | with speech: the name of the sound that was downloaded |

`save_to="output"` writes the transcript as `<video id>_transcript.md`, named by the id so two requests sharing a folder never hand each other's file over. `keep_audio=True` keeps the sound beside it, when there was one: with captions nothing was downloaded. Otherwise the sound is deleted with the temporary folder it was fetched into, whatever happens.

Two addresses are refused before anything is fetched:

- **One that is not YouTube's.** Only `youtube.com`, `www.`, `m.` and `music.youtube.com`, and `youtu.be`. yt-dlp can fetch from a thousand sites and from plain addresses, so this keeps it from being pointed at anything else the host can reach.
- **One that is not one video.** A playlist would download and transcribe every video in it. `youtu.be/<id>`, `/watch?v=<id>`, `/shorts/<id>`, `/live/<id>`, `/embed/<id>` and `/v/<id>` name one video, and a watch address inside a playlist is taken as the one video it names.

```python
from vectrixdb.extract.youtube import is_youtube, video_id

video_id("https://www.youtube.com/watch?v=q3Results01&list=PL123")   # 'q3Results01'
is_youtube("https://www.youtube.com/playlist?list=PL123")            # False
```

When YouTube answers "Sign in to confirm you're not a bot", the `ExtractionError` says that it does this to cloud addresses and that the address is not at fault. `download=` replaces yt-dlp with a fetcher of your own: a callable `download(url, folder)` that answers the sound's path and what it learned, and, to have captions read first, `look(url)` and `read(url)` beside it.

A YouTube channel's feed, `https://www.youtube.com/feeds/videos.xml?channel_id=...`, gives each video's title and description to [a source](sources.md); `load_youtube` is what reads what is said.

## Podcasts

A podcast is a feed whose entries carry their episode's sound. `db.sources` keeps a collection in step with one: when the collection has an audio engine registered for the episodes' files, each new episode is downloaded and transcribed, and is cited to the second like any recording.

```python
from vectrixdb import Vectrix
from vectrixdb.extract.engines import Whisper

pods = Vectrix("pods", extractors={".mp3": Whisper("base"), ".m4a": Whisper("base")})
pods.sources.add("https://pod.example.com/feed.xml", every="1d")
pods.sources.refresh(max_items=5)
```

Without an engine for the episode's suffix, or with `transcribe=False`, an episode is indexed as its show notes, and nothing is downloaded to find out which. An episode may be up to 300 MB; `max_audio_bytes` sets another limit. The `audio` each chunk carries is the episode's address without the token a CDN signs it with. Scheduling, refreshing and what is refused are in [Follow a podcast](sources.md#follow-a-podcast).

## Through an extraction service

An extraction service hears recordings, videos and YouTube addresses for a server that does not, with Azure Speech and Document Intelligence: `/transcribe/audio`, `/transcribe/video`, `/transcribe/youtube` and `/transcribe/youtube_save`, a long job on a queue. A VectrixDB server sends it the suffixes named in `VECTRIXDB_EXTRACTOR_ROUTES`. Its settings, `VECTRIXDB_SPEECH_LOCALES`, `VECTRIXDB_SPEECH_SPEAKERS` and `VECTRIXDB_VIDEO_FRAMES`, are in [Recordings and videos](extraction-service.md#recordings-and-videos) and [YouTube videos](extraction-service.md#youtube-videos).
