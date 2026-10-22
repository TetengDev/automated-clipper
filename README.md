# Automated Clipper

24/7 auto-clipper. It watches 6 creators (MrBeast, IShowSpeed, Kai Cenat, Raffy Tulfo in Action, Ivana Alawi, Cong TV) for new long-form videos and stream VODs. For each one it transcribes the audio, has Claude pick the highlights, and renders captioned 9:16 clips. A human then approves or rejects them in a small review app.

- PRD: [docs/PRD.md](docs/PRD.md)
- Review workflow: [docs/REVIEW_WORKFLOW.md](docs/REVIEW_WORKFLOW.md)
- Deployment: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
- Tracking: Linear project "Automated Clipper" (team CLP)

## Quickstart (local)

```bash
uv sync
cp .env.example .env              # fill in keys as features need them
uv run clipper check-config       # validate config/creators.yaml
uv run clipper migrate            # create data/state.db
uv run pytest                     # tests
docker compose build              # image with ffmpeg + yt-dlp
```

Transcription runs locally with faster-whisper by default (`transcriber: local`,
`whisper_model: large-v3-turbo` in `config/creators.yaml`, also per creator). The model is
downloaded once into `data/models/`. `GROQ_API_KEY` is needed only with `transcriber: groq`.
Compare models on a sample with `uv run clipper bench-transcribe audio.flac --models
large-v3-turbo,large-v3`.

## Adding a creator

1. Add an entry to `config/creators.yaml`.
2. Run `uv run clipper check-config`.
3. Run `uv run clipper resolve`. This step depends on CLP-22, which isn't built yet.
