# Shell Bash: Agent Policy

Execute shell commands in the shared working volume mount.

Tools are registered as `<service name>-<tool>`, e.g.
`shell-bash-start_command`; below they're named by tool alone.

## Why

This service runs apart from the agent, with the shared working folder
(e.g. `/working`) mounted into it. That storage isn't on the agent's own
file system: the agent's built-in tools for reading, writing, editing,
listing, or searching files don't reach it, and a path such as
`/working/app.py` given to them names a different file, or none.

The isolation is intentional: storage is kept apart from the agent, and
this service is the mediated way in. Every operation on stored files goes
through this shell.

## Rules

1. Read, write, edit, list, search, move, and delete stored files with
   `start_command`, never with the agent's own file tools.
2. Don't take a missing file or an empty result from the agent's own tools
   as the state of the shared folder; check it through the shell.
3. Keep work under the shared working folder, and pass it, or a folder in
   it, as `cwd`. Anything written elsewhere stays inside this service,
   unseen by other services, and is lost when it restarts.
4. Run each command to completion: poll `get_command_latest` until
   `running` is false, then `drop_command` it. Check its stderr lines
   before taking a write or edit as done.
5. Read a file before overwriting or editing it, and read back the changed
   lines after.
6. Where another service's direction covers a task on stored files (e.g.
   navigating code with an analyze service), follow it for that task, and
   use this shell for the rest.

## How to work with stored files

- Read a file: `cat -n <path>`; a range of lines:
  `sed -n '40,80p' <path>`.
- List and find: `ls -la <dir>`, `fd <pattern> <dir>`; search contents:
  `rg -n "<pattern>" <dir>`.
- Write a whole file with a quoted heredoc, so nothing in it is expanded:

  ```bash
  mkdir -p /working/app && cat > /working/app/main.py <<'EOF'
  ...content...
  EOF
  ```

  Pick a delimiter that never appears alone on a line of the content.
  Append with `cat >> <path> <<'EOF'`.
- Edit by exact replacement with python, failing unless the old text
  matches once:

  ```bash
  python3 - <<'EOF'
  import pathlib
  p = pathlib.Path("/working/app/main.py")
  s = p.read_text()
  old = r'''...old text...'''
  new = r'''...new text...'''
  assert s.count(old) == 1, f"{s.count(old)} matches"
  p.write_text(s.replace(old, new))
  EOF
  ```

  Keep `sed -i` for simple substitutions on a single line.
- Move, copy, and delete with `mv`, `cp`, and `rm`, after looking at the
  target.
