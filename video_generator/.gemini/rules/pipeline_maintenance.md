---
name: video_generator_pipeline_maintenance
description: Critical guidelines for updating the video generator pipeline, cron scripts, and XTTS formatting.
---
# Video Generator Pipeline Guidelines

### 1. Cron Scripts Synchronization
When making changes to the video generation logic (e.g., adding new arguments between scripts like `--topic` or `--images`, or updating AI prompts), you MUST always update the corresponding cron bash scripts (`video_virale.sh` and `video_promo.sh`). These scripts orchestrate the pipeline manually via `agy` and bypass `agente_tiktok.py`, so they will fail to apply new logic unless explicitly updated.

### 2. XTTS Voice Cloning Formatting
When modifying text that will be fed to the XTTS engine (Step 1):
- **Numbers**: Always prompt the LLM to write all numbers as words (e.g., "mille", not "1000").
- **Punctuation**: Do NOT feed `.`, `!`, or `?` directly to XTTS, otherwise it will literally pronounce the word "punto". Strip them and replace them with `\n` to chunk the audio correctly and maintain pacing.
- **Special Characters**: Strip all quotes, asterisks, and brackets before XTTS processes the text.
- **Emojis**: Emojis must ONLY be injected during the visual subtitle generation step (e.g., `whisper_captions.py`). Never put emojis in the base text script file.
