# Learn with DonumAI

An open-source generator for an automated educational podcast. Each run picks a new topic, writes a short script with Google's Gemini API, turns it into audio and publishes the episode to Podbean.

Episodes are AI-generated. Say so in the podcast description so listeners know.

## Features

- New topic every run: topics come from Gemini as structured JSON and are checked against the history, so repeats and near-duplicates are rejected.
- Short scripts: storytelling style, around 300 words, pitched at Grade 6.
- Audio with gTTS, saved locally as `episode.mp3`.
- Publishing to Podbean through its API, with timeouts and checked responses so failures appear in the logs.
- Protected trigger and background job: one run at a time, started by a secret key.

## How it works

1. A scheduler calls the trigger endpoint (or you run the job from the command line).
2. The app picks a topic and subject, saves it to the history in `data/topics.json`.
3. Gemini writes the script.
4. gTTS converts the script to `data/episode.mp3`.
5. Gemini writes an episode title and description.
6. The app obtains a Podbean token, uploads the audio and creates the episode.

All Gemini calls use the Interactions API with `store=False`.

### Routes

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Plain-text status line |
| `/trigger/hello` | GET | Starts a podcast job (requires `TRIGGER_KEY`) |
| `/trigger/podcast` | GET | Same as above |

The trigger returns `202` when a job starts, `409` if one is already running and `403` for a missing or wrong key.

## Getting started

Requirements: Python 3.10 or newer, a Google Gemini API key from [Google AI Studio](https://aistudio.google.com/) and, for publishing, a Podbean developer app.

```bash
git clone <your-repository-url>
cd <your-repository-folder>
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp env.example .env              # then edit .env
python flask_app.py podcast      # run one episode now
python flask_app.py              # run the trigger server at http://127.0.0.1:5000
```

Set `PODBEAN_PUBLISH=0` while testing to generate the audio without publishing.

## Configuration

Settings are read from a `.env` file next to `flask_app.py`. Never commit this file.

| Variable | Required | Meaning |
|---|---|---|
| `GOOGLE_API_KEY` | Yes | Gemini API key |
| `TRIGGER_KEY` | For the web trigger | Secret required by the trigger endpoints |
| `PODBEAN_CLIENT_ID` | To publish | Podbean app client ID |
| `PODBEAN_CLIENT_SECRET` | To publish | Podbean app client secret |
| `PODBEAN_PUBLISH` | No | `1` to publish (default), `0` to skip publishing |
| `TEXT_MODEL` | No | Defaults to `gemini-3.5-flash-lite` |

## Scheduling

Either option works:

- Web trigger: call `https://YOUR-SITE/trigger/hello` from a scheduler such as cron-job.org. Send the key in an `X-Trigger-Key` header (preferred) or as `?key=`. A cron expression for 10 PM on Mondays, Wednesdays and Fridays is `0 22 * * 1,3,5` (check the time zone your scheduler uses).
- Command line: run `python flask_app.py podcast` as a scheduled task on your host. No key is needed.

## Deployment (PythonAnywhere)

1. Upload the project and create a virtualenv.
2. Install dependencies inside that virtualenv: `pip install -U -r requirements.txt`.
3. Set the virtualenv path in the **Web** tab.
4. In the WSGI file, import the app:

   ```python
   from flask_app import app as application
   ```

5. Create the `.env` file, then reload the web app.

## Project structure

```
flask_app.py            Flask app and podcast pipeline
requirements.txt        Python dependencies
env.example             Template for the .env file
data/                   Created at runtime (topics.json, episode.mp3)
```

## Limitations

- Scripts are AI-generated and can contain errors. Review episodes if accuracy matters.
- gTTS is a lightweight text-to-speech option and sounds synthetic.
- Model availability, pricing and free-tier quotas are controlled by Google and may change.
- Podbean's API and terms are controlled by Podbean.

## Contributing

Issues and pull requests are welcome. For larger changes, please open an issue first to discuss what you would like to change.

## License

Released under the [MIT License](LICENSE).

## Author

Created by [Kannan Murugapandian](https://kannan.bearblog.dev).
