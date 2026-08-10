# TECO Melbourne Taiwan-Entry Permit Appointment Monitor and Personal Booking Assistant

This project was created in response to the difficulty of securing an appointment
through the existing booking workflow. It also reflects my frustration with what I
experienced as TECO Melbourne's inadequate response and technically fragile system.
Despite these conditions, I built and successfully used a personal workflow through
to completion. This is an independent, unofficial project and is not affiliated with
or endorsed by TECO Melbourne.

This repository contains a Playwright-based monitor for the public TECO Melbourne
YouCanBookMe page. It demonstrates:

- multi-window availability polling;
- a pre-warmed Chromium workflow;
- direct selection of the detected slot with a UI fallback;
- verified form filling and independent pre-submit checks;
- explicit dry-run mode and human handling for visible CAPTCHA challenges.

The public tree contains no applicant data, notification topics, logs, screenshots,
form dumps, browser state, or generated bookmarklet. Configure all local values through
environment variables or ignored local files.

## Setup

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp autobook.conf.example autobook.conf
```

Set local applicant values only in your shell or a private environment file:

```sh
export TECO_FNAME='...'
export TECO_LNAME='...'
export TECO_EMAIL='...'
export TECO_PHONE='...'
export TECO_VISA_GRANT_NO='...'
export TECO_TRAVEL_DATE='DD/MM/YYYY'
export TECO_Q12='否'
export NTFY_TOPIC='your-private-topic'
```

### Notifications

Notifications are disabled by default. For a personal installation, set `NTFY_TOPIC`
to a private topic that you create yourself. Anyone who knows an ntfy topic name can
publish to it, so never commit the topic name or send applicant data, prefilled URLs,
or booking details through a shared topic. Set `NTFY_TOPIC=''` to disable alerts.

The safe default is `AUTOFILL_ENABLED=0`, which fills and verifies the form without
clicking the final submit button. Only enable final submission after independently
checking the form and the website rules.
The automation does not bypass or solve a visible CAPTCHA.

## Commands

```sh
python3 -m unittest -v
python3 test_frontend_learning_demo.py
python3 teco_autobook.py --self-check
python3 teco_autobook.py --live-form-dry-run
```

The shell scripts are optional macOS launchd examples. They use `NTFY_TOPIC` and
`SHARED_TOPIC` from the environment and do nothing when those variables are empty.
`launchd_runner.sh` supervises one pre-warmed browser process and restarts it if it
exits; `check_taiwan_permit.sh` is an independent notification/evidence monitor and
does not start a second booking process. The monitor does not contain a pre-filled URL.

## Privacy and safety

Do not commit `autobook.conf`, `autofill_bookmarklet.html`, logs, screenshots, form
dumps, browser tokens, or any applicant values. The `.gitignore` file excludes these
artifacts. Review the repository with `git diff --cached` before publishing.

This project is intended for lawful personal use and respects the booking site's
security controls. It does not automate CAPTCHA solving or payment.
