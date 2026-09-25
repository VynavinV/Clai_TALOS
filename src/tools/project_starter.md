---
name: project_starter
description: Initialize new project structure with standard directories
command: "mkdir -p {project_name}/{src,tests,docs} && echo 'Project {project_name} initialized' > {project_name}/README.md"
parameters: "project_name: string, path: string?"
---

# Project Starter

Initialize a new project with a standard directory structure.

**Usage:**
- `project_name`: Name for your new project (required)
- `path`: Optional parent directory (defaults to current directory)

**Creates:**
- `{project_name}/src/` - Source code directory
- `{project_name}/tests/` - Test files directory
- `{project_name}/docs/` - Documentation directory
- `{project_name}/README.md` - Initial README file

This skill helps you quickly scaffold new projects with a consistent structure.
