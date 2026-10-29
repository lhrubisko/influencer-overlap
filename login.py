"""Interactive Instagram login that saves a reusable local session.

    python login.py            # log in (reuses a valid saved session if there is one)
    python login.py --check    # only validate the saved session
    python login.py --force    # discard the saved session and log in again
    python login.py --logout   # delete the saved session file
    python login.py --sessionid   # use the sessionid cookie from a browser login instead of a password

Username: IG_USERNAME env/.env or prompt. Password: IG_PASSWORD env/.env or hidden prompt (preferred).
The password is never written to disk or printed. The session file contains auth cookies and is
created with 0600 permissions under .secrets/ (git-ignored).
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from instagrapi import exceptions as igx

from follower_overlap import collector, config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="validate the saved session and exit")
    group.add_argument("--force", action="store_true", help="ignore the saved session and log in again")
    group.add_argument("--logout", action="store_true", help="delete the saved session file")
    group.add_argument("--sessionid", action="store_true",
                       help="log in with the 'sessionid' cookie of a browser session (hidden prompt)")
    args = parser.parse_args()
    session_file = config.SESSION_FILE

    if args.sessionid:
        return login_with_sessionid(session_file)

    if args.logout:
        if session_file.exists():
            session_file.unlink()
            print(f"Deleted {session_file}")
        else:
            print("No saved session.")
        return 0

    if session_file.exists() and not args.force:
        try:
            client = collector.load_session_client(session_file)
            print(f"Saved session is valid (logged in as @{client.username or 'unknown'}).")
            return 0
        except collector.SessionError as exc:
            print(f"Saved session not usable: {exc}")
            if args.check:
                return 1
        except Exception as exc:  # noqa: BLE001
            print(f"Could not validate saved session: {collector.describe_error(exc)}")
            if args.check:
                return 1
    elif args.check:
        print(f"No saved session at {session_file}.")
        return 1

    username = config.IG_USERNAME or input("Instagram username: ").strip().lstrip("@")
    password = os.getenv("IG_PASSWORD") or getpass.getpass(f"Password for @{username} (hidden): ")
    if not username or not password:
        print("Username and password are required.")
        return 1

    client = collector.new_client()
    client.delay_range = None  # pacing is for follower collection; the login flow itself makes many requests
    print("Logging in to Instagram – this can take up to a minute, please wait …", flush=True)
    try:
        try:
            client.login(username, password)
        except igx.TwoFactorRequired:
            code = input("Two-factor code (authenticator app / SMS): ").strip().replace(" ", "")
            if not code:
                print("No code entered; aborting.")
                return 1
            # Exactly one follow-up attempt with the code; no retry loop.
            client.login(username, password, verification_code=code)
    except (igx.ChallengeError, igx.CaptchaChallengeRequired, igx.AccountSuspended) as exc:
        print(f"\n{collector.CHECKPOINT_HELP}\n({type(exc).__name__})")
        return 2
    except igx.TwoFactorRequired:
        print("Two-factor code was not accepted. Wait for a fresh code and run `python login.py` again.")
        return 1
    except (igx.BadPassword, igx.BadCredentials) as exc:
        print(f"Login rejected: {type(exc).__name__}. Check username/password. "
              "Repeated failed logins can trigger a checkpoint – do not loop this.")
        return 1
    except (igx.PleaseWaitFewMinutes, igx.RateLimitError, igx.ClientThrottledError) as exc:
        print(f"Instagram asked to slow down ({type(exc).__name__}). Wait at least an hour before trying again.")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Login failed: {collector.describe_error(exc)}")
        return 1
    finally:
        password = None  # noqa: F841 – drop the reference early

    collector.save_session(client, session_file)
    print(f"Logged in as @{username}. Session saved to {session_file} (permissions 600).")
    return 0


def login_with_sessionid(session_file) -> int:
    """Reuse a session created by logging in on instagram.com in a browser (skips the password login flow)."""
    print("Log in on https://www.instagram.com in your browser, then DevTools (F12) → Application → Cookies →\n"
          "https://www.instagram.com → copy the value of 'sessionid'. Treat it like a password.")
    sessionid = getpass.getpass("sessionid (hidden): ").strip().strip('"')
    if not sessionid:
        print("No sessionid entered; aborting.")
        return 1
    client = collector.new_client()
    client.delay_range = None
    print("Checking the session with Instagram …", flush=True)
    try:
        client.login_by_sessionid(sessionid)
        client.account_info()
    except AssertionError:
        print("That does not look like a sessionid (it starts with your numeric user ID, e.g. '1234567%3A…').")
        return 1
    except collector.AUTH_ERRORS as exc:
        print(collector.auth_message(exc))
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"Session login failed: {collector.describe_error(exc)}")
        return 1
    finally:
        sessionid = None  # noqa: F841
    collector.save_session(client, session_file)
    print(f"Logged in as @{client.username}. Session saved to {session_file} (permissions 600).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
