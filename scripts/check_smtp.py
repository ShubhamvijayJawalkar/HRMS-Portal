#!/usr/bin/env python3
"""Verify the application's SMTP configuration by actually sending a message.

Configuring `SMTP_HOST` is the easy part; knowing it *works* before an employee
discovers it does not is the part that matters. This is why it exists. The
application reports a failed send honestly now (an unconfigured transport returns
failure, so the event dead-letters rather than looking delivered), but an honest
failure discovered by a user is still a failure.

It checks the three things that actually break SMTP configuration, in order, and
stops at the first:

1. **Reachability and TLS.**  Port 465 is implicit TLS and 587 is STARTTLS; a
   mismatch raises at `starttls()` or during the handshake. This performs the same
   handshake the application will perform.
2. **Authentication.**  A wrong password or a provider that wants an app password
   fails here rather than on the first password reset.
3. **Delivery.**  Sends one real message and reports whether the relay accepted
   it. `--to` is required so this can never accidentally mail a real employee.

Usage::

    # uses the same env vars as the app (.env is loaded if python-dotenv is present)
    python scripts/check_smtp.py --to you@example.com

    # with an explicit subject and no real recipient
    python scripts/check_smtp.py --to ops@example.com --subject 'SMTP check'

Exit code 0 means a message was accepted by the relay. Note that acceptance is not
delivery: a relay that accepts a message for an unknown recipient can still bounce
it later, so `--to` should be an address you control.
"""

from __future__ import annotations

import argparse
import os
import smtplib
import ssl
import sys


def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--to', required=True,
                        help='recipient for the test message (use an address you control)')
    parser.add_argument('--subject', default='HRMS SMTP configuration check')
    parser.add_argument('--from', dest='sender', default=None,
                        help='override EMAIL_FROM for this check')
    args = parser.parse_args()
    load_env()

    host = os.getenv('SMTP_HOST', '')
    if not host:
        print('SMTP_HOST is not set — the application cannot send email at all.')
        print('\nSet it in .env (loaded by app.py) or in the container environment:')
        print('    SMTP_HOST=smtp.your-provider.com')
        return 1

    port = int(os.getenv('SMTP_PORT', '587'))
    user = os.getenv('SMTP_USER', '')
    password = os.getenv('SMTP_PASS', '')
    sender = args.sender or os.getenv('EMAIL_FROM', 'noreply@hrms.com')
    flag = os.getenv('SMTP_USE_SSL')
    implicit = (flag.strip().lower() in ('1', 'true', 'yes')) if flag is not None else (port == 465)
    timeout = float(os.getenv('SMTP_TIMEOUT_SECONDS', '15'))

    print(f'host      {host}:{port}')
    print(f'from      {sender}')
    print(f'auth      {"user " + user if user else "(none — open relay expected)"}')
    print(f'transport {"implicit TLS (SMTP_SSL)" if implicit else "STARTTLS"}')
    print()

    # 1. Reachable at all.
    try:
        server = (smtplib.SMTP_SSL if implicit else smtplib.SMTP)(host, port, timeout=timeout)
    except Exception as exc:
        print(f'FAIL  connect: {exc}')
        print('\n  Check the host and port. Inside Docker the host must be reachable from')
        print('  the container network, not from your laptop.')
        return 1
    print(f'OK    connected; server says: {server.noop()[0].decode(errors="replace")[:70]}')

    # 2. TLS.
    try:
        if implicit:
            server.ehlo()
            print('OK    implicit TLS negotiated')
        else:
            server.ehlo()
            if not server.has_extn('starttls'):
                server.close()
                print(f'FAIL  the server on port {port} does not advertise STARTTLS.')
                print('      Port 465 is implicit TLS; set SMTP_PORT=465 (or')
                print('      SMTP_USE_SSL=true) if this is an implicit-TLS relay.')
                return 1
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            print('OK    STARTTLS negotiated')
    except Exception as exc:
        server.close()
        print(f'FAIL  TLS: {exc}')
        print('\n  A certificate error here is usually a missing CA bundle or a')
        print('  self-signed certificate on an internal relay.')
        return 1

    # 3. Authentication — skipped when none was configured, because the application
    #    skips it too, and an open relay is a legitimate on-premise setup.
    try:
        if user:
            server.login(user, password)
            print(f'OK    authenticated as {user}')
        else:
            print('SKIP  no SMTP_USER set; the application will not attempt login')
    except Exception as exc:
        server.close()
        print(f'FAIL  authentication: {exc}')
        print('\n  Most providers need an app password rather than the account')
        print('  password, and Gmail requires an App Password with 2FA enabled.')
        return 1

    # 4. Delivery.
    try:
        from email.message import EmailMessage

        message = EmailMessage()
        message['From'] = sender
        message['To'] = args.to
        message['Subject'] = args.subject
        message.set_content(
            'This is a configuration check from the HRMS application. '
            'If you are reading it, outbound mail works.'
        )
        refused = server.send_message(message)
        server.close()
    except Exception as exc:
        print(f'FAIL  send: {exc}')
        return 1

    if refused:
        print(f'FAIL  the relay refused these recipients: {sorted(refused)}')
        print('      Accepted as a connection but rejected the envelope — usually a')
        print('      sender-identity policy or an unverified "from" address.')
        return 1

    print(f'\nPASS  the relay accepted a message to {args.to}')
    print('\nNow confirm the application sees it:')
    print('    curl -s localhost:10000/api/health | python -m json.tool')
    print("  `email_configured` must be true and `status` must be \"ok\".")
    print('\nAcceptance is not delivery: a relay can accept and then bounce. Confirm the')
    print('message actually arrives, and check /api/admin/outbox for dead-lettered events.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
