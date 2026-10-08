# lockbox

Locks files and folders with a password so only you can open them. Drop something in, pick a password, and you get a `.lockbox` file that's just noise to anyone without it.

Uses AES-256-GCM for the encryption and Argon2id to turn your password into the key, so guessing passwords is slow even on a fast PC. If someone edits or cuts off a locked file, unlocking fails instead of giving back broken data.

**If you forget the password there's no way to get the files back.** I can't do anything about that and neither can anyone else.

## Install

    pip install -e .

## GUI

    lockbox-gui

Drop files or folders onto the window, type a password (or hit Generate), and press Lock. To open something, drop the `.lockbox` file in, type the password and press Unlock. It works out which is which by looking at the files.

- Folders get locked as one file and come back as a folder.
- Key file: optional extra. If you pick one, you need the password and that exact file to unlock.
- Hide the file name: locked file gets a random name. The real name is stored inside and comes back when you unlock.
- Delete the original: only happens after the locked copy has been opened and checked. "Overwrite it first" scribbles over it before deleting, but SSDs and backups can still keep copies.
- Passwords need at least 8 characters. The bar under the box is a rough guide, longer is better.
- Never overwrites anything. If the name's taken it adds a number.

## Command line

    lockbox lock secrets.txt                  makes secrets.txt.lockbox
    lockbox lock photos -o vault --hide-name  folder, random name, into ./vault
    lockbox lock a.pdf -k my.key --delete     needs my.key to open, deletes a.pdf after checking
    lockbox unlock secrets.txt.lockbox
    lockbox unlock vault                      every .lockbox in the folder
    lockbox info secrets.txt.lockbox          settings used, nothing secret

It asks for the password so it doesn't end up in your shell history. For scripts you can set `LOCKBOX_PASSWORD` instead.

## How it works

Each file has a small header, then the real file name and size (encrypted), then the data in 1 MB pieces. Every piece is authenticated and tied to the header and its position, and the last piece is marked, so reordering, changing or truncating anything gets caught. Each lock uses a fresh random salt and nonce.

The file name and size are encrypted too, but the total size of the `.lockbox` file is still visible, roughly the same as the original.

## What it can't do

- Hide that you have a locked file. It starts with `LKBX`.
- Protect you from a keylogger, or from a weak password.
- Recover anything if you lose the password or key file.
- Clean up the original properly on SSDs, backups or cloud sync.

## Tests

    python -m unittest discover tests

## Building the exe

    pip install pyinstaller
    build.bat

Gives you `dist\lockbox-gui.exe` and `dist\lockbox.exe`.

`python make_icon.py` remakes the icon.
