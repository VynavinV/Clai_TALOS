---
name: git_commit
description: Create a git commit with an automated message
command: "cd {repo_path} && git add . && git commit -m '{commit_message}'"
parameters: "repo_path: string, commit_message: string"
---

# Git Commit Helper

Automate git commits with standardized messages.

**Usage:**
- `repo_path`: Path to the git repository (required)
- `commit_message`: Custom commit message (required)

**What it does:**
- Stages all changes in the repository
- Creates a commit with your message
- Timestamps the commit automatically

Useful for quick commits during development or when automating workflows.
