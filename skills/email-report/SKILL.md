---
name: email-report
description: Email a Markdown report, digest, or summary to the user. Use when asked to email or send something, or to deliver a scheduled digest by mail.
license: Apache-2.0
compatibility: Requires uv. Reads KOKUA_EMAIL_* settings and KOKUA_EMAIL_PASSWORD from the environment.
metadata:
  author: kokua
---

# Email a report

Sends Markdown to the user as formatted HTML with a plain-text fallback.

**You cannot choose the recipient.** The address comes from the host's configuration, so this can only
ever mail the user. There is no flag for it.

## Steps

1. Write the body to a file. Do not pass a document as a command-line argument: it will be too long
   and the quoting will break.

2. Send it:

   ```bash
   uv run scripts/send.py --subject "Weekly review" --body-file <markdown-file>
   ```

3. To attach something already in the downloads or images folder, name it once per file:

   ```bash
   uv run scripts/send.py --subject "Report" --body-file body.md --attach weekly-report.pdf
   ```

## Notes

- An attachment must be a **bare file name** that already exists in the downloads or images folder. A
  path, or a name that is not there, is rejected and **nothing is sent** rather than sending an email
  the user thinks carried the file.
- The subject must be a single line.
- If the host has not configured email, the script says so and sends nothing. It never prints the
  password, and on failure reports only the error type, because SMTP errors can echo credentials.
- Run `uv run scripts/send.py --help` for the full interface.
