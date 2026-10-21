# slack-bkp

Daily incremental backup of all public Slack channels to Google Cloud Storage.

For each public channel, the script joins it if needed, fetches any messages
posted since the last run, resolves user IDs to display names, and appends a
plain-text transcript per channel/day to GCS. Thread replies are expanded
inline, and any file/image attachments are downloaded and re-uploaded to GCS
alongside the transcript. Sync state (last seen timestamp per channel) is
kept locally and mirrored to GCS so it survives on an ephemeral host.

## Setup

1. Create a Slack app/bot with scopes `channels:history`, `channels:read`,
   `channels:join`, `users:read`, `files:read`, and install it to your
   workspace. Copy the bot token.
2. Authenticate to GCS via Application Default Credentials:
   `gcloud auth application-default login` (or run on a host/VM with a
   service account attached — no key file needed).
3. Copy `.env.example` to `.env` and fill in `SLACK_BOT_TOKEN` and
   `GCS_BUCKET` (see `.env.example` for optional vars).
4. Run once manually to verify: `./run_backup.sh`

## Running on a schedule

Add to crontab for a daily run at 03:00:

```
0 3 * * * /Users/hmka/repos/slack-bkp/run_backup.sh
```

`run_backup.sh` creates a `.venv` on first run, installs dependencies from
`requirements.txt`, loads `.env`, and writes logs to `logs/<date>.log`.

## Output layout

```
gs://<bucket>/<prefix>/transcripts/<channel>/<YYYY-MM-DD>.txt
gs://<bucket>/<prefix>/files/<channel>/<file_id>_<file_name>
gs://<bucket>/<prefix>/state/last_ts.json
```

## Known limitations

- Public channels only — private channels and DMs would need a user token
  and broader scopes.
- Thread-reply backfill only catches replies to parent messages seen within
  that day's incremental window, so a reply posted long after its parent
  could be missed.
