---
name: claude-cli
description: Run Claude Code CLI for coding tasks, file operations, research, and complex problem solving
requires:
  bins:
    - claude
---

# Claude Code CLI

Use Claude Code CLI for complex tasks that benefit from file access, code execution, or multi-step reasoning.

## Run a Task

```bash
claude --print --dangerously-skip-permissions "$TASK_DESCRIPTION"
```

## Examples

**Research a topic:**
```bash
claude --print "Research the latest gold price drivers and summarize in 3 bullet points"
```

**Edit a file:**
```bash
claude --print "Read /opt/gold-trader/app/config.py and explain what it does"
```

**Debug code:**
```bash
claude --print "Check the logs at journalctl -u gold-trader -n 50 and diagnose any errors"
```

## When to use

- Coding tasks (writing, debugging, explaining code)
- File operations (reading, analyzing files on the server)
- Complex multi-step research
- System administration tasks
- Any task that benefits from tool use and file access

## Rules

- Always use --print flag for non-interactive output
- Use --dangerously-skip-permissions for autonomous operation
- Report the results back to the user clearly
- Do not modify critical system files without explicit permission
