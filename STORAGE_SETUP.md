# House of Jacob — persistent storage setup

This branch prepares the app to use PostgreSQL for accounts, posts, replies, reactions, and direct messages, and Supabase Storage for profile photos and post media.

## Important: fresh start

The app does not import the old SQLite database. If PostgreSQL is enabled against an empty database, users will need to register again and old posts/messages will not appear. This is intentional for the current fresh-start plan.

## Configure Supabase

1. Open the existing Supabase project for House of Jacob.
2. In **Project Settings → API**, copy the Project URL and the server-side secret key. Keep the key private.
3. In **Storage**, confirm the `community-media` bucket exists.
4. Because profile photos and post media are displayed to community members, configure the bucket as public. Do not put private documents or sensitive files in this bucket.
5. In **Project Settings → Database**, copy the PostgreSQL connection string intended for application use. Keep credentials private.

## Configure Render

In the Render service's **Environment** settings, set these values. Add secrets in Render only; never commit them to GitHub or paste them into chat.

- `HOUSE_OF_JACOB_STORAGE=postgres`
- `DATABASE_URL` = the Supabase PostgreSQL connection string
- `SUPABASE_URL` = the Supabase Project URL
- `SUPABASE_SECRET_KEY` = the server-side Supabase secret key
- `SUPABASE_MEDIA_BUCKET=community-media`
- `HOUSE_OF_JACOB_SECRET` = a long, random Flask session secret

Build command: `pip install -r requirements.txt`

Start command: `gunicorn app:app`

Do not change the live Render environment until the pull request has been reviewed and the deployment has been tested. When PostgreSQL mode is enabled, the app creates missing tables but does not migrate SQLite data.

## Smoke-test after deployment

1. Register a new test account.
2. Sign out and sign back in.
3. Create a text-only post and verify it remains after a redeploy/restart.
4. Upload a small image post and a profile picture; verify both display.
5. Add a reply and reaction.
6. Send a direct message between two test accounts and verify it remains after a restart.
7. Confirm the call page still opens.

If any test fails, turn off the new deployment or revert to the previous known-good commit; do not repeatedly retry uploads or expose secrets in logs.

## Media storage behavior

When Supabase credentials are configured, uploads are sent to the `community-media` bucket and the returned public URL is stored in PostgreSQL. Without those credentials, local development stores uploads in `static/uploads`; that local fallback is not durable on Render Free.
