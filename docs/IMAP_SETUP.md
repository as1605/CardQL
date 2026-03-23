# Gmail access: IMAP (recommended)

cardql fetches statement emails via **IMAP**. You use an **email** and **password** in config; for Gmail that password should be an **App Password** (not your normal account password).

---

## Why IMAP

- **No Google Cloud project** or OAuth consent screen.
- **One-time setup**: enable 2-Step Verification, create an App Password, put it in `secrets.json` under **`inboxes`**.
- Works with any IMAP provider (Gmail, Outlook, etc.) as long as you have host/port and credentials.

---

## Gmail: use an App Password

1. Enable **2-Step Verification** on your Google account:  
   [Google account → Security → 2-Step Verification](https://myaccount.google.com/security)
2. Create an **App Password**:  
   [App Passwords](https://myaccount.google.com/apppasswords) (or search "App Passwords" in your account).
   - Choose **Mail** and your device; copy the 16-character password.
3. In **`.local/config/secrets.json`** set **`inboxes`** with your email and password(s). The config uses **`email`** and **`passwords`** (no "app_password" in the key name — use an app password as the value):

```json
{
  "inboxes": [
    { "email": "you@gmail.com", "passwords": ["xxxx xxxx xxxx xxxx"] }
  ]
}
```

You can list multiple passwords per inbox (they are tried in order if login fails). For multiple inboxes, add more entries to **`inboxes`**; the default TO filter uses all listed emails.

Google help: [Sign in with App Passwords](https://support.google.com/accounts/answer/185833)

---

## Optional: Gmail API / OAuth

If you later add a Gmail API–based fetcher (e.g. for labels or history), you would:

1. Create a project in [Google Cloud Console](https://console.cloud.google.com/).
2. Enable the **Gmail API**.
3. Configure the **OAuth consent screen** and create **OAuth 2.0 Desktop** credentials.
4. Store the client JSON under `.local/credentials/` and implement an auth flow.

For the current IMAP-based fetch, **OAuth is not required**.
