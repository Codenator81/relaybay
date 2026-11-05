# Remove the integration

Take relaybay's hooks out of your Claude Code user settings (only relaybay's own entries; a backup
of the file is written first):

```bash
relaybay setup --remove --dry-run        # preview the removal and its hash
relaybay setup --remove --expect <hash>  # write exactly that plan
```

The same is the **Remove** button under **Integrations** on the board.

Remove the MCP server from Claude Code:

```bash
claude mcp remove relaybay --scope user
```

Your data stays in the data folder (`relaybay.sqlite`, `token`, `config.toml`, `backups/`) printed by
`relaybay serve`; delete the folder to remove it. Backups of the settings file are next to it as
`settings.json.relaybay-backup-<timestamp>`.
