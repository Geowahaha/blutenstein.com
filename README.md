# Blutenstein Portal

Thai-first landing and MVP portal skeleton for Blutenstein — AI-powered factory automation for Thai SME factories and marketplace sellers.

## Features
- Landing page
- Pricing
- Waitlist/demo form
- Dashboard mock
- Integration status
- Telegram notification from environment variables only
- LINE Messaging API push notification from environment variables only
- LINE webhook endpoint to capture userId/groupId/roomId for push targets

## Run
```bash
cp .env.example .env
docker compose up -d --build
```

## Deploy path
`/opt/blutenstein-portal` on `43.128.75.149`.

## LINE support after LINE Notify retirement
LINE Notify ended service on 2025-03-31. Blutenstein does not use LINE Notify.

Use LINE Messaging API instead:
1. In LINE Developers, create/use a Messaging API channel.
2. Set webhook URL to `https://api.blutenstein.com/api/line/webhook` after DNS/HTTPS is active.
3. Add the bot as friend or invite it to the target group.
4. Send a message to the bot/group.
5. Read `/data/line_sources.jsonl` in the container volume and set `LINE_MESSAGING_TO` to the captured `source.userId`, `source.groupId`, or `source.roomId`.
6. Keep `Blutenstein_LINEChannel_access_token_long-lived` or `LINE_CHANNEL_ACCESS_TOKEN` in `.env` only.

The legacy env name `LINE_NOTIFY_TO` is accepted as an alias, but the value must be a LINE Messaging API target ID, not a LINE Notify token.
