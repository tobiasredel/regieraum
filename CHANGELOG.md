# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/).

## [0.1.0] - 2026-11-11

First public version.

### Added

- Preparation: running order of segments, research dossiers with sources, several preparations side by side, choice of
  the preparation in the studio.
- Studio in the browser: local lossless recording of every participant (double-ender) with background upload, guests
  join through a link without an account, up to four participants, optional TURN relay, jingles from a soundboard.
- Broadcast mode: running order, video, level meters, soundboard and dossier on one screen.
- Live companion: live transcript, corrections, answers and follow-up questions with sources.
- Audio pipeline: alignment, crosstalk removal, noise reduction (DeepFilterNet), leveling and loudness mastering,
  jingle mix, chapters, transcript (faster-whisper), MP3.
- Review and editing: trim and cut on a waveform, chapters, show notes with suggestions, publish now or scheduled,
  discard a recording and redo it with the same running order.
- Own RSS feed with local or S3 compatible storage (tested with Cloudflare R2) and an importer for existing feeds.
- Move or copy a segment with its dossier to another preparation.
- Notes (`M`) for editing and chapters (`K`) set separately during recording; notes show as flags on the waveform and
  never end up in the episode.
- Sound check in the device check: record five seconds and listen back.
- Home screen app for the host (web app manifest) and broadcast mode on tablets in portrait.
- Raw data archive: after publishing, raw tracks move to an archive folder (`PS_ARCHIVE_DIR`) and can be restored.
- AI costs per episode, per segment and per month.
- Public show page (static, generated into the storage with the feed) and download counting, either in the app
  (privacy-friendly daily hashes, no IP addresses stored) or through a prefix service.
- Interface in German and English; content language follows the language of the show.
- Replaceable theme (`docs/THEME.md`), configurable show profile and running-order format.
- Docker image for `linux/amd64` and `linux/arm64` on GHCR, GitHub Actions for tests, operations guide and
  contributing guide.
