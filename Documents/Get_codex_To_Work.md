# Step-by-step

1. Close all VS Code windows.

2. Open the terminal and run:

```bash
code .
```

If Codex opens normally, the issue is probably with the VS Code shortcut used by Ubuntu.


3. Create a local copy of the VS Code shortcut:
```bash
mkdir -p ~/.local/share/applications

cp /usr/share/applications/code.desktop \
  ~/.local/share/applications/code.desktop
```

4. Update the executable used by the shortcut:
```bash
sed -i \
  's|Exec=/usr/share/code/code|Exec=/usr/bin/code|g' \
  ~/.local/share/applications/code.desktop
```

5. Refresh the desktop application database:
```bash
update-desktop-database ~/.local/share/applications
```

6. Confirm that the change was applied:
```bash
grep '^Exec=' ~/.local/share/applications/code.desktop
```

The output should look similar to this:

```bash
Exec=/usr/bin/code %F
Exec=/usr/bin/code --new-window %F
```

7. Remove the current VS Code icon from your favorites or dock.

8. Search for “Visual Studio Code” again in the Ubuntu applications menu, open it, and pin the new icon to your favorites.

If Ubuntu still uses the old shortcut, log out and log back in, or restart the computer.

As a temporary workaround, you can continue opening VS Code from the terminal with:

```bash
code .
```