"""One-time login + camera picker. Run this first, then use Blink Live.bat."""
import asyncio
import json
import sys
from getpass import getpass
from pathlib import Path

from aiohttp import ClientSession
from blinkpy.blinkpy import Blink
from blinkpy.auth import Auth, BlinkTwoFARequiredError
from blinkpy.helpers.util import json_load

HERE = Path(__file__).parent
CREDS = HERE / "credentials.json"
CONFIG = HERE / "config.json"


async def main():
    session = ClientSession()
    blink = Blink(session=session)

    if CREDS.exists():
        print(f"Using saved credentials at {CREDS}")
        blink.auth = Auth(await json_load(str(CREDS)), no_prompt=True, session=session)
    else:
        username = input("Blink email: ").strip()
        password = getpass("Blink password: ")
        blink.auth = Auth(
            {"username": username, "password": password},
            no_prompt=True,
            session=session,
        )

    try:
        await blink.start()
    except BlinkTwoFARequiredError:
        code = input("Enter the 2FA code Blink emailed/texted you: ").strip()
        await blink.send_2fa_code(code)

    if not blink.available:
        print("Login failed. Delete credentials.json and try again.")
        await session.close()
        sys.exit(1)

    await blink.save(str(CREDS))
    print(f"\nCredentials saved to {CREDS}")

    cameras = list(blink.cameras.keys())
    if not cameras:
        print("No cameras found on this account.")
        await session.close()
        sys.exit(1)

    print("\nCameras on your account:")
    for i, name in enumerate(cameras, 1):
        print(f"  {i}. {name}")

    while True:
        choice = input(f"\nPick one (1-{len(cameras)}): ").strip()
        try:
            picked = cameras[int(choice) - 1]
            break
        except (ValueError, IndexError):
            print("Not a valid choice, try again.")

    CONFIG.write_text(json.dumps({"camera": picked}, indent=2), encoding="utf-8")
    print(f"\nSaved {picked!r} to {CONFIG}")
    print("Double-click 'Blink Live.bat' to open the live view.")

    await session.close()


if __name__ == "__main__":
    asyncio.run(main())
