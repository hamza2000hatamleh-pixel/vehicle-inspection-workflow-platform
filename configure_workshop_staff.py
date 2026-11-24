from getpass import getpass
import json
from pathlib import Path
import re
from secrets import token_bytes

from staff_access import ROLES, password_hash


def main():
    path = Path(__file__).with_name("workshop_staff.json")
    if path.exists():
        raise SystemExit("Workshop accounts already exist. Preserve the existing file before replacing the accounts.")
    accounts = []
    usernames = set()
    for role in ROLES:
        print(f"\n{role.capitalize()} account")
        while True:
            username = input("Username: ").strip()
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", username) and username not in usernames:
                break
            print("Use a unique username with letters, numbers, dots, dashes, or underscores.")
        usernames.add(username)
        while True:
            name = input("Name to record in case history: ").strip()
            if 1 <= len(name) <= 100:
                break
            print("Enter a name of 1 to 100 characters.")
        while True:
            password = getpass("Password: ")
            if len(password) < 12 or len(password.encode("utf-8")) > 512:
                print("Use at least 12 characters and at most 512 UTF-8 bytes.")
                continue
            if getpass("Confirm password: ") == password:
                break
            print("Passwords did not match.")
        salt = token_bytes(16)
        accounts.append(dict(username=username,name=name,role=role,salt=salt.hex(),
                             password_hash=password_hash(password,salt).hex()))
    with path.open("x", encoding="utf-8") as handle:
        json.dump(accounts, handle, indent=2)
    print("Staff sign-in is configured. Restart Vehicle Inspection.")


if __name__ == "__main__":
    main()

